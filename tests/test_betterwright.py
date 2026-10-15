import json

import pytest

from jev_use import betterwright


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test"}).encode()


def test_pnpm_home_discovery_without_inherited_path(monkeypatch, tmp_path):
    monkeypatch.setattr(betterwright.shutil, "which", lambda name: None)
    monkeypatch.setenv("PNPM_HOME", str(tmp_path))
    cli = tmp_path / "betterwright.cmd"
    cli.write_text("fixture")
    assert betterwright.executable() == str(cli)


def test_ws_url_for_resolves_the_browser_endpoint(monkeypatch):
    seen = []

    def fake_urlopen(url, timeout):
        seen.append((url, timeout))
        return _Response()

    monkeypatch.setattr(betterwright.urllib.request, "urlopen", fake_urlopen)

    assert betterwright.ws_url_for(9222) == "ws://127.0.0.1:9222/devtools/browser/test"
    assert seen == [("http://127.0.0.1:9222/json/version", 5.0)]


def test_run_script_reuses_connection_and_sends_code_over_stdin(monkeypatch):
    monkeypatch.setattr(betterwright, "ws_url_for", lambda port: "ws://127.0.0.1:9222/devtools/browser/test")
    class Tabs(_Response):
        def read(self):
            return b'[{"id":"tab1","type":"page","url":"about:blank"}]'
    monkeypatch.setattr(betterwright.urllib.request, "urlopen", lambda *a, **k: Tabs())
    calls = []
    instances = []

    class Bridge:
        def __init__(self, command):
            instances.append(command)
            self.process = type("Process", (), {"poll": lambda self: None})()
        def run(self, request, timeout):
            calls.append((request, timeout))
            return {"ok": True, "result": "done"}
        def close(self):
            pass

    monkeypatch.setattr(betterwright, "_Bridge", Bridge)
    monkeypatch.setattr(betterwright, "_BRIDGES", {})
    for code in ["state.count = 1; return state.count", "return state.count"]:
        assert betterwright.run_script(9222, code, command="betterwright")["ok"]
    assert instances == ["betterwright"]
    assert calls[0][0]["code"].startswith("state.count")
    assert calls[0][0]["ws"].endswith("/test")


@pytest.mark.parametrize("timeout", [0, -1, 901, float("nan"), float("inf")])
def test_invalid_deadline_fails_before_attach(timeout):
    with pytest.raises(betterwright.BetterWrightError, match="timeout"):
        betterwright.run_script(9222, "return 1", timeout=timeout)


def test_run_script_rejects_empty_code():
    with pytest.raises(betterwright.BetterWrightError, match="non-empty"):
        betterwright.run_script(9222, "", command="betterwright")


def test_environment_for_adds_a_shim_directory_to_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/system/bin")
    command = tmp_path / "betterwright.cmd"

    env = betterwright._environment_for(str(command))

    assert env["PATH"].startswith(str(tmp_path))
    assert "/system/bin" in env["PATH"]


def test_duplicate_facebook_tabs_reach_selector_and_pin_exact_target(monkeypatch):
    monkeypatch.setattr(betterwright, "ws_url_for", lambda port: "ws://127.0.0.1:9222/browser")
    monkeypatch.setattr(betterwright, "list_tabs", lambda port: [
        {"id": "one", "url": "https://www.facebook.com/"},
        {"id": "two", "url": "https://www.facebook.com/"}])
    calls = []
    class Bridge:
        def __init__(self, command):
            self.process = type("Process", (), {"poll": lambda self: None})()
        def run(self, request, timeout):
            calls.append(request)
            return {"ok": True, "workflowAttachment": {"targetId": "two"}}
        def close(self):
            pass
    monkeypatch.setattr(betterwright, "_Bridge", Bridge)
    monkeypatch.setattr(betterwright, "_BRIDGES", {})
    monkeypatch.setattr(betterwright, "_WORKFLOW_TABS", {})
    betterwright.run_script(9222, "return 1", command="fixture", facebook_workflow=True)
    assert [t["id"] for t in calls[0]["targets"]] == ["one", "two"]
    betterwright.close_sessions()
    betterwright.run_script(9222, "return 1", command="fixture", facebook_workflow=True)
    assert calls[1]["target_id"] == "two"
    assert len(calls[1]["targets"]) == 1
    with pytest.raises(betterwright.BetterWrightError):
        betterwright.run_script(9222, "return 1", command="fixture", page_target_id="gone", facebook_workflow=True)


def test_dedicated_workflow_tab_ignores_duplicate_and_stale_legacy_tabs(monkeypatch):
    monkeypatch.setattr(betterwright, "ws_url_for", lambda port: "ws://127.0.0.1:9222/browser")
    monkeypatch.setattr(betterwright, "list_tabs", lambda port: [
        {"id": "old", "url": "https://www.facebook.com/"},
        {"id": "duplicate", "url": "https://www.facebook.com/"}])
    monkeypatch.setattr(betterwright, "_WORKFLOW_TABS", {9222: ("ws://127.0.0.1:9222/browser", "old")})
    closed = []
    monkeypatch.setattr(betterwright, "_BRIDGES", {9222: ("unused", type("Bridge", (), {"close": lambda self: closed.append(True)})())})
    requests = []
    class Created(_Response):
        def read(self):
            return b'{"id":"dedicated","type":"page","url":"https://www.facebook.com/"}'
    def create(request, timeout):
        requests.append(request)
        return Created()
    monkeypatch.setattr(betterwright.urllib.request, "urlopen", create)
    result = betterwright.workflow_tab(9222, create=True)
    assert result["created"] and result["target_id"] == "dedicated"
    assert requests[0].method == "PUT"
    assert closed == [True]
    assert betterwright._WORKFLOW_TABS[9222][1] == "dedicated"


def test_dedicated_tab_survives_new_worker_but_lost_creation_tab_never_gets_replaced(monkeypatch):
    ws = "ws://127.0.0.1:9222/browser"
    monkeypatch.setattr(betterwright, "ws_url_for", lambda port: ws)
    monkeypatch.setattr(betterwright, "_WORKFLOW_TABS", {})
    monkeypatch.setattr(betterwright, "_BRIDGES", {})
    monkeypatch.setattr(betterwright, "list_tabs", lambda port: [{"id": "task"}])
    monkeypatch.setattr(betterwright.urllib.request, "urlopen", lambda *a, **k: pytest.fail("must not create"))
    saved = {"browser_id": betterwright.hashlib.sha256(ws.encode()).hexdigest(), "target_id": "task"}
    assert betterwright.workflow_tab(9222, saved, create=True)["created"] is False
    monkeypatch.setattr(betterwright, "list_tabs", lambda port: [])
    with pytest.raises(betterwright.BetterWrightError, match="creation_tab_unavailable"):
        betterwright.workflow_tab(9222, saved, create=True, preserve_creation=True)
    with pytest.raises(betterwright.BetterWrightError, match="workflow_tab_unavailable"):
        betterwright.workflow_tab(9222, saved)
