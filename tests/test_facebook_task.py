"""Credential-free end-to-end controller replays, including lost responses."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from jev_use import facebook_runs as runs, facebook_task as task, gologin, mcp_server


class Browser:
    def __init__(self):
        self.session = None
        self.records = {}
        self.events = []
        self.cards = [{"name": "Alice", "publicAccountIds": ["123"]},
                      {"name": "Bob", "publicAccountIds": ["456"]}]
        self.fail = None
        self.reject = False
        self.status_override = {}

    def open(self, profile_id, run_id):
        runs.guard_profile(run_id, profile_id)
        self.session = SimpleNamespace(profile=SimpleNamespace(id=profile_id), port=9222)
        runs.record_profile(run_id, profile_id, 9222, "opened")
        self.events.append((profile_id, "open"))

    def close(self):
        self.events.append((self.session.profile.id, "close"))
        self.session = None

    def action(self, args):
        profile = self.session.profile.id
        action, account, run_id = (args[k] for k in ("action", "account", "run_id"))
        token = runs.begin_action(run_id, profile, account, action, args)
        key = (profile, run_id, account)
        record = self.records.setdefault(key, {"stage": "started"})
        self.events.append((profile, action, account))
        if self.fail == action:
            runs.finish_action(run_id, token, False, record)
            raise RuntimeError("SENSITIVE_BACKEND_PAYLOAD")
        if action == "login":
            assert args.get("password")
            record.update(stage="signed_in", accountId=args.get("account_id"))
        stages = {"wait_home": "home_ready", "open_form": "form_open", "wait_form": "form_ready",
                  "fill": "submission_reserved", "create": "submitting", "confirm": "created", "logout": "logged_out"}
        if action in stages:
            record["stage"] = stages[action]
        if action == "fill":
            record["pageName"] = args["page_name"]
        if action == "browse":
            record["agentBrowseMs"] = record.get("agentBrowseMs", 0) + 60000
            if record["agentBrowseMs"] >= 180000:
                record["stage"] = "browsed"
        if action == "confirm":
            record["confirmation"] = {"text": "Page was created"}
        if action == "home":
            record["agentHome"] = True
        if action == "browse_after":
            record["postCreateBrowsed"] = True
        if action == "correct_password":
            self.reject = False
            record.update(stage="signed_in", accountId="123")
        next_action = {"started": "login", "signed_in": "wait_home", "home_ready": "browse",
                       "browsed": "open_form", "form_open": "wait_form", "form_ready": "fill",
                       "submission_reserved": "create", "submitting": "confirm", "created": "home",
                       "logged_out": "accounts"}[record["stage"]]
        if record["stage"] == "created" and record.get("agentHome"):
            next_action = "logout" if record.get("postCreateBrowsed") else "browse_after"
        result = {**record, "next_action": next_action,
                  "current_state": {"kind": "account_chooser" if record["stage"] in {"started", "logged_out"} else "signed_in"},
                  "live_login": {"passwordRejected": self.reject}}
        if action == "status":
            result.update(self.status_override)
        if action == "accounts":
            result = {"accounts": [c["name"] for c in self.cards], "cards": self.cards}
        # Mimic bridge's atomic commit before returning or losing the response.
        journal = {json.dumps([r, a]): c for (p, r, a), c in self.records.items() if p == profile}
        digest = hashlib.sha256(("gologin:" + profile).encode()).hexdigest()
        (runs.checkpoint_dir() / (digest + ".json")).write_text(json.dumps(journal), encoding="utf-8")
        runs.finish_action(run_id, token, True, record, self.cards if action == "accounts" else None)
        return "port=9222\n" + json.dumps({"ok": True, "result": result})

    def step(self, **args):
        return task.advance({"run_id": "batch", **args}, owner=lambda: self.session,
                            open_profile=self.open, close_profile=self.close, action=self.action)


@pytest.fixture
def browser(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_USE_WORKFLOW_DIR", str(tmp_path))
    task._PASSWORDS.clear()
    profiles = [gologin.GoLoginProfile("id-23", "Profile 23"), gologin.GoLoginProfile("id-29", "Profile 29")]
    runs.start("batch", None, None, profiles, profile_numbers=[29, 23])
    return Browser()


def test_full_multi_profile_run_requires_only_repeated_next_calls_and_requested_inputs(browser):
    reply = browser.step(password="PRIVATE_RUNTIME_SECRET")
    for _ in range(100):
        if reply["status"] == "completed":
            break
        if reply["status"] == "needs_input":
            assert reply["required"] == ["page_name", "category"]
            reply = browser.step(page_name="Requested Page " + reply["account"], category="Artist")
        else:
            assert reply["status"] == "continue", reply
            assert reply["next_call"]["arguments"] == {"run_id": "batch"}
            reply = browser.step()
    assert reply["status"] == "completed"
    assert reply["profiles_completed"] == 2
    assert reply["progress"]["accounts_completed"] == 4
    assert len(reply["results"]) == 4
    assert [e for e in browser.events if e[1] == "open"] == [("id-29", "open"), ("id-23", "open")]
    assert len([e for e in browser.events if e[1] == "create"]) == 4
    assert len([e for e in browser.events if e[1] == "logout"]) == 4
    assert len([e for e in browser.events if e[1] == "browse"]) == 12
    assert browser.session is None
    assert "PRIVATE_RUNTIME_SECRET" not in json.dumps(runs.status())
    assert not task._PASSWORDS


def ready(browser):
    assert browser.step()["performed"] == "open_profile"
    assert browser.step()["performed"] == "inventory"


def test_password_survives_calls_but_not_restart_pause_or_expiry(browser, monkeypatch):
    ready(browser)
    assert browser.step()["required"] == ["password"]
    assert browser.step(password="PRIVATE")["performed"] == "login"
    task.forget("batch")  # New MCP process has no credentials.
    browser.records.clear()
    assert browser.step()["required"] == ["password"]
    task._password("batch", "PRIVATE")
    task._PASSWORDS["batch"] = ("PRIVATE", -10000)
    assert task._password("batch") is None
    task._password("batch", "PRIVATE")
    runs.transition("batch", "pause")
    assert browser.step()["status"] == "paused"
    assert not task._PASSWORDS


def test_wrong_owned_profile_pending_action_and_pause_never_act(browser):
    browser.session = SimpleNamespace(profile=SimpleNamespace(id="id-30"), port=9222)
    assert browser.step()["status"] == "blocked"
    assert browser.events == []
    browser.session = None
    ready(browser)
    runs.begin_action("batch", "id-29", "Alice", "status")
    count = len(browser.events)
    assert browser.step()["status"] == "needs_resume"
    assert len(browser.events) == count


def test_repeated_failure_stops_without_dumping_backend_or_looping_forever(browser):
    ready(browser)
    browser.step(password="PRIVATE")
    browser.fail = "wait_home"
    assert browser.step()["outcome"] == "requires_fresh_inspection"
    assert browser.step()["outcome"] == "requires_fresh_inspection"
    result = browser.step()
    assert result["reason"] == "repeated_action_failure"
    assert len([e for e in browser.events if e[1] == "wait_home"]) == 2
    assert "SENSITIVE" not in json.dumps(result)


def test_rejected_password_needs_explicit_correction_and_is_not_replayed(browser):
    ready(browser)
    browser.reject = True
    assert browser.step(password="OLD")["required"] == ["corrected_password"]
    assert not any(e[1] == "login" for e in browser.events)
    assert browser.step(corrected_password="NEW")["performed"] == "correct_password"
    assert task._password("batch") == "NEW"
    assert "NEW" not in json.dumps(runs.status())


def test_lost_create_response_resumes_confirmation_without_second_create(browser):
    ready(browser)
    browser.step(password="PRIVATE")
    while True:
        result = browser.step()
        if result["status"] == "needs_input":
            break
    browser.step(page_name="Stable", category="Artist")
    browser.step()  # sole create, journal committed
    task.forget("batch")
    assert browser.step()["performed"] == "confirm"
    assert len([e for e in browser.events if e[1] == "create"]) == 1


@pytest.mark.parametrize("observation,reason", [
    ({"current_state": {"accountMismatch": True}}, "identity_challenge_or_unknown_state"),
    ({"next_action": "inspect"}, "identity_challenge_or_unknown_state"),
    ({"loginSubmitted": True}, "login_submitted_identity_unconfirmed"),
    ({"stage": "logged_out", "confirmation": None}, "completion_evidence_missing"),
])
def test_uncertain_states_never_authorize_a_mutation(browser, observation, reason):
    ready(browser)
    browser.status_override = observation
    count = len(browser.events)
    assert browser.step(password="PRIVATE")["reason"] == reason
    assert all(e[1] == "status" for e in browser.events[count:])


def test_mcp_registration_and_pause_forgets_runtime_credentials(browser, monkeypatch):
    assert task.SCHEMA in mcp_server.TOOLS
    assert mcp_server.HANDLERS[task.SCHEMA["name"]] == mcp_server.tool_browser_facebook_next
    task._password("batch", "PRIVATE")
    mcp_server.tool_browser_facebook_run({"action": "pause", "run_id": "batch"})
    assert task._password("batch") is None


def test_concurrent_controller_calls_cannot_start_duplicate_actions(browser):
    with task._LOCK:
        assert browser.step()["reason"] == "another_controller_call_is_running"
    assert browser.events == []


def test_verified_historical_creation_does_not_complete_a_new_request(browser):
    ready(browser)
    browser.records[("id-29", "older-run", "Alice")] = {
        "stage": "logged_out", "accountId": "123", "confirmation": {"text": "Already created"}}
    browser.action({"run_id": "batch", "account": "Alice", "port": 9222, "action": "status"})
    result = browser.step(password="PRIVATE")
    assert result["account"] == "Alice" and result["performed"] == "login"
    assert result["progress"]["accounts_completed"] == 0
    assert result["history"]["previous_pages_created"] == 1
    assert browser.records[("id-29", "older-run", "Alice")]["confirmation"]["text"] == "Already created"


def test_old_completed_account_never_hides_a_later_uncertain_submission():
    profile = {"checkpoints": [
        {"run_id": "old", "account": "Alice", "accountId": "123", "completed": True, "stage": "logged_out"},
        {"run_id": "later", "account": "Alice", "accountId": "123", "completed": False, "stage": "submitting"}]}
    assert task._completion(profile, {"account": "Alice", "account_id": "123"}, "later") is None
    assert task._completion(profile, {"account": "Alice", "account_id": "123"}, "old")["completed"]
    assert not task._matches(profile["checkpoints"][0], {"account": "Alice", "account_id": "different"})


def test_controller_completion_marker_without_journal_evidence_is_not_success(browser):
    for profile in ["id-29", "id-23"]:
        runs.task_progress("batch", profile, completed=True)
    assert browser.step()["reason"] == "completion_journals_missing_or_inconsistent"
    assert browser.events == []


def test_previous_login_submission_does_not_block_verified_precreation_reauthentication(browser):
    ready(browser)
    browser.step(password="PRIVATE")
    browser.status_override = {"stage": "home_ready", "next_action": "login", "loginSubmitted": True,
                               "current_state": {"kind": "account_chooser"},
                               "live_login": {"chooserVisible": True, "identityPresent": False}}
    assert browser.step()["performed"] == "login"
    browser.status_override["loginRecoveries"] = [{"at": 1}]
    assert browser.step()["reason"] == "login_submitted_identity_unconfirmed"


def test_failed_fill_reuses_durable_page_inputs_without_model_memory(browser):
    ready(browser)
    browser.step(password="PRIVATE")
    for _ in range(10):
        if browser.step()["status"] == "needs_input":
            break
    browser.fail = "fill"
    assert browser.step(page_name="Keep this Page", category="Artist")["outcome"] == "requires_fresh_inspection"
    browser.fail = None
    assert browser.step()["performed"] == "fill"
    assert browser.records[("id-29", "batch", "Alice")]["pageName"] == "Keep this Page"


def test_preparing_one_owned_tab_is_a_bounded_step_before_any_login(browser):
    assert browser.step()["performed"] == "open_profile"
    calls = []
    def prepare(run_id, owner):
        calls.append((run_id, owner.profile.id))
        return True
    result = task.advance({"run_id": "batch"}, owner=lambda: browser.session, open_profile=browser.open,
                          close_profile=browser.close, action=browser.action, prepare_profile=prepare)
    assert result["performed"] == "prepare_workflow_tab"
    assert calls == [("batch", "id-29")]
    assert browser.events == [("id-29", "open")]


def finish_job(browser, run_id):
    result = browser.step(run_id=run_id, password="PRIVATE")
    for _ in range(100):
        if result["status"] == "completed":
            return result
        assert result["status"] in {"continue", "needs_input"}, result
        fields = {"page_name": run_id + " " + result["account"], "category": "Artist"} if result["status"] == "needs_input" else {}
        result = browser.step(run_id=run_id, **fields)
    pytest.fail("Workflow did not complete within bounded replay")


def test_two_requests_create_multiple_pages_per_account_but_resume_never_creates_extras(browser):
    first = finish_job(browser, "batch")
    assert first["progress"]["accounts_completed"] == 4
    original = json.loads(json.dumps({str(k): v for k, v in browser.records.items()}))
    events_before = len(browser.events)
    profiles = [gologin.GoLoginProfile("id-29", "Profile 29")]
    runs.start("additional", 29, 29, profiles)
    second = finish_job(browser, "additional")
    assert second["progress"]["accounts_completed"] == 2
    assert {r["source_run"] for r in second["results"]} == {"additional"}
    assert second["history"]["previous_pages_created"] == 2
    assert {e[0] for e in browser.events[events_before:]} == {"id-29"}
    assert len([e for e in browser.events if e[1] == "create"]) == 6
    assert all(browser.records[k] == original[str(k)] for k in browser.records if k[1] == "batch")
    assert browser.records[("id-29", "batch", "Alice")]["pageName"] != browser.records[("id-29", "additional", "Alice")]["pageName"]
    runs.start("additional", 29, 29, profiles)  # Same request, not another Page.
    runs.transition("additional", "pause")
    runs.transition("additional", "resume")
    before = len(browser.events)
    assert browser.step(run_id="additional")["status"] == "completed"
    assert len(browser.events) == before


@pytest.mark.parametrize("stage", ["started", "form_ready", "submission_reserved", "submitting", "created"])
def test_new_request_preserves_all_old_attempts_without_retrying_them(browser, stage):
    ready(browser)
    old = {"stage": stage, "accountId": "123", "pageName": "Older Page"}
    browser.records[("id-29", "old", "Alice")] = old.copy()
    browser.action({"run_id": "batch", "account": "Alice", "port": 9222, "action": "status"})
    result = browser.step(password="PRIVATE")
    assert result["performed"] == "login" and result["account"] == "Alice"
    assert result["history"]["previous_unresolved_creations"] == int(stage in {"submission_reserved", "submitting", "created"})
    assert browser.records[("id-29", "old", "Alice")] == old
    assert not any(e[1] in {"create", "confirm"} for e in browser.events)
