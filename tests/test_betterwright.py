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
