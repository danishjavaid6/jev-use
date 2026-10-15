"""Durable, non-secret batch scope and resume context for Facebook agents."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit


SCHEMA = {
    "name": "browser_facebook_run",
    "description": "Persist exactly the profiles selected by the user and their resume context. start accepts a numeric range, profile_numbers list (one item for a single profile), or observed profile_ids. No profiles are selected by default. status reads progress; resume continues that saved selection; pause stops new actions. Never supply passwords here.",
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["start", "status", "resume", "pause"]},
            "run_id": {"type": "string", "minLength": 1},
            "profile_start": {"type": "integer", "minimum": 1},
            "profile_end": {"type": "integer", "minimum": 1},
            "profile_numbers": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1}, "description": "Only the numbers requested by the user, in requested order. Do not combine with range or IDs."},
            "profile_ids": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}, "description": "Exact observed GoLogin IDs selected by the user, including custom-named profiles. Do not combine with range or numbers."},
        },
        "required": ["action"],
    },
}
_LOCK = threading.RLock()


def checkpoint_dir() -> Path:
    override = os.environ.get("JEV_USE_WORKFLOW_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or Path.home()) / ".jev-use" / "workflow-checkpoints"


def root() -> Path:
    return checkpoint_dir() / "runs"


def _path(run_id: str) -> Path:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("A stable run_id is required")
    return root() / (hashlib.sha256(run_id.encode()).hexdigest() + ".json")


@contextmanager
def _locked():
    # Coordinate separate MCP processes as well as threads. No browser ownership
    # is acquired here, and a crashed process releases this OS file lock.
    with _LOCK:
        root().mkdir(parents=True, exist_ok=True)
        with (root() / ".lock").open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def _write(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(run_id: str) -> dict:
    path = _path(run_id)
    if not path.exists():
        raise ValueError("Run state missing: call browser_facebook_run start with the user's exact profile range first")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1 or state.get("run_id") != run_id:
        raise ValueError("Run state is invalid; preserve it and inspect before acting")
    return state


def _save(state: dict) -> None:
    state["updated_at"] = time.time()
    _write(_path(state["run_id"]), state)
    _write(root() / "latest.json", {"run_id": state["run_id"]})


def _latest() -> str:
    path = root() / "latest.json"
    if not path.exists():
        raise ValueError("No saved Facebook run; start with the user's explicit profile range")
    return json.loads(path.read_text(encoding="utf-8"))["run_id"]


def _number(name: str) -> int | None:
    match = re.fullmatch(r"Profile\s+(\d+)", name.strip(), re.IGNORECASE)
    return int(match[1]) if match else None


def start(run_id: str, first: int | None, last: int | None, profiles: list, *,
          profile_numbers: list | None = None, profile_ids: list | None = None) -> dict:
    _path(run_id)
    is_range = first is not None or last is not None
    if sum((is_range, profile_numbers is not None, profile_ids is not None)) != 1:
        raise ValueError("Choose exactly the user's profile selection: a range, profile_numbers, or observed profile_ids")
    if is_range:
        if type(first) is not int or type(last) is not int or not 1 <= first <= last:
            raise ValueError("profile_start/profile_end must be an ascending positive numeric range")
        numbers = list(range(first, last + 1))
        selection = {"mode": "range", "profile_start": first, "profile_end": last}
    elif profile_numbers is not None:
        if not isinstance(profile_numbers, list) or not profile_numbers or any(type(n) is not int or n < 1 for n in profile_numbers):
            raise ValueError("profile_numbers must be a non-empty list of positive integers")
        numbers = list(dict.fromkeys(profile_numbers))
        first, last = min(numbers), max(numbers)
        selection = {"mode": "numbers", "profile_numbers": numbers}
    else:
        if not isinstance(profile_ids, list) or not profile_ids or any(not isinstance(i, str) or not i.strip() for i in profile_ids):
            raise ValueError("profile_ids must be a non-empty list of exact observed IDs")
        numbers = []
        selection = {"mode": "ids", "profile_ids": list(dict.fromkeys(profile_ids))}
    selected = []
    for number in numbers:
        matches = [p for p in profiles if _number(p.name) == number]
        if len(matches) != 1:
            raise ValueError(f"Profile {number} is missing or ambiguous in live discovery; do not substitute another profile")
        selected.append({"id": matches[0].id, "name": matches[0].name, "number": number})
    for profile_id in selection.get("profile_ids", []):
        matches = [p for p in profiles if p.id == profile_id]
        if len(matches) != 1:
            raise ValueError("Selected profile ID is missing or ambiguous in live discovery; do not substitute another profile")
        selected.append({"id": matches[0].id, "name": matches[0].name, "number": _number(matches[0].name)})
    if len({p["id"] for p in selected}) != len(selected):
        raise ValueError("Live profile IDs are ambiguous; do not substitute a profile")
    with _locked():
        if _path(run_id).exists():
            state = _load(run_id)
            if state["profiles"] != selected:
                raise ValueError("Run scope is immutable; do not change the range or profile IDs under an existing run_id")
            return _view(state)
        state = {"schema_version": 1, "run_id": run_id, "status": "active",
                 "profile_start": first, "profile_end": last, "profiles": selected,
                 "selection": selection,
                 "created_at": time.time(), "cursor": None, "pending_action": None,
                 "requires_observation": True}
        _save(state)
        return _view(state)


def _journals(profile_id: str) -> dict:
    digest = hashlib.sha256(("gologin:" + profile_id).encode()).hexdigest()
    path = checkpoint_dir() / (digest + ".json")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid workflow journal; inspect it without resetting progress")
    return data


def _checkpoint_summary(run_id: str, account: str, value: dict) -> dict:
    # Allowlist only non-secret resume data, never raw arguments/backend errors.
    fields = ("accountId", "stage", "pageName", "agentBrowseMs", "agentBrowsePlan",
              "agentAfterMs", "loginCardClicked", "loginSubmitted", "postCreateBrowsed",
              "completedAt", "elapsedMs", "timings", "loginStartedAt", "updatedAt")
    result = {key: value[key] for key in fields if key in value}
    result.update(run_id=run_id, account=account)
    stage = result.get("stage", "started")
    result["creation_uncertain"] = stage == "submitting"
    result["completed"] = stage == "logged_out" and bool(value.get("confirmation"))
    result["next_action"] = {"started": "login", "signed_in": "wait_home", "home_ready": "browse",
        "browsed": "open_form", "form_open": "wait_form", "form_ready": "fill",
        "submission_reserved": "create", "submitting": "confirm", "created": "status",
        "logged_out": "skip", "mfa_required": "inspect"}.get(stage, "inspect")
    if isinstance(value.get("loginFailure", {}).get("code"), str):
        result["login_failure_code"] = value["loginFailure"]["code"]
    if isinstance(value.get("confirmation"), dict):
        confirmation = value["confirmation"]
        result["confirmation"] = {key: confirmation[key] for key in ("text", "pageId") if key in confirmation}
        if isinstance(confirmation.get("pageUrl"), str):
            url = urlsplit(confirmation["pageUrl"])
            result["confirmation"]["pageUrl"] = urlunsplit((url.scheme, url.netloc, url.path, "", ""))
    return result


def _view(state: dict) -> dict:
    view = json.loads(json.dumps(state))
    for profile in view["profiles"]:
        profile["checkpoints"] = []
        for key, value in _journals(profile["id"]).items():
            run_id, account = json.loads(key)
            summary = _checkpoint_summary(run_id, account, value)
            profile["checkpoints"].append(summary)
            cursor = view.get("cursor")
            if cursor and cursor["profile_id"] == profile["id"] and account == cursor["account"] and run_id == state["run_id"]:
                cursor.update(summary)
    view["instruction"] = "Stay within these exact profile IDs. Read current browser status before resuming; saved checkpoints describe prior observations, not guaranteed live UI. Never repeat uncertain creation."
    view["resume_action"] = "resume" if state["status"] == "paused" or state.get("pending_action") else "status"
    if state["status"] == "paused" or state.get("pending_action"):
        view["requires_observation"] = True
    return view


def status(run_id: str | None = None) -> dict:
    with _locked():
        return _view(_load(run_id or _latest()))


def transition(run_id: str | None, action: str) -> dict:
    if action not in {"pause", "resume"}:
        raise ValueError("Unknown run transition")
    with _locked():
        state = _load(run_id or _latest())
        state["status"] = "paused" if action == "pause" else "active"
        state["requires_observation"] = True
        if action == "resume" and state.get("pending_action"):
            state["interrupted_action"] = state.pop("pending_action")
            state["pending_action"] = None
        _save(state)
        return _view(state)


def _guard(state: dict, profile_id: str) -> None:
    if profile_id not in {p["id"] for p in state["profiles"]}:
        raise ValueError("Profile is outside this run's user-requested range; no browser action is allowed")
    if state["status"] != "active":
        raise ValueError("This run is paused; explicit resume is required before browser actions")


def guard_profile(run_id: str, profile_id: str) -> None:
    with _locked():
        _guard(_load(run_id), profile_id)


def begin_action(run_id: str, profile_id: str, account: str, action: str, details: dict | None = None) -> str:
    with _locked():
        state = _load(run_id)
        _guard(state, profile_id)
        if state.get("pending_action"):
            raise ValueError("An action is still in flight or was interrupted; read run status and explicitly resume before retrying")
        if state.get("requires_observation") and action not in {"status", "accounts", "tabs"}:
            raise ValueError("Inspect current account status before resuming browser actions")
        if action not in {"status", "accounts", "tabs"} and state.get("last_observation") != {"profile_id": profile_id, "account": account}:
            raise ValueError("Inspect status for this specific profile/account before acting")
        token = uuid.uuid4().hex
        state["pending_action"] = {"id": token, "profile_id": profile_id, "account": account,
                                   "action": action, "started_at": time.time()}
        state["cursor"] = {"profile_id": profile_id, "account": account}
        public = {k: v for k, v in (details or {}).items() if k in {"account_id", "account_name", "page_name", "category", "bio"}}
        if public and account != "__inventory__":
            saved = state.setdefault("saved_accounts", [])
            item = next((a for a in saved if a["profile_id"] == profile_id and a["account"] == account), None)
            if item is None:
                item = {"profile_id": profile_id, "account": account, "stage": "pending"}
                saved.append(item)
            item.update(public)
        _save(state)
        return token


def finish_action(run_id: str, token: str, success: bool, checkpoint: dict | None = None, cards: list | None = None) -> None:
    with _locked():
        state = _load(run_id)
        pending = state.get("pending_action")
        if not pending or pending["id"] != token:
            return  # A later explicit pause/resume must not be overwritten.
        if checkpoint is None:
            for key, candidate in _journals(pending["profile_id"]).items():
                if json.loads(key) == [run_id, pending["account"]]:
                    checkpoint = candidate
                    break
        state["last_action"] = {**pending, "finished_at": time.time(),
                                "outcome": "observed" if success else "requires_inspection"}
        if checkpoint:
            state["cursor"].update(_checkpoint_summary(run_id, pending["account"], checkpoint))
            for item in state.get("saved_accounts", []):
                if item["profile_id"] == pending["profile_id"] and item["account"] == pending["account"]:
                    item["checkpoint_stage"] = checkpoint.get("stage")
                    if checkpoint.get("stage") in {"created", "logged_out", "submitting", "mfa_required"}:
                        item["stage"] = checkpoint["stage"]
                    if checkpoint.get("stage") in {"created", "logged_out"}:
                        item.pop("blocker", None)
        if cards:
            saved = state.setdefault("saved_accounts", [])
            for card in cards:
                name = card.get("name")
                ids = card.get("publicAccountIds", [])
                if not isinstance(name, str) or not name:
                    continue
                same_profile = [a for a in saved if a["profile_id"] == pending["profile_id"]]
                matching = next((a for a in same_profile if a.get("account_id") in ids or
                    (a["account"] == name or a.get("account_name") == name) and (not ids or not a.get("account_id"))), None)
                if matching is not None:
                    if len(ids) == 1 and not matching.get("account_id"):
                        matching["account_id"] = ids[0]
                    continue
                item = {"profile_id": pending["profile_id"], "account": name, "stage": "pending"}
                if len(ids) == 1:
                    item["account_id"] = ids[0]
                    if any(a["account"] == name or a.get("account_name") == name for a in same_profile):
                        item.update(account=ids[0], account_name=name)
                saved.append(item)
        state["pending_action"] = None
        state["requires_observation"] = not success or (pending["action"] not in {"status", "accounts"} and state.get("requires_observation", False))
        if success and pending["action"] in {"status", "accounts"}:
            state["last_observation"] = {"profile_id": pending["profile_id"], "account": pending["account"]}
        _save(state)


def record_profile(run_id: str, profile_id: str, port: int | None, event: str) -> None:
    with _locked():
        state = _load(run_id)
        if profile_id not in {p["id"] for p in state["profiles"]}:
            if event == "closed_saved":
                return  # Cleanup of an older owned session is always allowed.
            raise ValueError("Profile is outside the saved range")
        state["last_profile_event"] = {"profile_id": profile_id, "port": port, "event": event, "at": time.time()}
        if event == "opened":
            state["requires_observation"] = True
        _save(state)


def task_progress(run_id: str, profile_id: str, *, inventoried: bool = False,
                  completed: bool = False, failure: dict | None = None,
                  clear_failure: bool = False) -> None:
    """Persist only controller facts, never secrets, tool payloads or exceptions."""
    with _locked():
        state = _load(run_id)
        _guard(state, profile_id)
        progress = state.setdefault("controller", {}).setdefault(profile_id, {})
        if inventoried:
            progress["inventoried"] = True
        if completed:
            progress["completed"] = True
        if clear_failure:
            progress.pop("failure", None)
        if failure is not None:
            progress["failure"] = {k: failure[k] for k in ("account", "action", "stage", "count")}
        _save(state)


def import_inventory(path: Path) -> dict:
    """One-time migration of this project's existing non-secret batch inventory.

    It preserves every browser journal and always imports paused. No browser or
    network is touched. Existing run states are never overwritten.
    """
    inventory = json.loads(path.read_text(encoding="utf-8-sig"))
    profiles = [{"id": p["id"], "name": p["profile"], "number": _number(p["profile"])} for p in inventory["profiles"]]
    if not profiles or any(p["number"] is None for p in profiles):
        raise ValueError("Inventory has an invalid profile range")
    profiles.sort(key=lambda p: p["number"])
    numbers = [p["number"] for p in profiles]
    if numbers != list(range(numbers[0], numbers[-1] + 1)):
        raise ValueError("Inventory range must be contiguous")
    with _locked():
        run_id = inventory["batch"]
        if _path(run_id).exists():
            return _view(_load(run_id))
        state = {"schema_version": 1, "run_id": run_id, "status": "paused", "profile_start": numbers[0],
                 "profile_end": numbers[-1], "profiles": profiles, "created_at": time.time(),
                 "cursor": None, "pending_action": None, "requires_observation": True,
                 "imported_inventory": str(path.resolve()), "saved_accounts": []}
        for profile in inventory["profiles"]:
            for account in profile["accounts"]:
                state["saved_accounts"].append({"profile_id": profile["id"], **{k: account[k] for k in
                    ("account", "account_id", "page_name", "category", "bio", "stage", "blocker") if k in account}})
        latest_at = 0
        for profile in profiles:
            for key, checkpoint in _journals(profile["id"]).items():
                source_run, account = json.loads(key)
                if source_run != run_id or account == "__inventory__":
                    continue
                stamps = [checkpoint.get(k) for k in ("updatedAt", "completedAt", "loginStartedAt")]
                stamps.append(checkpoint.get("loginFailure", {}).get("at"))
                timestamp = max((s for s in stamps if isinstance(s, (int, float))), default=0)
                if timestamp > latest_at:
                    latest_at = timestamp
                    state["cursor"] = {"profile_id": profile["id"], **_checkpoint_summary(run_id, account, checkpoint),
                                       "source": "latest_dated_legacy_checkpoint", "live_inspection_required": True}
        _save(state)
        return _view(state)
