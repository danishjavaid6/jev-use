"""Structured, agent-driven Facebook actions backed by the persistent bridge."""
import json

ACTIONS = ["tabs", "accounts", "status", "retry_login", "correct_password", "login", "wait_home", "browse", "open_form",
           "wait_form", "fill", "create", "confirm", "home", "browse_after", "logout"]
SCHEMA = {
    "name": "browser_facebook",
    "description": (
        "One Facebook Page workflow step using Betterwright; no code argument. "
        "Small agents should prefer browser_facebook_next, which chooses and executes the next step. "
        "Call browser_facebook_run first to restore the authorized profile range and progress. "
        "The agent owns the account/profile loop. Call status for next_action; repeat browse "
        "in chunks up to 60 seconds: Home 60, Reels 60, Home 60 with varied pacing; browse_after Home 30. "
        "Use browser_profiles/open/close for GoLogin. Never retry uncertain creation; confirm it. "
        "Jev browser_use can inspect unfamiliar UI; keep this workflow's selected tab."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ACTIONS},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "run_id": {"type": "string", "minLength": 1},
            "account": {"type": "string", "minLength": 1},
            "account_name": {"type": "string"},
            "account_id": {"type": "string", "description": "Only a user-supplied or observed verified ID."},
            "restart_from_run": {"type": "string", "description": "Explicit user-authorized precreation restart of the same verified account; requires a signed-out chooser and preserves historical journals."},
            "password": {"type": "string", "description": "Runtime login secret; never put in files. correct_password is only for a user-supplied correction after explicit rejection."},
            "page_name": {"type": "string"},
            "category": {"type": "string"},
            "bio": {"type": "string"},
            "seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 60},
            "page_url": {"type": "string", "description": "Existing tab URL on first attachment only."},
            "page_target_id": {"type": "string", "description": "Exact observed target ID from tabs when duplicate URLs remain ambiguous; omit after attachment."},
            "checkpoint_scope": {"type": "string"},
        },
        "required": ["action", "port", "run_id", "account"],
    },
}


def request(args):
    """Validate before touching the browser; data is JSON, never executable input."""
    unknown = set(args) - set(SCHEMA["inputSchema"]["properties"])
    if unknown:
        raise ValueError("Unsupported Facebook arguments: " + ", ".join(sorted(unknown)))
    for key in ("run_id", "account"):
        if not isinstance(args.get(key), str) or not args[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if args.get("action") not in ACTIONS:
        raise ValueError("Unknown Facebook action")
    if type(args.get("port")) is not int or not 1 <= args["port"] <= 65535:
        raise ValueError("port must be from 1 to 65535")
    seconds = args.get("seconds", 60)
    if type(seconds) is not int or not 1 <= seconds <= 60:
        raise ValueError("seconds must be from 1 to 60")
    for key in set(args) - {"port", "seconds"}:
        if not isinstance(args[key], str):
            raise ValueError(f"{key} must be a string")
    if args["action"] == "fill" and any(not args.get(k, "").strip() for k in ("page_name", "category")):
        raise ValueError("fill requires page_name and category")
    if "password" in args and args["action"] not in {"login", "correct_password"}:
        raise ValueError("password is accepted only by login or correct_password")
    if "restart_from_run" in args and (args["action"] != "login" or not args.get("account_id") or not args["restart_from_run"].strip() or args["restart_from_run"] == args["run_id"]):
        raise ValueError("restart_from_run requires login, a verified account_id and a different historical run")
    if args["action"] == "correct_password" and not args.get("password"):
        raise ValueError("correct_password requires the user's corrected runtime password")
    options = {key: args[key] for key in ("port", "page_url", "page_target_id", "checkpoint_scope") if key in args}
    data = {key: value for key, value in args.items() if key not in options}
    if args["action"] in {"browse", "browse_after"}:
        data.setdefault("seconds", seconds)
    options.update(timeout=90, facebook_workflow=True, code="return await facebookAgent.step(" + json.dumps(data) + ");")
    if args["action"] == "create":
        options["submission"] = {"run_id": args["run_id"], "account": args["account"], "verify_account": True}
    return options


def compact_response(output):
    """Keep the active step and diagnostics; other journals remain on disk."""
    if not isinstance(output, str):
        return output
    header, separator, body = output.partition("\n")
    if not separator or not header.startswith("port="):
        return output
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return output
    if not isinstance(payload, dict):
        return output
    payload.pop("workflowCheckpoints", None)
    return header + "\n" + json.dumps(payload, ensure_ascii=False)
