"""Tests for typed extraction — asking Jev questions about a page."""

from __future__ import annotations

import pytest

from jev_use import extract
from jev_use.extract import ExtractError, ask

PAGE = """
Overview
TOTAL TOKENS
97.6M tokens
Current billing month
TOTAL RUNS
251 runs
USAGE LIMITS
5-hour limit 19% resets in 4h 4m
Weekly limit 8%
Monthly limit 4%
"""


class FakeAnswer:
    def __init__(self, **fields) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.model = "jev-fake-1.13.0"


class FakeClient:
    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.seen: dict | None = None

    def system_one(self, state, questions):
        self.seen = questions
        return FakeResponse(self.answers)


def test_choice_answer_is_normalised() -> None:
    client = FakeClient(
        {"plan": FakeAnswer(choice="goat", confidence=0.91, probabilities={"goat": 0.91, "free": 0.09})}
    )
    result = ask(PAGE, {"plan": {"type": "choice", "instructions": "which plan?",
                                 "criteria": {"goat": "paid", "free": "free"}}}, client=client)
    answer = result["answers"]["plan"]
    assert answer["value"] == "goat"
    assert answer["type"] == "choice"
    assert answer["confidence"] == 0.91
    assert answer["probabilities"]["free"] == 0.09


def test_score_answer_carries_the_legend() -> None:
    client = FakeClient(
        {"tokens": FakeAnswer(score=1.035, confidence=0.84, legend={"0": "none", "1": "some"})}
    )
    result = ask(PAGE, {"tokens": {"type": "score", "instructions": "how many tokens?",
                                   "criteria": ["none", "some"]}}, client=client)
    assert result["answers"]["tokens"]["value"] == 1.035
    assert result["answers"]["tokens"]["legend"]["1"] == "some"


def test_noul_answer() -> None:
    client = FakeClient({"logged_out": FakeAnswer(noul=0.02)})
    result = ask(PAGE, {"logged_out": {"type": "noul", "instructions": "logged out?"}},
                 client=client)
    assert result["answers"]["logged_out"]["value"] == 0.02
    assert "confidence" not in result["answers"]["logged_out"]


def test_all_questions_go_in_one_call() -> None:
    """Parallel evaluation is the point: six questions cost about one round trip."""
    client = FakeClient(
        {
            "a": FakeAnswer(noul=0.9),
            "b": FakeAnswer(noul=0.1),
            "c": FakeAnswer(noul=0.5),
        }
    )
    ask(PAGE, {k: {"type": "noul", "instructions": f"q {k}"} for k in "abc"}, client=client)
    assert set(client.seen) == {"a", "b", "c"}


def test_the_request_is_sent_as_text_not_an_image() -> None:
    client = FakeClient({"q": FakeAnswer(noul=0.5)})
    ask(PAGE, {"q": {"type": "noul", "instructions": "?"}}, client=client)
    assert client.seen is not None


# -- refusals ---------------------------------------------------------------


def test_no_questions_is_an_error() -> None:
    with pytest.raises(ExtractError):
        ask(PAGE, {})


def test_empty_text_is_an_error() -> None:
    with pytest.raises(ExtractError):
        ask("   ", {"q": {"type": "noul", "instructions": "?"}})


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ExtractError) as excinfo:
        ask(PAGE, {"q": {"type": "vibe", "instructions": "?"}})
    assert "choice, score or noul" in str(excinfo.value)


def test_missing_instructions_is_rejected() -> None:
    with pytest.raises(ExtractError):
        ask(PAGE, {"q": {"type": "noul"}})


def test_choice_needs_an_object_of_criteria() -> None:
    with pytest.raises(ExtractError) as excinfo:
        ask(PAGE, {"q": {"type": "choice", "instructions": "?", "criteria": ["a", "b"]}})
    assert "object" in str(excinfo.value)


def test_score_needs_ordered_levels() -> None:
    with pytest.raises(ExtractError):
        ask(PAGE, {"q": {"type": "score", "instructions": "?", "criteria": ["only one"]}})


def test_provider_failure_becomes_an_extract_error() -> None:
    class Boom:
        def system_one(self, state, questions):
            raise RuntimeError("connection reset")

    with pytest.raises(ExtractError) as excinfo:
        ask(PAGE, {"q": {"type": "noul", "instructions": "?"}}, client=Boom())
    assert "connection reset" in str(excinfo.value)


def test_a_missing_answer_is_reported_not_crashed() -> None:
    class Partial:
        def system_one(self, state, questions):
            return FakeResponse({})

    result = ask(PAGE, {"q": {"type": "noul", "instructions": "?"}}, client=Partial())
    assert result["answers"]["q"] == {"error": "no answer returned"}


def test_state_is_capped() -> None:
    client = FakeClient({"q": FakeAnswer(noul=0.5)})
    result = ask("x" * 100_000, {"q": {"type": "noul", "instructions": "?"}},
                 client=client, max_chars=1000)
    assert result["chars_asked"] == 1000


def test_default_model_is_pinned() -> None:
    assert extract.DEFAULT_MODEL == "jev-1.13.0"
