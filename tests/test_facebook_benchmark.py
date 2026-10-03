"""Benchmark reporting for cumulative, resumable Facebook audit responses."""

from scripts.benchmark_facebook_audit import summarize


def response(*, complete, results, accounts=("A", "B"), call_seconds=0,
             enumeration_seconds=0, state=None):
    return {
        "state": state or ("complete" if complete else "in_progress"),
        "complete": complete,
        "accounts": list(accounts),
        "results": results,
        "resume_token": None if complete else "opaque-token",
        "timings": {
            "call_seconds": call_seconds,
            "chunk_seconds": call_seconds,
            "enumeration_seconds": enumeration_seconds,
        },
    }


def verified(account, seconds, *, location="Lahore", retries=0):
    return {
        "account": account,
        "location": location,
        "identity_verified": True,
        "state": "location",
        "elapsed_seconds": seconds,
        "location_retries": retries,
    }


def chunk(response_value, **capture):
    return {"response": response_value,
            "capture": {"response": response_value, **capture}}


def test_cumulative_chunks_count_accounts_and_enumeration_once():
    first = response(
        complete=False,
        results=[verified("A", 3, retries=1)],
        call_seconds=10,
        enumeration_seconds=4,
    )
    second = response(
        complete=True,
        results=[verified("A", 3, retries=1), verified("B", 5)],
        call_seconds=8,
        # The enumeration duration is repeated in every cumulative response.
        enumeration_seconds=4,
    )

    report = summarize([
        chunk(first, wall_seconds=12, tool_calls=1, model_calls=1),
        chunk(second, wall_seconds=9, tool_calls=1, model_calls=1),
    ])

    assert report["chunks"] == 2
    assert report["enumerated_accounts"] == 2
    assert report["verified_unique_accounts"] == 2
    assert report["verified_complete"] is True
    assert report["result_elapsed_seconds"] == 8  # A is present in both snapshots.
    assert report["audit_call_seconds"] == 18
    assert report["enumeration_seconds"] == 4
    assert report["wall_seconds"] == 21
    assert report["unattributed_wall_seconds"] == 3
    assert report["location_retries"] == 1
    assert report["tool_calls"] == 2
    assert report["model_calls"] == 2
    # The report is an aggregate and must not leak account or location content.
    assert "A" not in report
    assert "Lahore" not in str(report)


def test_reported_complete_is_not_verified_complete_without_location():
    incomplete_result = verified("B", 5, location="")
    complete_response = response(
            complete=True,
            results=[verified("A", 3), incomplete_result],
            call_seconds=9,
            enumeration_seconds=4,
        )
    report = summarize([chunk(complete_response)])

    assert report["reported_complete"] is True
    assert report["verified_unique_accounts"] == 1
    assert report["verified_complete"] is False


def test_blocked_partial_audit_never_claims_completion_or_model_timing():
    partial = response(
        complete=False,
        results=[verified("A", 3)],
        call_seconds=10,
        enumeration_seconds=4,
        state="blocked",
    )
    partial["blocker"] = {"state": "timeout", "account": "B"}
    partial["resume_token"] = "opaque-token"

    report = summarize([chunk(partial)])

    assert report["reported_complete"] is False
    assert report["verified_complete"] is False
    assert report["resume_token_present"] is True
    assert report["wall_seconds"] is None
    assert report["model_calls"] is None
    assert report["unattributed_wall_seconds"] is None
