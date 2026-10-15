"""Offline invariants for bounded jobs and recovery across agent processes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jev_use import facebook_runs as runs, gologin, mcp_server


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_USE_WORKFLOW_DIR", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_FACEBOOK_RUN_ID", None)
    monkeypatch.setitem(mcp_server._GOLOGIN, "session", None)


def profiles():
    return [gologin.GoLoginProfile(f"id-{i}", f"Profile {i}") for i in range(22, 31)]


def observed(account="Alice"):
    token = runs.begin_action("batch", "id-23", account, "status")
    runs.finish_action("batch", token, True, {"stage": "home_ready", "agentBrowseMs": 60000})


def test_exact_range_survives_new_python_process_and_cannot_expand():
    state = runs.start("batch", 23, 29, profiles())
    assert [p["number"] for p in state["profiles"]] == list(range(23, 30))
    code = "from jev_use.facebook_runs import status; import json; print(json.dumps(status()))"
    saved = json.loads(subprocess.check_output([sys.executable, "-c", code], text=True))
    assert saved["profiles"] == state["profiles"]
    for number in (22, 30):
        with pytest.raises(ValueError, match="outside"):
            runs.guard_profile("batch", f"id-{number}")
    with pytest.raises(ValueError, match="immutable"):
        runs.start("batch", 22, 29, profiles())
    assert runs.status()["profile_start"] == 23


def test_user_can_select_one_profile_or_a_noncontiguous_list():
    single = runs.start("single", None, None, profiles(), profile_numbers=[25])
    assert [p["number"] for p in single["profiles"]] == [25]
    custom = runs.start("custom", None, None, profiles(), profile_numbers=[29, 23, 26])
    assert [p["number"] for p in custom["profiles"]] == [29, 23, 26]
    with pytest.raises(ValueError, match="outside"):
        runs.guard_profile("custom", "id-24")  # Between min/max, but not selected.
    assert runs.status("single")["profiles"] == single["profiles"]


def test_named_profile_selection_uses_only_exact_observed_ids(monkeypatch):
    catalog = profiles() + [gologin.GoLoginProfile("marketing-id", "Marketing")]
    monkeypatch.setattr(gologin, "profiles", lambda: catalog)
    state = json.loads(mcp_server.tool_browser_facebook_run({
        "action": "start", "run_id": "named", "profile_ids": ["marketing-id"]}))
    assert [p["name"] for p in state["profiles"]] == ["Marketing"]
    with pytest.raises(ValueError, match="outside"):
        runs.guard_profile("named", "id-23")
    with pytest.raises(ValueError, match="missing or ambiguous"):
        runs.start("missing", None, None, catalog, profile_ids=["unknown"])


@pytest.mark.parametrize("selection", [{}, {"profile_numbers": []}, {"profile_numbers": [True]},
    {"profile_numbers": [23], "profile_ids": ["id-23"]},
    {"profile_numbers": [23], "profile_start": 23, "profile_end": 29}])
def test_selection_is_never_defaulted_or_silently_combined(monkeypatch, selection):
    monkeypatch.setattr(gologin, "profiles", profiles)
    with pytest.raises(ValueError):
        mcp_server.tool_browser_facebook_run({"action": "start", "run_id": "bad", **selection})


def test_new_user_selection_cannot_be_ignored_by_resume():
    runs.start("batch", 23, 29, profiles())
    runs.transition("batch", "pause")
    with pytest.raises(ValueError, match="new profile selection"):
        mcp_server.tool_browser_facebook_run({"action": "resume", "run_id": "batch", "profile_numbers": [25]})
    assert runs.status("batch")["status"] == "paused"


def test_missing_duplicate_or_renamed_profiles_never_fall_back():
    for candidates in (profiles()[1:], profiles() + [gologin.GoLoginProfile("duplicate", "Profile 23")]):
        # First candidate list is missing22, intentionally request22–29.
        with pytest.raises(ValueError, match="missing or ambiguous"):
            runs.start("batch", 22, 29, candidates)
    assert not list(runs.root().glob("*.json"))


def test_inflight_action_survives_stop_and_requires_resume_then_fresh_observation():
    runs.start("batch", 23, 29, profiles())
    observed()
    token = runs.begin_action("batch", "id-23", "Alice", "browse")
    assert runs.status()["pending_action"]["id"] == token
    with pytest.raises(ValueError, match="in flight"):
        runs.begin_action("batch", "id-23", "Alice", "browse")
    runs.transition("batch", "pause")
    with pytest.raises(ValueError, match="paused"):
        runs.guard_profile("batch", "id-23")
    state = runs.transition("batch", "resume")
    assert state["interrupted_action"]["action"] == "browse"
    with pytest.raises(ValueError, match="Inspect current"):
        runs.begin_action("batch", "id-23", "Alice", "browse")
    observed()
    next_token = runs.begin_action("batch", "id-23", "Alice", "browse")
    runs.finish_action("batch", token, True)  # Late old response cannot clear new work.
    assert runs.status()["pending_action"]["id"] == next_token


def test_status_for_one_account_does_not_authorize_another():
    runs.start("batch", 23, 29, profiles())
    observed()
    with pytest.raises(ValueError, match="specific profile/account"):
        runs.begin_action("batch", "id-23", "Bob", "login")


def test_live_journals_remain_authority_for_partial_and_uncertain_actions():
    runs.start("batch", 23, 29, profiles())
    digest = hashlib.sha256(b"gologin:id-23").hexdigest()
    journal = runs.checkpoint_dir() / (digest + ".json")
    values = {
        json.dumps(["older-run", "Alice"]): {"stage": "submitting", "pageName": "Existing Page", "password": "DO_NOT_RETURN"},
        json.dumps(["batch", "Bob"]): {"stage": "home_ready", "agentBrowseMs": 60000,
            "agentBrowsePlan": [{"surface": "home", "elapsedMs": 60000}, {"surface": "reels", "elapsedMs": 0}]},
    }
    journal.write_text(json.dumps(values), encoding="utf-8")
    state = runs.status()
    checkpoints = state["profiles"][0]["checkpoints"]
    assert checkpoints[0]["creation_uncertain"]
    assert checkpoints[0]["next_action"] == "confirm"
    assert checkpoints[1]["agentBrowsePlan"][1]["elapsedMs"] == 0
    assert "DO_NOT_RETURN" not in json.dumps(state)
    assert json.loads(journal.read_text()) == values  # Discovery never rewrites history.


def test_scoped_profile_open_is_rejected_before_launch(monkeypatch):
    runs.start("batch", 23, 29, profiles())
    monkeypatch.setattr(mcp_server, "_FACEBOOK_RUN_ID", "batch")
    monkeypatch.setattr(gologin, "find", lambda _: profiles()[0])  # Profile22.
    monkeypatch.setattr(gologin, "launch", lambda *a, **k: pytest.fail("must not launch outside requested range"))
    with pytest.raises(ValueError, match="outside"):
        mcp_server.tool_browser_open({"vendor": "gologin", "profile": "id-22"})


def test_structured_actions_reject_outside_profile_or_wrong_port_before_browser(monkeypatch):
    runs.start("batch", 23, 29, profiles())
    monkeypatch.setattr(mcp_server, "tool_browser_script", lambda args: pytest.fail("must not touch browser"))
    monkeypatch.setitem(mcp_server._GOLOGIN, "session", SimpleNamespace(profile=profiles()[0], port=9222))
    args = {"action": "status", "port": 9222, "run_id": "batch", "account": "Alice"}
    with pytest.raises(ValueError, match="outside"):
        mcp_server.tool_browser_facebook(args)
    with pytest.raises(ValueError, match="port alone"):
        mcp_server.tool_browser_facebook({**args, "port": 9333})


def test_import_existing_progress_is_paused_and_keeps_completed_account_mapping(tmp_path):
    inventory = {"batch": "batch", "profiles": [
        {"id": "id-23", "profile": "Profile 23", "accounts": [
            {"account": "Alice", "stage": "logged_out", "page_name": "Keep this Page", "password": "SECRET"}]},
        {"id": "id-24", "profile": "Profile 24", "accounts": [
            {"account": "Bob", "stage": "blocked", "blocker": "creation_uncertain"}]},
    ]}
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory), encoding="utf-8")
    state = runs.import_inventory(path)
    assert state["status"] == "paused"
    assert state["saved_accounts"][0]["stage"] == "logged_out"
    assert state["saved_accounts"][1]["blocker"] == "creation_uncertain"
    assert "SECRET" not in json.dumps(state)
    assert json.loads(path.read_text()) == inventory
    with pytest.raises(ValueError, match="paused"):
        runs.guard_profile("batch", "id-23")


def test_run_tool_restores_binding_without_opening_browser(monkeypatch):
    monkeypatch.setattr(gologin, "profiles", profiles)
    state = json.loads(mcp_server.tool_browser_facebook_run({"action": "start", "run_id": "batch", "profile_start": 23, "profile_end": 29}))
    assert state["profile_end"] == 29
    assert mcp_server._FACEBOOK_RUN_ID == "batch"
    mcp_server.tool_browser_facebook_run({"action": "pause"})
    assert mcp_server._FACEBOOK_RUN_ID is None
    state = json.loads(mcp_server.tool_browser_facebook_run({"action": "status"}))
    assert state["status"] == "paused"
    assert mcp_server._GOLOGIN["session"] is None


def test_action_result_is_saved_automatically_without_raw_credentials(monkeypatch):
    runs.start("batch", 23, 29, profiles())
    observed()
    owner = SimpleNamespace(profile=profiles()[1], port=9222)
    monkeypatch.setitem(mcp_server._GOLOGIN, "session", owner)
    reply = {"ok": True, "result": {"stage": "home_ready", "agentBrowseMs": 80000}}
    monkeypatch.setattr(mcp_server, "tool_browser_script", lambda args: "port=9222\n" + json.dumps(reply))
    mcp_server.tool_browser_facebook({"action": "browse", "port": 9222, "run_id": "batch", "account": "Alice"})
    state = runs.status()
    assert state["pending_action"] is None
    assert state["cursor"]["agentBrowseMs"] == 80000
    assert state["last_action"]["action"] == "browse"
    def fail(args):
        raise RuntimeError("PRIVATE_PASSWORD")
    monkeypatch.setattr(mcp_server, "tool_browser_script", fail)
    with pytest.raises(Exception, match="Secret-bearing"):
        mcp_server.tool_browser_facebook({"action": "login", "port": 9222, "run_id": "batch", "account": "Alice", "password": "PRIVATE_PASSWORD"})
    state = runs.status()
    assert state["last_action"]["outcome"] == "requires_inspection"
    assert state["requires_observation"]
    assert "PRIVATE_PASSWORD" not in json.dumps(state)


def test_discovery_and_page_mapping_survive_before_submission():
    runs.start("batch", 23, 29, profiles())
    token = runs.begin_action("batch", "id-23", "__inventory__", "accounts")
    runs.finish_action("batch", token, True, cards=[
        {"name": "Alice", "publicAccountIds": ["123"]},
        {"name": "Alice", "publicAccountIds": ["456"]},
        {"name": "Bob", "publicAccountIds": []}])
    saved = runs.status()["saved_accounts"]
    assert len(saved) == 3
    assert saved[1]["account_id"] == "456"
    assert saved[1]["account"] != saved[0]["account"]
    observed()
    runs.begin_action("batch", "id-23", "Alice", "fill", {
        "page_name": "Stable Page", "category": "Community", "password": "NEVER_SAVE"})
    state = runs.status()
    assert state["saved_accounts"][0]["page_name"] == "Stable Page"
    assert "NEVER_SAVE" not in json.dumps(state)


def test_commit_journal_wins_over_stale_cursor_after_lost_creation_response():
    runs.start("batch", 23, 29, profiles())
    observed()
    runs.begin_action("batch", "id-23", "Alice", "create")
    digest = hashlib.sha256(b"gologin:id-23").hexdigest()
    path = runs.checkpoint_dir() / (digest + ".json")
    path.write_text(json.dumps({json.dumps(["batch", "Alice"]):
        {"stage": "submitting", "pageName": "Existing Page"}}), encoding="utf-8")
    state = runs.status()
    assert state["cursor"]["stage"] == "submitting"
    assert state["cursor"]["next_action"] == "confirm"
    assert state["requires_observation"]
    assert state["pending_action"]["action"] == "create"
    with pytest.raises(ValueError, match="in flight"):
        runs.begin_action("batch", "id-23", "Alice", "create")
