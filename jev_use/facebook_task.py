"""A small-agent interface: one verified, bounded transition per tool call.

The model supplies the user's scope/inputs; code owns sequencing. Browser truth
and submission journals always outrank the controller's saved suggestions.
"""
from __future__ import annotations

import json
import threading
import time

from . import facebook_runs as runs

SCHEMA = {
    "name": "browser_facebook_next",
    "description": (
        "Preferred Facebook workflow for small agents. Start/resume browser_facebook_run with "
        "the user's exact profiles first, then repeat this tool with the same run_id. It owns "
        "profile/account selection, fresh status, and ONE bounded next action. Follow status: "
        "continue=call again; needs_input=supply requested fields; blocked/needs_resume/paused=stop; "
        "completed=verified creation AND logout for every inventoried account, profiles saved. "
        "Runtime password is retained only in this MCP process for this run; never saved. "
        "Page details apply to the returned current account; supply only when requested. "
        "Each new user-authorized run creates one additional Page per observed account; "
        "only completion in THIS run is skipped. Resume retains this run's creation guards. "
        "Never run alongside another worker or use raw scripts to escape a blocker."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "run_id": {"type": "string", "minLength": 1},
            "password": {"type": "string", "minLength": 1, "description": "User-supplied common password, memory-only for this run until pause/restart or 30 minutes idle."},
            "corrected_password": {"type": "string", "minLength": 1, "description": "Only an explicit user correction after rejection; never guess or retry the old password."},
            "page_name": {"type": "string", "minLength": 1},
            "category": {"type": "string", "minLength": 1},
            "bio": {"type": "string"},
        },
        "required": ["run_id"],
    },
}

_LOCK = threading.Lock()
_PASSWORDS: dict[str, tuple[str, float]] = {}
_TTL = 30 * 60


def _matches(checkpoint: dict, account: dict) -> bool:
    if checkpoint.get("accountId") and account.get("account_id"):
        return checkpoint["accountId"] == account["account_id"]
    return checkpoint["account"] in {account["account"], account.get("account_name")}


def _completion(profile: dict, account: dict, run_id: str) -> dict | None:
    matching = [c for c in profile["checkpoints"] if c["run_id"] == run_id and _matches(c, account)]
    # A different job's Page is history, not completion of this request.
    if any(c.get("stage") in {"submission_reserved", "submitting", "created"}
           and not c.get("completed") for c in matching):
        return None
    completed = [c for c in matching if c.get("completed")]
    return max(completed, key=lambda c: c.get("completedAt") or c.get("updatedAt") or 0, default=None)


def _known_accounts(state: dict, profile: dict) -> list[dict]:
    accounts = [a for a in state.get("saved_accounts", []) if a["profile_id"] == profile["id"]]
    for c in profile["checkpoints"]:
        if c["run_id"] == state["run_id"] and c["account"] != "__inventory__" and not any(a["account"] == c["account"] for a in accounts):
            accounts.append({"account": c["account"], "account_id": c.get("accountId")})
    return accounts


def _verified_accounts(state: dict) -> list[tuple]:
    return [(p, a, c) for p in state["profiles"] for a in _known_accounts(state, p)
            if (c := _completion(p, a, state["run_id"])) is not None]


def forget(run_id: str) -> None:
    _PASSWORDS.pop(run_id, None)


def _password(run_id: str, supplied: str | None = None) -> str | None:
    now = time.monotonic()
    for key, (_, used) in list(_PASSWORDS.items()):
        if now - used > _TTL:
            forget(key)
    if supplied is not None:
        _PASSWORDS[run_id] = (supplied, now)
    entry = _PASSWORDS.get(run_id)
    if entry:
        _PASSWORDS[run_id] = (entry[0], now)
        return entry[0]
    return None


def _payload(text: str) -> dict:
    if text.startswith("port="):
        text = text.split("\n", 1)[1]
    value = json.loads(text)
    if not isinstance(value, dict) or value.get("ok") is False:
        raise ValueError("Backend did not return a successful structured observation")
    result = value.get("result", value)
    if not isinstance(result, dict):
        raise ValueError("Backend result is not an object")
    return result


def advance(args: dict, *, owner, open_profile, close_profile, action, prepare_profile=None) -> dict:
    """Injected browser boundaries make restart/failure behavior replayable offline."""
    if set(args) - set(SCHEMA["inputSchema"]["properties"]):
        raise ValueError("Unsupported next-step argument")
    for key, value in args.items():
        if not isinstance(value, str) or (key != "bio" and not value.strip()):
            raise ValueError(f"{key} must be a non-empty string")
    if not args.get("run_id"):
        raise ValueError("run_id is required")
    if args.get("password") and args.get("corrected_password"):
        raise ValueError("Supply password or corrected_password, not both")
    if not _LOCK.acquire(blocking=False):
        return {"status": "blocked", "reason": "another_controller_call_is_running"}
    try:
        return _advance(args, owner=owner, open_profile=open_profile,
                        close_profile=close_profile, action=action, prepare_profile=prepare_profile)
    finally:
        _LOCK.release()


def _advance(args, *, owner, open_profile, close_profile, action, prepare_profile=None):
    run_id = args["run_id"]
    state = runs.status(run_id)

    def reply(status, **fields):
        verified = _verified_accounts(state)
        known = {(p["id"], a["account"]) for p in state["profiles"] for a in _known_accounts(state, p)}
        history = [c for p in state["profiles"] for c in p["checkpoints"] if c["run_id"] != run_id]
        return {"status": status, "run_id": run_id, **fields,
                "page_goal": "one_new_page_per_account",
                "history": {"previous_pages_created": sum(bool(c.get("completed")) for c in history),
                            "previous_unresolved_creations": sum(c.get("stage") in {"submission_reserved", "submitting", "created"}
                                                                 and not c.get("completed") for c in history)},
                "progress": {"accounts_observed": len(known), "accounts_completed": len(verified),
                             "profiles_completed": sum(bool(state.get("controller", {}).get(p["id"], {}).get("completed")) for p in state["profiles"])},
                **({"results": [{"profile": p["name"], "account": a["account"], "source_run": c["run_id"],
                                  "page_name": c.get("pageName"), "confirmation": c.get("confirmation"),
                                  "elapsed_ms": c.get("elapsedMs")} for p, a, c in verified]}
                   if status == "completed" else {}),
                "next_call": {"tool": SCHEMA["name"], "arguments": {"run_id": run_id}}
                if status == "continue" else None}

    if state["status"] != "active":
        forget(run_id)
        return reply("paused", reason="explicit_resume_required")
    if state.get("pending_action"):
        return reply("needs_resume", reason="interrupted_or_inflight_action",
                     action=state["pending_action"]["action"])
    _password(run_id, args.get("password"))
    profiles = state["profiles"]
    current_owner = owner()
    allowed = {p["id"] for p in profiles}
    if current_owner and current_owner.profile.id not in allowed:
        return reply("blocked", reason="another_profile_is_owned_by_this_connection")
    unfinished = [p for p in profiles if not state.get("controller", {}).get(p["id"], {}).get("completed")]
    profile = next((p for p in profiles if current_owner and p["id"] == current_owner.profile.id), None)
    profile = profile or next(iter(unfinished), None)
    if profile is None:
        forget(run_id)
        verified = {(p["id"], a["account"]) for p, a, _ in _verified_accounts(state)}
        accounts = {(p["id"], a["account"]) for p in profiles for a in _known_accounts(state, p)}
        if not accounts or not accounts.issubset(verified) or any(
                not any(profile_id == p["id"] for profile_id, _ in verified) for p in profiles):
            return reply("blocked", reason="completion_journals_missing_or_inconsistent")
        return reply("completed", profiles_completed=len(profiles))
    profile_id = profile["id"]
    context = {"profile_id": profile_id, "profile_name": profile["name"]}
    progress = state.get("controller", {}).get(profile_id, {})
    if not current_owner:
        # This callback must verify ownership rather than trusting launcher text.
        try:
            open_profile(profile_id, run_id)
        except Exception:
            return reply("blocked", **context, reason="profile_open_failed")
        opened = owner()
        if not opened or opened.profile.id != profile_id:
            return reply("blocked", **context, reason="profile_open_not_verified")
        return reply("continue", **context, performed="open_profile")
    port = current_owner.port
    if prepare_profile is not None:
        try:
            prepared = prepare_profile(run_id, current_owner)
        except Exception as exc:
            reason = "another_worker_owns_run" if str(exc).startswith("another_worker_owns_run:") else "workflow_tab_unavailable"
            return reply("blocked", **context, reason=reason,
                         instruction="Inspect the original tab; never replace a pending creation tab.")
        if prepared:
            return reply("continue", **context, performed="prepare_workflow_tab")
    base = {"run_id": run_id, "port": port}
    checkpoints = [c for c in profile["checkpoints"] if c["run_id"] == run_id and c["account"] != "__inventory__"]
    saved = _known_accounts(state, profile)

    def checkpoint_for(item):
        return next((c for c in checkpoints if c["account"] == item["account"]), {})

    def done(item):
        return _completion(profile, item, run_id) is not None

    pending = [a for a in saved if not done(a)]
    cursor = state.get("cursor") or {}
    account = next((a for a in pending if cursor.get("profile_id") == profile_id and a["account"] == cursor.get("account")), None)
    account = account or next(iter(pending), None)
    if account is None and not progress.get("inventoried"):
        # On first attachment inspect live identity before assuming a chooser.
        try:
            observed = _payload(action({**base, "account": "__inventory__", "action": "status"}))
            current = observed.get("current_state", {})
            if current.get("kind") != "account_chooser":
                return reply("blocked", **context, reason="signed_in_or_unknown_account_without_checkpoint",
                             current_state=current)
            inventory = _payload(action({**base, "account": "__inventory__", "action": "accounts"}))
            if not inventory.get("accounts"):
                return reply("blocked", **context, reason="no_saved_accounts_observed")
            runs.task_progress(run_id, profile_id, inventoried=True)
            return reply("continue", **context, performed="inventory", accounts_observed=len(inventory["accounts"]))
        except Exception:
            return reply("blocked", **context, reason="account_inventory_failed", diagnostic_tool="browser_facebook_vision")
    if account is None:
        # All currently known accounts are done; refresh chooser before closure.
        try:
            inventory = _payload(action({**base, "account": "__inventory__", "action": "accounts"}))
            refreshed = runs.status(run_id)
            known = {a["account"] for a in saved}
            if any(a["profile_id"] == profile_id and a["account"] not in known for a in refreshed.get("saved_accounts", [])):
                return reply("continue", **context, performed="refresh_inventory")
            close_profile()
            if owner() is not None:
                return reply("blocked", **context, reason="profile_save_not_verified")
            runs.task_progress(run_id, profile_id, completed=True)
            return reply("continue", **context, performed="close_saved_profile")
        except Exception:
            return reply("blocked", **context, reason="profile_close_or_chooser_verification_failed")
    context["account"] = account["account"]
    current_checkpoint = checkpoint_for(account)
    # A new job is a separate Page request. Never reuse or change an older
    # reservation; a resumed current job still follows its own live journal.
    public = {key: account[key] for key in ("account_name", "account_id") if account.get(key)}
    if current_checkpoint.get("accountId"):
        public["account_id"] = current_checkpoint["accountId"]
    call = {**base, **public, "account": account["account"]}
    try:
        observed = _payload(action({**call, "action": "status"}))
    except Exception:
        return reply("blocked", **context, reason="live_status_unavailable", diagnostic_tool="browser_facebook_vision")
    stage = observed.get("stage", "started")
    live = observed.get("live_login", {})
    current = observed.get("current_state", {})
    next_action = observed.get("next_action")
    if current.get("accountMismatch") or next_action in {None, "inspect"} or stage == "mfa_required":
        return reply("blocked", **context, reason=observed.get("blocker") or "identity_challenge_or_unknown_state", stage=stage,
                     current_state=current, diagnostic_tool="browser_facebook_vision")
    if stage == "logged_out":
        if not observed.get("confirmation"):
            return reply("blocked", **context, reason="completion_evidence_missing")
        return reply("continue", **context, performed="verify_completed_account")
    if live.get("passwordRejected"):
        forget(run_id)
        if not args.get("corrected_password"):
            return reply("needs_input", **context, reason="password_rejected", required=["corrected_password"])
        next_action = "correct_password"
        call["password"] = args["corrected_password"]
    elif args.get("corrected_password"):
        return reply("blocked", **context, reason="correction_requires_explicit_password_rejection")
    elif next_action == "login":
        recovery = next(reversed(observed.get("loginRecoveries") or []), None)
        current_attempt = stage == "started" or recovery is not None and recovery.get("completedAt") is None
        if observed.get("loginSubmitted") and current_attempt and not live.get("identityPresent"):
            return reply("blocked", **context, reason="login_submitted_identity_unconfirmed")
        secret = _password(run_id)
        if not secret:
            return reply("needs_input", **context, reason="runtime_password_required", required=["password"])
        call["password"] = secret
    if next_action == "fill":
        details = {key: args.get(key, account.get(key)) for key in ("page_name", "category", "bio")}
        missing = [key for key in ("page_name", "category") if not details.get(key)]
        if missing:
            return reply("needs_input", **context, reason="page_details_required", required=missing)
        call.update({key: value for key, value in details.items() if value is not None})
    allowed_actions = {"reset_login_view", "retry_login", "correct_password", "login", "wait_home", "browse", "open_form",
                       "wait_form", "fill", "create", "confirm", "home", "browse_after", "logout"}
    if next_action not in allowed_actions:
        return reply("blocked", **context, reason="unsupported_next_action")
    previous = progress.get("failure", {})
    signature = {"account": account["account"], "action": next_action, "stage": stage}
    same = all(previous.get(key) == value for key, value in signature.items())
    if same and previous.get("count", 0) >= 2:
        return reply("blocked", **context, reason="repeated_action_failure", action=next_action,
                     failure_code=observed.get("loginFailure", {}).get("code"),
                     live_login=live,
                     diagnostic_tool="browser_facebook_vision")
    try:
        result = _payload(action({**call, "action": next_action}))
    except Exception:
        if next_action == "correct_password":
            forget(run_id)
        runs.task_progress(run_id, profile_id, failure={**signature, "count": previous.get("count", 0) + 1 if same else 1})
        return reply("continue", **context, performed=next_action, outcome="requires_fresh_inspection")
    runs.task_progress(run_id, profile_id, clear_failure=True)
    if next_action == "correct_password":
        _password(run_id, args["corrected_password"])
    return reply("continue", **context, performed=next_action, stage=result.get("stage"),
                 browse_remaining_seconds=result.get("browse_remaining_seconds"),
                 after_remaining_seconds=result.get("after_remaining_seconds"))
