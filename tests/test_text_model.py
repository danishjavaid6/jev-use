"""Tests for the text model: the planner and the writer Jev cannot be."""

from __future__ import annotations

import pytest

from jev_use import text_model
from jev_use.text_model import TextModel, TextUnavailable


def scripted(replies: list[str], model: str = "test-model") -> TextModel:
    """A TextModel whose single network call returns canned replies in order."""
    client = TextModel(model=model, api_key="x")
    queue = list(replies)

    def fake_chat(_system: str, _user: str) -> str:
        if not queue:
            raise TextUnavailable("ran out of scripted replies")
        return queue.pop(0)

    client._chat = fake_chat  # type: ignore[method-assign]
    return client


# -- availability -----------------------------------------------------------


def test_unavailable_without_a_model() -> None:
    client = TextModel(model="", base_url="http://x", api_key="")
    assert client.available is False


def test_chat_refuses_clearly_when_unconfigured() -> None:
    client = TextModel(model="", base_url="http://x", api_key="")
    with pytest.raises(TextUnavailable) as excinfo:
        client._chat("s", "u")
    assert "JEV_USE_TEXT_MODEL" in str(excinfo.value)


def test_decompose_fails_open_to_the_original_goal() -> None:
    """A missing planner must not block a task that could work without one."""
    client = TextModel(model="")
    assert client.decompose("open the usage page") == ["open the usage page"]


def test_write_refuses_when_unconfigured() -> None:
    assert TextModel(model="").write("goal", "email field") is None


# -- decompose --------------------------------------------------------------


def test_decompose_parses_a_plain_array() -> None:
    client = scripted(['["press 7", "press multiply", "press 8"]'])
    assert client.decompose("compute 7 times 8") == ["press 7", "press multiply", "press 8"]


def test_decompose_parses_a_fenced_reply() -> None:
    client = scripted(['```json\n["open the page", "read the total"]\n```'])
    assert client.decompose("tell me the total") == ["open the page", "read the total"]


def test_decompose_parses_a_chatty_reply() -> None:
    client = scripted(['Sure! Here are the steps:\n["a", "b"]\nHope that helps.'])
    assert client.decompose("goal") == ["a", "b"]


def test_decompose_collapses_a_single_step_to_the_original_goal() -> None:
    """One step is a wasted round trip and identical to no plan."""
    client = scripted(['["open the page"]'])
    assert client.decompose("open the page") == ["open the page"]


def test_decompose_caps_the_plan() -> None:
    client = scripted([str([f"step {i}" for i in range(50)]).replace("'", '"')])
    assert len(client.decompose("big goal")) == text_model.MAX_SUBGOALS


def test_decompose_survives_a_bad_reply() -> None:
    client = scripted(["I cannot help with that."])
    assert client.decompose("goal") == ["goal"]


def test_decompose_survives_a_network_failure() -> None:
    def boom(_system: str, _user: str) -> str:
        raise TextUnavailable("unreachable")

    client = TextModel(model="m")
    client._chat = boom  # type: ignore[method-assign]
    assert client.decompose("goal") == ["goal"]


# -- write ------------------------------------------------------------------


def test_write_returns_the_string() -> None:
    client = scripted(['{"text": "zurich"}'])
    assert client.write("search a flight", 'input field "Where from?"') == "zurich"


def test_write_honours_a_refusal() -> None:
    """The writer is allowed to decline — passwords, cards, unknown values."""
    client = scripted(['{"text": null}'])
    assert client.write("log in", 'input field "Password"') is None


def test_write_rejects_an_empty_string() -> None:
    client = scripted(['{"text": "   "}'])
    assert client.write("goal", "field") is None


def test_write_survives_a_bad_reply() -> None:
    client = scripted(["I would rather not."])
    assert client.write("goal", "field") is None


def test_write_accepts_a_fenced_reply() -> None:
    client = scripted(['```json\n{"text": "london"}\n```'])
    assert client.write("goal", "field") == "london"


# -- parsing helpers --------------------------------------------------------


def test_extract_json_raises_when_absent() -> None:
    with pytest.raises(TextUnavailable):
        text_model._extract_json("nothing here", "[", "]")


def test_parse_string_array_rejects_an_object() -> None:
    with pytest.raises(TextUnavailable):
        text_model._parse_string_array('{"a": 1}')


def test_parse_text_object_rejects_an_array() -> None:
    with pytest.raises(TextUnavailable):
        text_model._parse_text_object('["a"]')
