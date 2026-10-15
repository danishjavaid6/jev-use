import json
import pytest
from types import SimpleNamespace
from jev_use import facebook_pages, facebook_runs, mcp_server, betterwright, gologin

BASE = {"port": 9222, "run_id": "run", "account": "account"}


@pytest.fixture(autouse=True)
def scoped_run(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_USE_WORKFLOW_DIR", str(tmp_path))
    profile = gologin.GoLoginProfile("p23", "Profile 23")
    facebook_runs.start("run", 23, 23, [profile])
    token = facebook_runs.begin_action("run", "p23", "account", "status")
    facebook_runs.finish_action("run", token, True)
    monkeypatch.setitem(mcp_server._GOLOGIN, "session", SimpleNamespace(profile=profile, port=9222))
    monkeypatch.setattr(mcp_server, "_FACEBOOK_RUN_ID", None)


def test_tabs_inventory_is_read_only_and_omits_url_secrets(monkeypatch):
    monkeypatch.setattr(betterwright, "list_tabs", lambda port: [{"id":"tab1", "url":"https://www.facebook.com/?token=private#secret", "title":"private"}])
    monkeypatch.setattr(mcp_server, "tool_browser_script", lambda args: pytest.fail("must not execute browser actions"))
    result = json.loads(mcp_server.tool_browser_facebook({**BASE, "action":"tabs"}))
    assert result == {"tabs":[{"target_id":"tab1", "url":"https://www.facebook.com/"}]}


def test_explicit_restart_requires_login_verified_id_and_different_run():
    valid = {**BASE, "action": "login", "account_id": "verified", "restart_from_run": "old"}
    assert '"restart_from_run": "old"' in facebook_pages.request(valid)["code"]
    for changes in ({"action": "status"}, {"account_id": ""}, {"restart_from_run": "run"}, {"restart_from_run": ""}):
        with pytest.raises(ValueError):
            facebook_pages.request({**valid, **changes})


@pytest.mark.parametrize("args", [
    {"action": "bogus"}, {"action": "browse", "seconds": 61},
    {"action": "browse", "seconds": True}, {"action": "fill"},
    {"action": "status", "password": "secret"}, {"action": "status", "code": "evil()"},
])
def test_invalid_actions_fail_before_browser_access(args):
    with pytest.raises(ValueError):
        facebook_pages.request({**BASE, **args})


def test_data_cannot_become_executable_code():
    name = '\"); throw Error("oops"); //'
    request = facebook_pages.request({**BASE, "action": "fill", "page_name": name, "category": "Artist"})
    payload = request["code"].removeprefix("return await facebookAgent.step(").removesuffix(");")
    assert json.loads(payload)["page_name"] == name


def test_browsing_defaults_to_one_minute_without_extending_fixed_waits():
    for action in ("browse", "browse_after"):
        code = facebook_pages.request({**BASE, "action": action})["code"]
        assert '"seconds": 60' in code
    assert '"seconds"' not in facebook_pages.request({**BASE, "action": "wait_home"})["code"]


def test_compact_response_keeps_current_evidence_and_popup_diagnostics():
    payload = {"ok": True, "result": {"stage": "logged_out", "confirmation": {"text": "Page was created"}},
               "browserDialogs": {"inspected": True, "blocked": False},
               "nativeDialogsDismissed": ["Windows.NotificationPermission"], "warnings": ["known warning"],
               "workflowCheckpoints": {"other": {"stage": "created"}}}
    response = facebook_pages.compact_response("port=9222\n" + json.dumps(payload))
    observed = json.loads(response.split("\n", 1)[1])
    assert "workflowCheckpoints" not in observed
    assert observed["result"] == payload["result"]
    assert observed["browserDialogs"] == payload["browserDialogs"]
    assert observed["warnings"] == payload["warnings"]
    assert facebook_pages.compact_response("unknown") == "unknown"


def test_creation_uses_existing_journal_with_identity_verification(monkeypatch):
    captured = []
    monkeypatch.setattr(mcp_server, "tool_browser_script", lambda options: captured.append(options) or "ok")
    assert mcp_server.tool_browser_facebook({**BASE, "action": "create"}) == "ok"
    assert captured[0]["submission"] == {"run_id": "run", "account": "account", "verify_account": True}
    assert "click" not in captured[0]["code"]


@pytest.mark.parametrize("action", ["login", "correct_password"])
def test_login_backend_errors_never_echo_secret(monkeypatch, action):
    def fail(options):
        raise RuntimeError(options["code"])
    monkeypatch.setattr(mcp_server, "tool_browser_script", fail)
    with pytest.raises(betterwright.BetterWrightError) as exc:
        mcp_server.tool_browser_facebook({**BASE, "action": action, "password": "PRIVATE"})
    assert "PRIVATE" not in str(exc.value)


def test_failed_structured_action_omits_unrelated_journals(monkeypatch):
    monkeypatch.setattr(mcp_server.betterwright, "run_script", lambda *args, **kwargs: {
        "ok": False, "error": "button unavailable", "workflowCheckpoints": {"old": {"stage": "created"}},
        "browserDialogs": {"inspected": True, "blocked": False}})
    monkeypatch.setattr(mcp_server, "reset_browser_session", lambda: None)
    monkeypatch.setitem(mcp_server._SESSION, "target", None)
    with pytest.raises(betterwright.BetterWrightError) as exc:
        mcp_server.tool_browser_script({"port": 9222, "code": "return 1;", "facebook_workflow": True})
    payload = json.loads(str(exc.value))
    assert "workflowCheckpoints" not in payload
    assert payload["error"] == "button unavailable"
    assert payload["browserDialogs"]["inspected"]
