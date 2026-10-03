#!/usr/bin/env python3
"""Summarize captured Facebook Android audit responses without phone access.

Input is JSONL, one response per audit call. Lines may be the response object
itself or {"response": ..., "wall_seconds": ..., "tool_calls": ...}. The
wrapper lets the operator record harness wall time separately from reported
device/tool timings. Never estimates model/provider latency.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def read_chunks(path: Path) -> list[dict[str, Any]]:
    chunks = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        response = item.get("response", item)
        if not isinstance(response, dict) or not isinstance(response.get("results", []), list):
            raise ValueError(f"{path}:{line_number}: expected an audit response with a results array")
        chunks.append({"response": response, "capture": item})
    return chunks


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else 0.0


def summarize(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    verified: set[str] = set()
    audit_call_seconds = 0.0
    wall_seconds = 0.0
    wall_chunks = 0
    tool_calls = 0
    retries = 0
    retries_reported = False
    known_model_calls = 0
    has_model_call_count = False
    states: Counter[str] = Counter()
    enumerated: set[str] = set()
    final_results: dict[str, dict[str, Any]] = {}
    enumeration_seconds = 0.0
    reported_complete = False
    resume_token = None
    for chunk in chunks:
        response, capture = chunk["response"], chunk["capture"]
        enumerated.update(str(name) for name in response.get("accounts", []) if isinstance(name, str))
        reported_complete = response.get("complete") is True or response.get("state") == "complete"
        resume_token = response.get("resume_token", resume_token)
        timings = response.get("timings") or {}
        enumeration_seconds = max(enumeration_seconds, number(timings.get("enumeration_seconds")))
        audit_call_seconds += number(timings.get("call_seconds", timings.get("chunk_seconds")))
        if "wall_seconds" in capture:
            wall_seconds += number(capture["wall_seconds"])
            wall_chunks += 1
        if "tool_calls" in capture:
            tool_calls += int(number(capture["tool_calls"]))
        if "model_calls" in capture:
            known_model_calls += int(number(capture["model_calls"]))
            has_model_call_count = True
        for result in response.get("results", []):
            if not isinstance(result, dict) or not isinstance(result.get("account"), str):
                continue
            account = result["account"]
            final_results[account] = result
    result_seconds = sum(number(result.get("elapsed_seconds")) for result in final_results.values())
    for account, result in final_results.items():
        if isinstance(result.get("location_retries"), (int, float)) and not isinstance(result.get("location_retries"), bool):
            retries += int(number(result["location_retries"]))
            retries_reported = True
        state = str(result.get("state", "unknown"))
        states[state] += 1
        if state == "location" and result.get("identity_verified") is True and result.get("location"):
            verified.add(account)
    complete = (reported_complete and bool(enumerated)
                and enumerated.issubset(verified))
    return {
        "chunks": len(chunks), "enumerated_accounts": len(enumerated),
        "verified_unique_accounts": len(verified),
        "result_elapsed_seconds": round(result_seconds, 2),
        "audit_call_seconds": round(audit_call_seconds, 2),
        "enumeration_seconds": round(enumeration_seconds, 2),
        "wall_seconds": round(wall_seconds, 2) if wall_chunks else None,
        "wall_chunks_recorded": wall_chunks,
        "tool_calls": tool_calls if tool_calls else None,
        "model_calls": known_model_calls if has_model_call_count else None,
        "location_retries": retries if retries_reported else None,
        "result_states": dict(states), "reported_complete": reported_complete,
        "verified_complete": complete,
        "resume_token_present": bool(resume_token),
        "unattributed_wall_seconds": round(max(0.0, wall_seconds - audit_call_seconds), 2) if wall_chunks else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("responses", type=Path, help="JSONL captured audit call responses")
    args = parser.parse_args()
    try:
        result = summarize(read_chunks(args.responses))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
