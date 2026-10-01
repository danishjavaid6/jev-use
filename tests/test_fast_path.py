"""Regressions from the Windows run: wrong binary, lost port, helper loops."""
import sys
from types import SimpleNamespace

from jev_use import browser, host, mcp_server, tool_cli
from jev_use import profiles
import pytest


def test_explicit_cdp_does_not_scan_windows_processes(monkeypatch):
    def forbidden():
        raise AssertionError("explicit CDP must not shell out to PowerShell")
    monkeypatch.setattr(browser, "running_profiles", forbidden)
    monkeypatch.setattr(browser, "cdp_alive", lambda port: port == 9333)
    target = browser.attach(SimpleNamespace(evaluate=lambda *a: None), port=9333)
    assert target.port == 9333


def test_url_read_uses_port_and_no_action_model(monkeypatch):
    seen = []
    target = SimpleNamespace(port=9333, url="https://example.com")
    def session(profile, body, port):
        assert port == 9333
        return body(object(), target), target
    monkeypatch.setattr(mcp_server, "with_browser_session", session)
    monkeypatch.setattr(mcp_server, "navigate_and_settle", lambda d, t, u, s: seen.append(u))
    monkeypatch.setattr(mcp_server, "browser_read", lambda d, t: "x" * 10000)
    out = mcp_server.tool_browser_read({"port": 9333, "url": target.url})
    assert seen == [target.url]
    assert len(out.split("\n\n")[1]) == 6000
    assert "port=9333" in out


def test_windows_prefers_installed_chrome_to_path_or_registry(monkeypatch, tmp_path):
    binary = tmp_path / host._WINDOWS_CHROME_PATHS[0]
    binary.write_text("test")
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("JEV_USE_BROWSER_BINARY", raising=False)
    monkeypatch.setattr(host.shutil, "which", lambda n: "orbita/chrome.exe")
    monkeypatch.setattr(host, "_windows_chrome_from_registry", lambda: "orbita/chrome.exe")
    assert host.find_browser_binary() == str(binary)


def test_cli_preserves_spaced_arguments_and_port(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jev-use", "browser_read", "--port", "9333", "--profile", "Danish Javaid", "--url", "https://example.com"])
    monkeypatch.setattr(mcp_server, "load_env", lambda: None)
    def read(args):
        assert args == {"profile": "Danish Javaid", "port": 9333, "url": "https://example.com"}
        return "page text"
    monkeypatch.setitem(mcp_server.HANDLERS, "browser_read", read)
    assert tool_cli.main() == 0
    assert capsys.readouterr().out == "page text\n"


def test_browser_exit_is_reported_without_waiting_launch_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "_cdp_alive", lambda port: False)
    monkeypatch.setattr(profiles, "prepare_profile", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(profiles, "browser_binary", lambda: "wrong-orbita.exe")
    monkeypatch.setattr(profiles.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(poll=lambda: 403, returncode=403))
    monkeypatch.setattr(profiles.time, "sleep", lambda *a: pytest.fail("must fail immediately when the process exits"))
    with pytest.raises(RuntimeError, match="wrong-orbita.exe"):
        profiles.start_profile("Default")


def test_background_tab_is_pinned_by_id_without_activation():
    from test_harness import FakeCDP, started
    fake = FakeCDP()
    with started(fake) as connection:
        tab = connection.open_tab("https://two.test/b")
        connection.evaluate("1", url_hint="target:" + tab["id"])
        connection.click_at(10, 20, url_hint="target:" + tab["id"])
    create = next(params for method, params, _ in fake.calls if method == "Target.createTarget")
    assert create["background"] is True
    attach = next(params for method, params, _ in fake.calls if method == "Target.attachToTarget")
    assert attach["targetId"] == "T2"
    assert not any(method in ("Target.activateTarget", "Page.bringToFront") for method, _, _ in fake.calls)
