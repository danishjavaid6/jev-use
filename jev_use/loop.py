"""The observe -> decide -> validate -> execute -> re-observe loop.

Speed comes from doing fewer round trips, not from a faster model. Measured on
this driver, one AT-SPI snapshot costs ~750 ms and one click ~1350 ms regardless
of configuration (tighter `max_elements`/`max_depth` bounds are actually *slower*
and return fewer elements). The protocol floor is ~4 ms, so essentially all of
that is driver-side enumeration work.

So the loop is built to amortise perception:

* One snapshot serves up to ``actions_per_snapshot`` actions. Clicking five
  buttons does not require five accessibility walks when the buttons do not move.
* There is no fixed post-action sleep. Re-observation is the verification; a
  blanket sleep is pure latency. ``settle_seconds`` exists for apps that need it
  and defaults to 0.

Together those take a five-click task from ~14.6 s to ~7.6 s with no model change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .candidates import Observation, observe
from .choosers import MIN_CONFIDENCE_DEFAULT, Chooser, Decision, validate
from .driver import DriverError

TERMINAL = ("done", "impossible")
MAX_ACTION_FAILURES = 3


@dataclass
class Step:
    index: int
    observation: Observation
    decision: Decision
    executed: bool
    note: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    goal: str
    steps: list[Step]
    outcome: str
    seconds: float
    observations: int = 0

    @property
    def actions(self) -> int:
        return sum(1 for s in self.steps if s.executed)


def run(
    driver: Any,
    goal: str,
    pid: int,
    window_id: int,
    chooser: Chooser,
    *,
    act: bool = False,
    max_steps: int = 12,
    min_confidence: float = MIN_CONFIDENCE_DEFAULT,
    actions_per_snapshot: int = 1,
    settle_seconds: float = 0.0,
    on_step: Any = None,
) -> RunResult:
    """Drive one window toward `goal`.

    `act=False` is a dry run: every decision is made and validated, nothing is
    sent to the desktop.

    `actions_per_snapshot` is the speed/safety knob. At 1 the loop re-observes
    before every action, which is the most conservative choice and matches the
    driver's documented invariant. Higher values reuse one snapshot for several
    actions — sound when the UI does not re-layout between them (a keypad, a
    list of checkboxes) and unsafe when it does, because a reused element token
    may then resolve to a different control.
    """
    started = time.monotonic()
    history: list[str] = []
    steps: list[Step] = []
    outcome = "max_steps"
    observations = 0
    failures = 0
    index = 0
    batch = max(1, actions_per_snapshot)

    while index < max_steps:
        observation = observe(driver, pid, window_id)
        observations += 1

        for _ in range(batch):
            if index >= max_steps:
                break

            decision = validate(chooser.choose(goal, observation, history), observation)

            if not decision.accepted:
                steps.append(
                    Step(index, observation, decision, False, f"refused: {decision.rejection}")
                )
                history.append(f"refused: {decision.rejection}")
                outcome = "refused"
                index += 1
                return RunResult(goal, steps, outcome, time.monotonic() - started, observations)

            if decision.kind in TERMINAL:
                steps.append(Step(index, observation, decision, False, decision.kind))
                history.append(decision.kind)
                outcome = decision.kind
                index += 1
                return RunResult(goal, steps, outcome, time.monotonic() - started, observations)

            if decision.confidence < min_confidence:
                note = f"low confidence {decision.confidence:.2f} < {min_confidence:.2f}"
                steps.append(Step(index, observation, decision, False, note))
                history.append(f"escalate: {note}")
                outcome = "low_confidence"
                index += 1
                return RunResult(goal, steps, outcome, time.monotonic() - started, observations)

            note = describe_action(decision, observation)
            executed = False
            if act:
                try:
                    execute(driver, decision, observation)
                    executed = True
                    note = f"acted: {note}"
                    if settle_seconds > 0:
                        time.sleep(settle_seconds)
                except DriverError as exc:
                    # A driver-side action failure (transient AT-SPI/X11 errors are
                    # real) must not kill the run. Count it, then re-observe: the
                    # snapshot the action came from may now be stale.
                    failures += 1
                    note = f"failed ({failures}/{MAX_ACTION_FAILURES}): {note} -> {str(exc)[:70]}"
            else:
                note = f"would: {note}"

            steps.append(Step(index, observation, decision, executed, note, decision.raw))
            history.append(short_action(decision, observation))
            index += 1

            if on_step is not None:
                on_step(steps[-1])

            if act and not executed:
                if failures >= MAX_ACTION_FAILURES:
                    outcome = "action_failed"
                    return RunResult(goal, steps, outcome, time.monotonic() - started, observations)
                break  # drop the rest of this batch and re-observe

    return RunResult(goal, steps, outcome, time.monotonic() - started, observations)


def short_action(decision: Decision, observation: Observation) -> str:
    """A terse journal entry — this is what the chooser reads back as history.

    Verbose step notes are for humans; the model needs to see what it already did
    in as few tokens as possible, so it can tell whether the next action is done.
    """
    if decision.kind == "click_element":
        candidate = observation.by_id(decision.element_id or "")
        return f'clicked "{candidate["label"]}"' if candidate else "clicked unknown"
    if decision.kind == "press_key":
        return f"pressed {decision.key}"
    return decision.kind


def describe_action(decision: Decision, observation: Observation) -> str:
    if decision.kind == "click_element":
        candidate = observation.by_id(decision.element_id or "")
        label = candidate["description"] if candidate else decision.element_id
        return f"{decision.kind} {label} (conf {decision.confidence:.2f})"
    if decision.kind == "press_key":
        return f"{decision.kind} {decision.key} (conf {decision.confidence:.2f})"
    return f"{decision.kind} (conf {decision.confidence:.2f})"


def execute(driver: Any, decision: Decision, observation: Observation) -> None:
    """Send exactly one already-validated action."""
    target = {"pid": observation.pid, "window_id": observation.window_id}

    if decision.kind == "click_element":
        driver.call("click", {**target, "element_token": decision.element_id})
        return

    if decision.kind == "press_key":
        driver.call("press_key", {**target, "key": decision.key})
        return

    if decision.kind in ("scroll_down", "scroll_up"):
        driver.call(
            "scroll",
            {**target, "direction": "down" if decision.kind == "scroll_down" else "up", "amount": 3},
        )
        return

    if decision.kind == "wait":
        time.sleep(1.0)
        return

    raise ValueError(f"no executor for kind {decision.kind!r}")


# -- action caching ---------------------------------------------------------


def plan_from_steps(steps: list[Step]) -> list[dict[str, str]]:
    """Reduce a successful run to a replayable plan.

    Element tokens are snapshot-scoped and worthless later, so the plan stores
    the human-readable label and the executor re-resolves it against a fresh
    snapshot. That is what makes a repeat run cost one snapshot and zero model
    calls.
    """
    plan: list[dict[str, str]] = []
    for step in steps:
        if not step.executed:
            continue
        if step.decision.kind == "click_element":
            candidate = step.observation.by_id(step.decision.element_id or "")
            if candidate is None:
                return []
            plan.append({"kind": "click_element", "label": candidate["label"]})
        elif step.decision.kind == "press_key" and step.decision.key:
            plan.append({"kind": "press_key", "key": step.decision.key})
    return plan


def replay_plan(
    driver: Any,
    plan: list[dict[str, str]],
    observation: Observation,
    *,
    act: bool = False,
    settle_seconds: float = 0.0,
) -> RunResult:
    """Execute a cached plan against one fresh snapshot, matching by label.

    Costs one snapshot and zero model calls. A label that no longer resolves
    aborts the replay rather than guessing — a stale plan is worse than no plan.
    """
    started = time.monotonic()
    steps: list[Step] = []

    for index, entry in enumerate(plan):
        kind = entry.get("kind")
        if kind == "click_element":
            match = next(
                (c for c in observation.candidates if c["label"] == entry.get("label")),
                None,
            )
            if match is None:
                steps.append(
                    Step(index, observation, Decision(kind="cache", source="cache"), False,
                         f"replay_miss: no candidate labelled {entry.get('label')!r}")
                )
                return RunResult("", steps, "replay_miss", time.monotonic() - started, 1)
            decision = Decision(
                kind="click_element", element_id=match["id"], confidence=1.0, source="cache"
            )
        elif kind == "press_key":
            decision = Decision(
                kind="press_key", key=entry.get("key"), confidence=1.0, source="cache"
            )
        else:
            steps.append(
                Step(index, observation, Decision(kind="cache", source="cache"), False,
                     f"replay_miss: unknown plan entry {kind!r}")
            )
            return RunResult("", steps, "replay_miss", time.monotonic() - started, 1)

        note = f"cache: {describe_action(decision, observation)}"
        if act:
            execute(driver, decision, observation)
            note = f"cache: acted {entry.get('label') or entry.get('key')}"
            if settle_seconds > 0:
                time.sleep(settle_seconds)
        steps.append(Step(index, observation, decision, act, note))

    return RunResult("", steps, "replayed", time.monotonic() - started, 1)
