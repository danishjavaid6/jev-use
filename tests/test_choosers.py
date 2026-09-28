"""Chooser tests, including the confidence gate found by the hard test suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jev_use.candidates import Observation
from jev_use.choosers import JevChooser, validate

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "calculator.json"


class FakeAnswer:
    def __init__(self, choice: str, confidence: float, probabilities: dict | None = None) -> None:
        self.choice = choice
        self.confidence = confidence
        self.probabilities = probabilities or {}


class FakeResponse:
    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.model = "jev-fake-1.0"


class FakeClient:
    """Records the questions it was asked and replays canned answers."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.questions: dict | None = None

    def system_one(self, state, questions):
        self.questions = questions
        return FakeResponse(self.answers)


@pytest.fixture()
def observation() -> Observation:
    return Observation.from_payload(json.loads(FIXTURE.read_text()))


def test_confidence_is_the_weakest_answer(observation: Observation) -> None:
    """The measured failure: operation 1.00 while the target was 0.45 vs 0.36.

    Gating on the operation alone let a coin flip through and produced a malformed
    expression. The decision must carry the weakest link instead.
    """
    chooser = JevChooser(
        client=FakeClient(
            {
                "operation": FakeAnswer("click_element", 1.00),
                "click_target": FakeAnswer("p1:3", 0.36, {"p1:3": 0.45, "p1:20": 0.36}),
                "key": FakeAnswer("return", 0.99),
            }
        )
    )
    decision = chooser.choose("compute 9 times 6", observation, [])
    assert decision.kind == "click_element"
    assert decision.confidence == pytest.approx(0.36), "must not report the operation's 1.00"


def test_a_confident_target_keeps_a_confident_decision(observation: Observation) -> None:
    chooser = JevChooser(
        client=FakeClient(
            {
                "operation": FakeAnswer("click_element", 0.98),
                "click_target": FakeAnswer("p1:3", 0.95),
                "key": FakeAnswer("return", 0.99),
            }
        )
    )
    decision = chooser.choose("compute 9 times 6", observation, [])
    assert decision.confidence == pytest.approx(0.95)


def test_press_key_gates_on_the_key_answer(observation: Observation) -> None:
    chooser = JevChooser(
        client=FakeClient(
            {
                "operation": FakeAnswer("press_key", 1.00),
                "click_target": FakeAnswer("p1:3", 0.99),
                "key": FakeAnswer("return", 0.41),
            }
        )
    )
    decision = chooser.choose("confirm the dialog", observation, [])
    assert decision.confidence == pytest.approx(0.41)


def test_an_out_of_set_target_is_dropped(observation: Observation) -> None:
    """Closed sets stay closed."""
    chooser = JevChooser(
        client=FakeClient(
            {
                "operation": FakeAnswer("click_element", 0.9),
                "click_target": FakeAnswer("not-in-this-snapshot", 0.9),
                "key": FakeAnswer("return", 0.9),
            }
        )
    )
    decision = chooser.choose("goal", observation, [])
    assert decision.element_id is None
    assert not validate(decision, observation).accepted


def test_click_element_withholds_click_target_when_nothing_is_activatable() -> None:
    """Only supported operations are offered, so the target question is skipped."""
    inert = Observation(
        1, 2, "inert",
        [{"element_index": 0, "element_token": "t:0", "role": "spin button",
          "label": "Precision", "actions": ["set_value"]}],
    )
    client = FakeClient(
        {
            "operation": FakeAnswer("press_key", 0.9),
            "click_target": FakeAnswer("t:0", 0.9),
            "key": FakeAnswer("down", 0.8),
        }
    )
    chooser = JevChooser(client=client)
    chooser.choose("goal", inert, [])
    assert "click_target" not in client.questions, "no legal target means no target question"
    assert "operation" in client.questions


def test_key_is_not_asked_when_press_key_is_unavailable() -> None:
    """The browser and Android engines never offer press_key, so the `key`
    question and its six-option criteria set were dead weight on every decision."""
    from jev_use import browser

    obs = browser.Observation(
        browser.Target(port=1, pid=2, window_id=3),
        {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "Home"}]},
    )
    client = FakeClient(
        {
            "operation": FakeAnswer("click_element", 0.9),
            "click_target": FakeAnswer("0", 0.9),
        }
    )
    decision = JevChooser(client=client).choose("open home", obs, [])
    assert "key" not in client.questions, "no press_key means no key question"
    assert "operation" in client.questions
    assert decision.kind == "click_element"


def _browser_observation(elements: list[dict]) -> object:
    from jev_use import browser

    return browser.Observation(
        browser.Target(port=1, pid=2, window_id=3), {"url": "u", "elements": elements}
    )


def test_deterministic_decision_takes_a_uniquely_named_control() -> None:
    from jev_use.choosers import deterministic_decision

    obs = _browser_observation(
        [
            {"ref": 0, "tag": "a", "text": "Learn more"},
            {"ref": 1, "tag": "input", "text": "Search", "fillable": True},
        ]
    )
    fixed = deterministic_decision("click Learn more", obs)
    assert fixed is not None and fixed.kind == "click_element"
    assert fixed.element_id == "0"
    assert fixed.source == "deterministic"
    assert deterministic_decision("make the page nicer", obs) is None


def test_deterministic_decision_ignores_an_ambiguous_label() -> None:
    """Two identical labels are a real choice; the model must make it."""
    from jev_use.choosers import deterministic_decision

    obs = _browser_observation(
        [
            {"ref": 0, "tag": "a", "text": "Sign in"},
            {"ref": 1, "tag": "a", "text": "Sign in"},
        ]
    )
    assert deterministic_decision("click Sign in", obs) is None


def test_deterministic_decision_opts_into_an_explicit_url() -> None:
    from jev_use.choosers import deterministic_decision

    obs = _browser_observation([])
    obs.navigate_url = "https://example.com"
    fixed = deterministic_decision("go to example.com", obs)
    assert fixed is not None and fixed.kind == "navigate"
    assert deterministic_decision("search for cats on example.com", obs) is None


def test_state_carries_goal_journal_and_current_display(observation: Observation) -> None:
    """The two fields that make one-action-at-a-time decisions work."""
    client = FakeClient(
        {
            "operation": FakeAnswer("click_element", 0.9),
            "click_target": FakeAnswer("p1:3", 0.9),
            "key": FakeAnswer("return", 0.9),
        }
    )
    chooser = JevChooser(client=client)
    chooser.choose("compute 9 times 6", observation, ['clicked "9"'])
    # request_state is called inside choose(); rebuild it to inspect the contract.
    from jev_use.choosers import request_state

    state = request_state("compute 9 times 6", observation, ['clicked "9"'])
    assert state["already_done"] == ['clicked "9"']
    assert "window_shows" in state
    assert state["goal"] == "compute 9 times 6"
