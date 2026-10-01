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


def test_run_script_attaches_explicitly_and_disables_daemon(monkeypatch):
    monkeypatch.setattr(betterwright, "ws_url_for", lambda port: "ws://127.0.0.1:9222/devtools/browser/test")
    calls = {}

    class Completed:
        returncode = 0
        stdout = '{"ok": true, "result": "done"}'
        stderr = ""

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(betterwright.subprocess, "run", fake_run)

    result = betterwright.run_script(9222, "return page.url()", command="betterwright")

    assert result["ok"] is True
    assert calls["argv"] == [
        "betterwright",
        "run",
        "--no-daemon",
        "--browser",
        "ws://127.0.0.1:9222/devtools/browser/test",
        "--no-ad-block",
        "-c",
        "return page.url()",
    ]
    assert calls["kwargs"]["env"]["BETTERWRIGHT_NO_DAEMON"] == "1"


def test_run_script_rejects_empty_code():
    with pytest.raises(betterwright.BetterWrightError, match="non-empty"):
        betterwright.run_script(9222, "", command="betterwright")


def test_environment_for_adds_a_shim_directory_to_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/system/bin")
    command = tmp_path / "betterwright.cmd"

    env = betterwright._environment_for(str(command))

    assert env["PATH"].startswith(str(tmp_path))
    assert "/system/bin" in env["PATH"]
