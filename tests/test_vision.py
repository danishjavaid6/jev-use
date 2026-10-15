"""Typed image contracts; no external request or browser needed."""
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from jev_use import mcp_server, vision


def test_vision_does_not_capture_an_unowned_profile(monkeypatch):
    monkeypatch.setattr(mcp_server.facebook_runs, "status", lambda _: {"profiles": [{"id": "allowed"}]})
    monkeypatch.setitem(mcp_server._GOLOGIN, "session", SimpleNamespace(profile=SimpleNamespace(id="other")))
    monkeypatch.setattr(mcp_server, "tool_browser_script", lambda _: pytest.fail("must not capture"))
    assert json.loads(mcp_server.tool_browser_facebook_vision({"run_id": "run"}))["reason"] == "no_owned_profile_in_run"


def test_capture_is_read_only_and_provider_failures_are_sanitized(monkeypatch):
    monkeypatch.setattr(mcp_server.facebook_runs, "status", lambda _: {"profiles": [{"id": "allowed"}]})
    monkeypatch.setitem(mcp_server._GOLOGIN, "session", SimpleNamespace(profile=SimpleNamespace(id="allowed"), port=9222))
    requests = []
    monkeypatch.setattr(mcp_server, "tool_browser_script", lambda args: requests.append(args) or 'port=9222\n{"result":{"path":"trusted.png"}}')
    def fail(_):
        raise RuntimeError("PRIVATE_API_KEY_AND_IMAGE_BYTES")
    monkeypatch.setattr(vision, "diagnose_file", fail)
    result = mcp_server.tool_browser_facebook_vision({"run_id": "run"})
    assert json.loads(result)["reason"] == "vision_unavailable"
    assert "PRIVATE" not in result
    assert requests[0]["dismiss_overlays"] is False
    assert "click" not in requests[0]["code"]
    assert requests[0]["checkpoint_scope"] == "gologin:allowed"


@pytest.fixture
def png():
    image = pytest.importorskip("PIL.Image")
    out = BytesIO()
    image.new("RGB", (640, 400), "blue").save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("size", [(100, 100), (2400, 1600), (2000, 100), (100, 2000)])
def test_prepare_image_resizes_and_pads_to_api_limits(size):
    image = pytest.importorskip("PIL.Image")
    data = BytesIO()
    image.new("RGB", size, "white").save(data, format="PNG")
    prepared = image.open(BytesIO(vision.prepare_image(data.getvalue())))
    w, h = prepared.size
    assert 256 <= w <= 1568 and 256 <= h <= 1568
    assert 78400 <= w * h <= 2458624


@pytest.mark.parametrize("probability", [0.01, 0.99])
def test_batched_image_questions_use_header_pinned_model_and_presence_gate(png, probability):
    sdk = pytest.importorskip("typesafe_sdk")
    if not hasattr(sdk, "Box"):
        pytest.skip("image-capable SDK optional")
    requests = []
    box = {"type": "box", "box": {"top": 0.1, "bottom": 0.2, "left": 0.3, "right": 0.4}, "confidence": 0.99}
    def system_one(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(model_dump=lambda **_: {"model": "jev-1.14.0", "answers": {
            "screen_kind": {"choice": "password_form", "confidence": 0.98},
            "dismissible_popup": {"noul": probability}, "close_box": box}})
    result = vision.diagnose(png, client=SimpleNamespace(system_one=system_one))
    assert requests[0]["extra_headers"] == {"TypeSafe-Danger-Zone-Images": "true"}
    assert requests[0]["model"] == "jev-1.14.0"
    assert set(requests[0]["questions"]) == {"screen_kind", "dismissible_popup", "close_box"}
    assert result["possible_close_box"] == (box if probability > 0.5 else None)
    assert result["actionable"] is False
