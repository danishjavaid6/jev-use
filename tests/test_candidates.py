"""Pure-logic tests: candidate construction, operation filtering, and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jev_use.candidates import (
    ACTIVATION_ACTIONS,
    Observation,
    ancestor_path,
    available_operations,
    build_candidates,
    describe,
    position_hint,
)
from jev_use.choosers import Decision, MockChooser, validate
from jev_use.cli import parse_script
from jev_use.loop import describe_action

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "calculator.json"

# The fixture holds 14 elements: a frame, two labels, ten buttons ("press"), and
# one spin button that is actionable but NOT activatable ("set_value", "focus").
TOTAL_ELEMENTS = 14
ACTIONABLE = 11
ACTIVATABLE = 10


@pytest.fixture()
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture()
def observation(payload: dict) -> Observation:
    return Observation.from_payload(payload)


# -- candidate construction ------------------------------------------------


def test_only_actionable_elements_become_candidates(observation: Observation) -> None:
    assert len(observation.elements) == TOTAL_ELEMENTS
    assert len(observation.candidates) == ACTIONABLE
    assert all(c["actions"] for c in observation.candidates)


def test_targets_are_a_strict_subset_of_candidates(observation: Observation) -> None:
    """An actionable element is not automatically an activatable one."""
    assert len(observation.targets) == ACTIVATABLE
    assert len(observation.targets) < len(observation.candidates)
    assert all(c["clickable"] for c in observation.targets)


def test_set_value_only_element_is_a_candidate_but_not_a_target(
    observation: Observation,
) -> None:
    precision = next(c for c in observation.candidates if c["label"] == "Precision")
    assert precision["clickable"] is False
    assert precision["id"] not in observation.target_map()


def test_activation_actions_are_exactly_what_makes_a_target() -> None:
    element = {
        "element_index": 1,
        "element_token": "t:1",
        "role": "button",
        "label": "x",
        "actions": ["set_value"],
    }
    assert build_candidates([element], None)[0]["clickable"] is False

    for action in sorted(ACTIVATION_ACTIONS):
        element["actions"] = [action]
        assert build_candidates([element], None)[0]["clickable"] is True


def test_candidate_ids_are_unique(observation: Observation) -> None:
    ids = [c["id"] for c in observation.candidates]
    assert len(ids) == len(set(ids))


# -- the what / where / supports metadata ----------------------------------


def test_description_carries_what_where_and_supports(observation: Observation) -> None:
    equals = next(c for c in observation.candidates if c["label"] == "Equals")
    assert equals["description"].startswith('push button "Equals"')
    assert " - in Calculator" in equals["description"]
    assert " - supports press" in equals["description"]


def test_where_is_empty_without_a_parent_chain() -> None:
    orphan = {
        "element_index": 5,
        "element_token": "t:5",
        "role": "button",
        "label": "lonely",
        "actions": ["press"],
    }
    candidate = build_candidates([orphan], None)[0]
    assert candidate["where"] == ""
    assert candidate["description"] == 'button "lonely" - supports press'


def test_ancestor_path_stops_on_a_cycle() -> None:
    """A malformed tree must not hang the walk."""
    cyclic = {"element_index": 1, "parent_index": 2, "role": "button", "label": "a"}
    parent = {"element_index": 2, "parent_index": 1, "role": "group", "label": "b"}
    assert ancestor_path(cyclic, {1: cyclic, 2: parent}) == "b"


def test_ancestor_path_is_outermost_first() -> None:
    by_index = {
        1: {"element_index": 1, "parent_index": None, "role": "window", "label": "Root"},
        2: {"element_index": 2, "parent_index": 1, "role": "panel", "label": "Middle"},
    }
    leaf = {"element_index": 3, "parent_index": 2, "role": "button", "label": "Leaf"}
    assert ancestor_path(leaf, by_index) == "Root > Middle"


def test_duplicate_labels_get_positional_hints(observation: Observation) -> None:
    """Two buttons read '6'. They must not be offered as identical options."""
    descriptions = [c["description"] for c in observation.candidates if '"6"' in c["description"]]
    assert len(descriptions) == 2
    assert len(set(descriptions)) == 2, f"options are ambiguous: {descriptions}"
    assert any("middle-centre" in d for d in descriptions)
    assert any("middle-right" in d for d in descriptions)


def test_describe_includes_value_when_it_differs() -> None:
    text = describe({"role": "label", "label": "Result", "value": "42"})
    assert text == "label \"Result\" currently showing '42'"


def test_position_hint_needs_both_frame_and_bounds() -> None:
    assert position_hint(None, {"width": 10, "height": 10}) == ""
    assert position_hint({"x": 1, "y": 1, "w": 1, "h": 1}, None) == ""


def test_build_candidates_ignores_elements_without_tokens() -> None:
    elements = [{"role": "button", "label": "x", "actions": ["press"]}]
    assert build_candidates(elements, None) == []


# -- readouts ---------------------------------------------------------------


def test_readouts_exclude_activation_targets(observation: Observation) -> None:
    """Buttons are noise in state; the model already sees them as candidates."""
    readouts = observation.readouts()
    assert "7" not in readouts
    assert "Equals" not in readouts


def test_readouts_include_the_display(observation: Observation) -> None:
    """A display field must be visible even when it is technically a candidate.

    The fixture's 'Precision' spin button is a candidate (set_value/focus) but not
    an activation target, exactly like a calculator display carrying clipboard
    actions. Excluding all candidates would hide the one thing the model needs.
    """
    readouts = observation.readouts()
    assert "0" in readouts, "the Result field's current value"
    assert "2" in readouts, "the Precision field's current value"


def test_readouts_prefer_the_value_over_the_label(observation: Observation) -> None:
    """State answers 'what does the window show', so the value wins."""
    readouts = observation.readouts()
    assert "0" in readouts
    assert "Result" not in readouts, "the label is not what the window displays"


def test_readouts_are_deduplicated_and_capped(observation: Observation) -> None:
    readouts = observation.readouts(limit=2)
    assert len(readouts) == 2
    assert len(observation.readouts()) == len(set(observation.readouts()))


# -- operation filtering ----------------------------------------------------


def test_all_operations_are_offered_when_a_target_exists(observation: Observation) -> None:
    operations = available_operations(observation)
    assert set(operations) == {
        "click_element",
        "press_key",
        "scroll_down",
        "scroll_up",
        "wait",
        "done",
        "impossible",
    }


def test_click_element_is_withheld_when_nothing_is_activatable() -> None:
    """Only supported operations are offered."""
    inert = Observation(
        1,
        2,
        "inert",
        [
            {
                "element_index": 0,
                "element_token": "t:0",
                "role": "spin button",
                "label": "Precision",
                "actions": ["set_value"],
            }
        ],
    )
    assert inert.candidates, "still a candidate"
    assert not inert.targets, "but not a target"
    assert "click_element" not in available_operations(inert)


# -- validation -------------------------------------------------------------


def test_validation_rejects_unknown_element(observation: Observation) -> None:
    decision = validate(Decision(kind="click_element", element_id="p9:99"), observation)
    assert not decision.accepted
    assert "not a legal target" in decision.rejection


def test_validation_rejects_unknown_kind(observation: Observation) -> None:
    decision = validate(Decision(kind="rm -rf"), observation)
    assert not decision.accepted


def test_validation_rejects_unoffered_key(observation: Observation) -> None:
    decision = validate(Decision(kind="press_key", key="f13"), observation)
    assert not decision.accepted


def test_validation_rejects_click_when_no_target_is_activatable() -> None:
    inert = Observation(
        1,
        2,
        "inert",
        [
            {
                "element_index": 0,
                "element_token": "t:0",
                "role": "spin button",
                "label": "Precision",
                "actions": ["set_value"],
            }
        ],
    )
    decision = validate(Decision(kind="click_element", element_id="t:0"), inert)
    assert not decision.accepted
    # click_element is withheld entirely when nothing is activatable, so the
    # rejection names the operation rather than the target.
    assert "not offered" in decision.rejection


def test_validation_accepts_a_real_candidate(observation: Observation) -> None:
    real_id = observation.targets[0]["id"]
    decision = validate(Decision(kind="click_element", element_id=real_id), observation)
    assert decision.accepted


# -- mock chooser -----------------------------------------------------------


def test_mock_chooser_cycles_then_finishes(observation: Observation) -> None:
    chooser = MockChooser()
    history: list[str] = []
    seen = []
    for _ in range(len(observation.candidates)):
        decision = chooser.choose("anything", observation, history)
        seen.append(decision.element_id)
        history.append("step")
    assert seen == [c["id"] for c in observation.candidates]
    assert chooser.choose("anything", observation, history).kind == "done"


def test_mock_chooser_script_resolves_element_index(observation: Observation) -> None:
    chooser = MockChooser(script=[{"kind": "click_element", "element_index": 2}])
    decision = chooser.choose("goal", observation, [])
    assert decision.element_id == observation.candidates[2]["id"]


def test_mock_chooser_reports_impossible_with_no_candidates() -> None:
    empty = Observation(1, 2, "blank", [])
    assert MockChooser().choose("goal", empty, []).kind == "impossible"


def test_parse_script() -> None:
    script = parse_script("click:0,click:3,press_key:return,done")
    assert script == [
        {"kind": "click_element", "element_index": 0},
        {"kind": "click_element", "element_index": 3},
        {"kind": "press_key", "key": "return"},
        {"kind": "done"},
    ]


def test_describe_action_uses_the_candidate_label(observation: Observation) -> None:
    candidate = observation.targets[0]
    decision = Decision(kind="click_element", element_id=candidate["id"], confidence=0.9)
    assert candidate["description"] in describe_action(decision, observation)
    assert "0.90" in describe_action(decision, observation)
