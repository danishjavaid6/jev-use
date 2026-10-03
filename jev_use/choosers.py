"""Choosers.

A chooser answers exactly one question: *given this state and these candidates,
which candidate id?* It never invents an action, never writes text, and never
touches the desktop.

Two implementations:

* ``MockChooser``  - deterministic, credential-free. Replays a script or cycles
  through candidates. Used for dry runs, tests and replay.
* ``JevChooser``   - TypeSafe's System One API. One parallel call carrying up to
  four typed questions, returns a candidate id plus a calibrated confidence.

Both go through the same ``validate`` step, so the loop cannot tell them apart
and cannot be made to execute an unvalidated action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .candidates import KEYS, KINDS, Observation

MIN_CONFIDENCE_DEFAULT = 0.4


@dataclass
class Decision:
    """What the chooser decided, before validation."""

    kind: str
    element_id: str | None = None
    key: str | None = None
    confidence: float = 0.0
    source: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)
    rejection: str | None = None

    @property
    def accepted(self) -> bool:
        return self.rejection is None


class Chooser(Protocol):
    def choose(
        self,
        goal: str,
        observation: Observation,
        history: list[str],
    ) -> Decision: ...


def validate(decision: Decision, observation: Any) -> Decision:
    """The provider-agnostic gate every decision must pass before execution.

    Checks against the operations *this* observation offers, so the desktop set and
    the browser set (which adds `navigate`) both work without a branch.
    """
    operations = observation.operations()
    if decision.kind not in operations:
        decision.rejection = (
            f"operation {decision.kind!r} is not offered here "
            f"(have: {', '.join(sorted(operations))})"
        )
        return decision

    if decision.kind in ("click_element", "type_text"):
        if not decision.element_id:
            decision.rejection = f"{decision.kind} without an element id"
            return decision
        legal = {c["id"] for c in observation.targets_for(decision.kind)}
        if decision.element_id not in legal:
            decision.rejection = (
                f"element {decision.element_id!r} is not a legal target for "
                f"{decision.kind} in the current snapshot"
            )
            return decision

    if decision.kind == "press_key":
        if decision.key not in KEYS:
            decision.rejection = f"key {decision.key!r} is not offered"
            return decision

    return decision


#: Leading filler verbs stripped before a goal is matched against a control label.
_LEADING_VERB = re.compile(
    r"^(?:please\s+)?(?:click|tap|press|open|go\s+to|goto|navigate\s+to|visit|select|choose|hit)\s+",
    re.I,
)
# Whole-goal patterns, anchored at BOTH ends and tolerating only a polite filler
# word. A prefix match would run the first clause of a compound goal and silently
# drop the rest: "go back and open settings", "scroll down then tap Checkout" and
# "go home after saving" must all reach Jev, which can weigh the whole instruction
# (and choose `done`). Only a goal that IS the single action is decided here.
_FILLER = r"(?:\s+(?:now|please))?"
_BACK_GOAL = re.compile(
    rf"^(?:please\s+)?(?:go\s+|press\s+)?(?:the\s+)?back{_FILLER}$", re.I
)
_HOME_GOAL = re.compile(
    rf"^(?:please\s+)?(?:go\s+|press\s+)?(?:the\s+)?home{_FILLER}$", re.I
)
_SCROLL_DOWN_GOAL = re.compile(rf"^(?:please\s+)?scroll\s+down{_FILLER}$", re.I)
_SCROLL_UP_GOAL = re.compile(rf"^(?:please\s+)?scroll\s+up{_FILLER}$", re.I)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def deterministic_decision(goal: str, observation: Any) -> Decision | None:
    """Decide an unambiguous action in code, without a model round trip.

    Jev is valuable exactly where the choice is ambiguous; where it is not, the
    ~200-400 ms decision is pure latency. Every branch below fires only when the
    operation is genuinely offered AND, for a tap, exactly one control matches —
    the same closed-set rule the model is held to, so nothing the model could
    refuse is executed here.

    Callers apply this on the FIRST step only: a goal like "go back" still matches
    after the action, so re-deciding it every step would loop until max_steps.
    """
    operations = observation.operations()
    goal_text = _normalize(goal)

    if "go_back" in operations and _BACK_GOAL.match(goal_text):
        return Decision(kind="go_back", confidence=1.0, source="deterministic")
    if "go_home" in operations and _HOME_GOAL.match(goal_text):
        return Decision(kind="go_home", confidence=1.0, source="deterministic")
    if "scroll_down" in operations and _SCROLL_DOWN_GOAL.match(goal_text):
        return Decision(kind="scroll_down", confidence=1.0, source="deterministic")
    if "scroll_up" in operations and _SCROLL_UP_GOAL.match(goal_text):
        return Decision(kind="scroll_up", confidence=1.0, source="deterministic")

    # A goal that is nothing but a URL (once the verb and the URL itself are
    # removed) can only mean one thing. A compound goal keeps its URL and is left
    # to Jev.
    url = getattr(observation, "navigate_url", None)
    if "navigate" in operations and url:
        remainder = _LEADING_VERB.sub("", goal_text)
        for form in {url.lower(), _normalize(url), re.sub(r"^https?://", "", url.lower())}:
            remainder = remainder.replace(form, " ")
        if _normalize(remainder) in ("", "the page", "this page"):
            return Decision(kind="navigate", confidence=1.0, source="deterministic")

    # A single control whose label is exactly the goal (minus its leading verb).
    if "click_element" in operations:
        wanted = _normalize(_LEADING_VERB.sub("", goal_text))
        if wanted:
            matches = [
                c
                for c in observation.targets_for("click_element")
                if wanted in {_normalize(c.get("label", "")), _normalize(c.get("description", ""))}
            ]
            if len(matches) == 1:
                return Decision(
                    kind="click_element",
                    element_id=matches[0]["id"],
                    confidence=1.0,
                    source="deterministic",
                )
    return None


def request_state(goal: str, observation: Observation, history: list[str]) -> dict[str, Any]:
    """The `state` object handed to the chooser.

    Text only. No image ever enters this payload, which is what keeps the loop
    cheap and keeps the accessibility tree as the single source of truth.

    `already_done` and `window_shows` are the two fields that make one-action-at-a-
    time decision making work. Jev is a decision point, not a planner: it evaluates
    each question against this state alone, so the state has to carry both what has
    already been tried and what the window currently displays.
    """
    return {
        "goal": goal,
        "window": {"title": observation.title, "pid": observation.pid},
        "already_done": history[-8:],
        "window_shows": observation.readouts(),
        "elements": observation.option_map(),
        "step": len(history),
    }


class MockChooser:
    """Deterministic, credential-free chooser.

    With no script it clicks each candidate in turn and then reports done, which
    is enough to exercise observation, validation, execution and re-observation
    without a provider.
    """

    def __init__(self, script: list[dict[str, Any]] | None = None, confidence: float = 0.95) -> None:
        self.script = script or []
        self.confidence = confidence

    def choose(self, goal: str, observation: Observation, history: list[str]) -> Decision:
        step = len(history)

        if self.script:
            if step >= len(self.script):
                return Decision(kind="done", confidence=1.0, source="mock")
            entry = dict(self.script[step])
            kind = entry.get("kind", "click_element")
            element_id = entry.get("element_id")
            if element_id is None and "element_index" in entry:
                index = entry["element_index"]
                if 0 <= index < len(observation.candidates):
                    element_id = observation.candidates[index]["id"]
            return Decision(
                kind=kind,
                element_id=element_id,
                key=entry.get("key"),
                confidence=float(entry.get("confidence", self.confidence)),
                source="mock",
                raw=entry,
            )

        if not observation.candidates:
            return Decision(kind="impossible", confidence=1.0, source="mock")

        if step >= len(observation.candidates):
            return Decision(kind="done", confidence=1.0, source="mock")

        return Decision(
            kind="click_element",
            element_id=observation.candidates[step]["id"],
            confidence=self.confidence,
            source="mock",
        )


class JevChooser:
    """TypeSafe System One chooser.

    One parallel call carrying several deliberately narrow questions:

    * ``operation``    - what should happen next, from the operations this
      snapshot can actually perform.
    * ``click_target`` - which element, drawn *only* from elements that expose a
      real activation action. Target heads are speculative: they are asked in the
      same round trip as the operation and only the matching one is used, so the
      extra question costs tokens but almost no latency.
    * ``key``          - which key, when the operation is press_key. Asked only
      when press_key is actually offered, which the browser and Android engines
      never do.

    A target Choice is never padded with legal-but-irrelevant elements: Jev's
    confidence measures how concentrated the distribution is, so a bloated
    option set reads as uncertainty even when the answer is obvious.
    """

    #: Pinned deliberately. ``jev-latest`` resolves to a moving version, so a
    #: confidence threshold tuned against today's answers can drift tomorrow.
    DEFAULT_MODEL = "jev-1.13.0"

    def __init__(self, model: str | None = None, client: Any | None = None) -> None:
        self.model = model or self.DEFAULT_MODEL
        self._client = client
        self.last_model_reported: str | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from typesafe_sdk import TypeSafeClient
            except ImportError as exc:  # pragma: no cover - depends on env
                raise RuntimeError(
                    "typesafe-sdk is not installed. Run: uv add typesafe-sdk "
                    "(or pip install typesafe-sdk) and set TYPESAFE_API_KEY."
                ) from exc
            self._client = TypeSafeClient(model=self.model)
        return self._client

    def choose(self, goal: str, observation: Observation, history: list[str]) -> Decision:
        from typesafe_sdk import Choice

        operations = observation.operations()
        heads = observation.target_heads()

        questions: dict[str, Any] = {
            "operation": Choice(
                instructions=(
                    f"Goal: {goal!r}. Decide the SINGLE next action. "
                    "Read `already_done` for what has already been tried and "
                    "`window_shows` for what the window currently displays. "
                    "Do not repeat an action whose effect is already visible. "
                    "Choose done only when `window_shows` reflects the goal being met. "
                    "Choose impossible only if nothing offered can help."
                ),
                criteria=operations,
            ),
        }

        # `key` is asked ONLY when press_key is on the table. The browser and
        # Android engines never offer press_key, so asking it unconditionally put a
        # dead question and a six-option criteria set on every decision they made —
        # paid in payload and inference work, with no way for the answer to matter.
        if "press_key" in operations:
            questions["key"] = Choice(
                instructions=(
                    "If operation is press_key, which key should be sent? Otherwise choose return."
                ),
                criteria=KEYS,
            )

        # Speculative target heads: one per operation that takes a target, each
        # containing ONLY elements legal for that operation. Asked in the same round
        # trip as the operation and used only when the matching operation wins.
        for head, options in heads.items():
            if not options:
                continue
            if head == "click_target":
                instructions = (
                    "If operation is click_element, which element should be activated? "
                    "Every listed element supports activation. Otherwise choose the "
                    "element least related to the goal."
                )
            else:
                instructions = (
                    "If operation is type_text, which form field should be filled? "
                    "Otherwise choose the field least related to the goal."
                )
            questions[head] = Choice(instructions=instructions, criteria=options)

        response = self.client.system_one(
            state=request_state(goal, observation, history),
            questions=questions,
        )

        answers = response.answers
        self.last_model_reported = getattr(response, "model", None)

        operation_answer = answers["operation"]
        key_answer = answers.get("key") if hasattr(answers, "get") else None
        operation = operation_answer.choice

        # Operation -> the head that carries its target, if any.
        head_for = {"click_element": "click_target", "type_text": "text_target"}
        head = head_for.get(operation)
        target_answer = (
            answers.get(head) if (head and hasattr(answers, "get")) else None
        )
        options = heads.get(head, {}) if head else {}

        element_id = None
        if target_answer is not None:
            chosen = target_answer.choice
            # Closed sets stay closed: a target is accepted only if it was in the
            # option set this same snapshot produced.
            element_id = chosen if chosen in options else None

        # Gate on the WEAKEST answer, not the operation alone. Measured failure:
        # for "compute 9 times 6" the operation came back at confidence 1.00 while
        # the target distribution was 9=0.45 vs pi=0.36 — a coin flip that the
        # operation's confidence hid completely. Acting on that is how the run
        # produced a malformed expression.
        confidences = [float(operation_answer.confidence)]
        if target_answer is not None:
            confidences.append(float(target_answer.confidence))
        elif operation == "press_key" and key_answer is not None:
            confidences.append(float(key_answer.confidence))
        confidence = min(confidences)

        return Decision(
            kind=operation,
            element_id=element_id,
            key=key_answer.choice if key_answer is not None else None,
            confidence=confidence,
            source=f"jev:{self.last_model_reported or self.model}",
            raw={
                "operation": _dump(operation_answer),
                "target_head": head,
                "target": _dump(target_answer) if target_answer else None,
                "key": _dump(key_answer),
                "min_confidence": confidence,
            },
        )


def _dump(answer: Any) -> dict[str, Any]:
    return {
        "choice": getattr(answer, "choice", None),
        "confidence": getattr(answer, "confidence", None),
        "probabilities": getattr(answer, "probabilities", None),
    }
