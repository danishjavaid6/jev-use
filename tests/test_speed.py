"""Tests for the speed machinery: batching, plan extraction, replay and cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jev_use.cache import PlanCache
from jev_use.candidates import Observation
from jev_use.choosers import Decision, MockChooser
from jev_use.driver import DriverError
from jev_use.loop import plan_from_steps, replay_plan, run

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "calculator.json"


class FakeDriver:
    """Records driver calls and always answers get_window_state with the fixture."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, arguments: dict | None = None):
        self.calls.append((tool, arguments or {}))

        class _Result:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        if tool == "get_window_state":
            return _Result(self.payload)
        return _Result({})

    @property
    def clicks(self) -> list[dict]:
        return [a for t, a in self.calls if t == "click"]

    @property
    def observations(self) -> int:
        return sum(1 for t, _ in self.calls if t == "get_window_state")


@pytest.fixture()
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture()
def observation(payload: dict) -> Observation:
    return Observation.from_payload(payload)


# -- batching ---------------------------------------------------------------


def test_actions_per_snapshot_amortises_observation(payload: dict) -> None:
    """Five clicks should cost one snapshot, not five."""
    driver = FakeDriver(payload)
    chooser = MockChooser(script=[{"kind": "click_element", "element_index": i} for i in range(5)])

    result = run(
        driver, "goal", 1, 2, chooser, act=True, max_steps=5, actions_per_snapshot=5
    )

    assert result.actions == 5
    assert result.observations == 1, "batching should collapse the snapshots"
    assert driver.observations == 1
    assert len(driver.clicks) == 5


def test_actions_per_snapshot_one_reobserves_every_action(payload: dict) -> None:
    driver = FakeDriver(payload)
    chooser = MockChooser(script=[{"kind": "click_element", "element_index": i} for i in range(3)])

    result = run(driver, "goal", 1, 2, chooser, act=True, max_steps=3, actions_per_snapshot=1)

    assert result.actions == 3
    assert result.observations == 3
    assert driver.observations == 3


def test_terminal_decision_stops_immediately(payload: dict) -> None:
    driver = FakeDriver(payload)
    chooser = MockChooser(script=[{"kind": "done"}])
    result = run(driver, "goal", 1, 2, chooser, act=True, max_steps=9, actions_per_snapshot=5)
    assert result.outcome == "done"
    assert result.actions == 0


class FailingDriver(FakeDriver):
    """Fails every click, the way a transient AT-SPI/X11 error does."""

    def call(self, tool: str, arguments: dict | None = None):
        if tool == "click":
            self.calls.append((tool, arguments or {}))
            raise DriverError("AT-SPI element click failed: X11 error")
        return super().call(tool, arguments)


def test_a_failing_action_does_not_kill_the_run(payload: dict) -> None:
    driver = FailingDriver(payload)
    chooser = MockChooser(script=[{"kind": "click_element", "element_index": i} for i in range(3)])

    result = run(driver, "goal", 1, 2, chooser, act=True, max_steps=6, actions_per_snapshot=1)

    assert result.outcome == "action_failed"
    assert result.actions == 0
    assert all("failed" in s.note for s in result.steps)
    assert len(result.steps) == 3, "should retry after each failure, then give up"


def test_a_failure_aborts_the_rest_of_its_batch(payload: dict) -> None:
    """After a failed action the snapshot may be stale, so re-observe."""
    driver = FailingDriver(payload)
    chooser = MockChooser(script=[{"kind": "click_element", "element_index": 1}])
    result = run(driver, "goal", 1, 2, chooser, act=True, max_steps=1, actions_per_snapshot=5)
    assert result.observations == 1, "one snapshot, one attempt"


# -- plan extraction --------------------------------------------------------


def test_plan_stores_labels_not_tokens(payload: dict) -> None:
    driver = FakeDriver(payload)
    chooser = MockChooser(script=[{"kind": "click_element", "element_index": 1}])
    result = run(driver, "goal", 1, 2, chooser, act=True, max_steps=1)

    plan = plan_from_steps(result.steps)
    assert plan == [{"kind": "click_element", "label": "7"}]
    assert "p1:3" not in json.dumps(plan), "tokens are snapshot-scoped and must not be cached"


def test_plan_includes_press_key() -> None:
    from jev_use.loop import Step

    step = Step(
        0,
        Observation(1, 2, "w", []),
        Decision(kind="press_key", key="return"),
        executed=True,
    )
    assert plan_from_steps([step]) == [{"kind": "press_key", "key": "return"}]


def test_plan_skips_unexecuted_steps(payload: dict) -> None:
    driver = FakeDriver(payload)
    chooser = MockChooser(script=[{"kind": "click_element", "element_index": 1}])
    result = run(driver, "goal", 1, 2, chooser, act=False, max_steps=1)
    assert plan_from_steps(result.steps) == []


# -- replay -----------------------------------------------------------------

SAMPLE_PLAN = [
    {"kind": "click_element", "label": "Clear"},
    {"kind": "click_element", "label": "7"},
    {"kind": "press_key", "key": "return"},
]


def test_replay_executes_the_plan_without_a_model(payload: dict, observation: Observation) -> None:
    driver = FakeDriver(payload)
    result = replay_plan(driver, SAMPLE_PLAN, observation, act=True)

    assert result.outcome == "replayed"
    assert result.actions == 3
    assert result.observations == 1
    assert [c["element_token"] for c in driver.clicks] == ["p1:2", "p1:3"]
    assert all(s.decision.source == "cache" for s in result.steps)


def test_replay_is_dry_by_default(payload: dict, observation: Observation) -> None:
    driver = FakeDriver(payload)
    result = replay_plan(driver, SAMPLE_PLAN, observation, act=False)
    assert result.outcome == "replayed"
    assert driver.clicks == []


def test_replay_aborts_on_a_missing_label(payload: dict, observation: Observation) -> None:
    """A stale plan must stop, not guess."""
    driver = FakeDriver(payload)
    result = replay_plan(driver, [{"kind": "click_element", "label": "Nonexistent"}], observation)
    assert result.outcome == "replay_miss"
    assert driver.clicks == []


def test_replay_aborts_partway_rather_than_continuing(
    payload: dict, observation: Observation
) -> None:
    driver = FakeDriver(payload)
    plan = [{"kind": "click_element", "label": "Clear"}, {"kind": "click_element", "label": "Gone"}]
    result = replay_plan(driver, plan, observation, act=True)
    assert result.outcome == "replay_miss"
    assert len(driver.clicks) == 1, "must not keep acting past the miss"


def test_replay_rejects_unknown_entries(payload: dict, observation: Observation) -> None:
    driver = FakeDriver(payload)
    result = replay_plan(driver, [{"kind": "teleport"}], observation)
    assert result.outcome == "replay_miss"


# -- cache ------------------------------------------------------------------


def test_cache_round_trip(tmp_path: Path) -> None:
    cache = PlanCache(tmp_path / "c.json")
    assert cache.get("Calculator", "compute 6 x 7") is None
    cache.put("Calculator", "compute 6 x 7", SAMPLE_PLAN)
    assert cache.get("Calculator", "compute 6 x 7") == SAMPLE_PLAN


def test_cache_key_is_case_and_space_insensitive(tmp_path: Path) -> None:
    cache = PlanCache(tmp_path / "c.json")
    cache.put("  Calculator ", "Compute 6 x 7", SAMPLE_PLAN)
    assert cache.get("calculator", "compute 6 x 7") == SAMPLE_PLAN


def test_cache_separates_windows_and_goals(tmp_path: Path) -> None:
    cache = PlanCache(tmp_path / "c.json")
    cache.put("Calculator", "goal a", SAMPLE_PLAN)
    assert cache.get("Calculator", "goal b") is None
    assert cache.get("TextEditor", "goal a") is None


def test_cache_drop(tmp_path: Path) -> None:
    cache = PlanCache(tmp_path / "c.json")
    cache.put("Calculator", "g", SAMPLE_PLAN)
    cache.drop("Calculator", "g")
    assert cache.get("Calculator", "g") is None


def test_cache_ignores_empty_plans(tmp_path: Path) -> None:
    cache = PlanCache(tmp_path / "c.json")
    cache.put("Calculator", "g", [])
    assert len(cache) == 0


def test_cache_survives_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text("{not json")
    cache = PlanCache(path)
    assert len(cache) == 0
    cache.put("Calculator", "g", SAMPLE_PLAN)
    assert cache.get("Calculator", "g") == SAMPLE_PLAN


def test_cache_reports_hits_and_misses(tmp_path: Path) -> None:
    cache = PlanCache(tmp_path / "c.json")
    cache.put("Calculator", "g", SAMPLE_PLAN)
    cache.get("Calculator", "g")
    cache.get("Calculator", "nope")
    assert cache.summary() == {"entries": 1, "hits": 1, "misses": 1}
