#!/usr/bin/env python3
"""End-to-end smoke test: launch a GTK app, drive it, and verify the result.

This is the real check — it proves observation, decision, execution and
verification all work against a live desktop, not just a fixture.

    python scripts/live_smoke.py                # dry run: decide, do not act
    python scripts/live_smoke.py --act          # actually click, then verify

Requires the WinRects helper to be loaded for native Wayland apps:
    gnome-extensions info winrects@cua   # want: State: ACTIVE
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jev_use.candidates import Observation, observe  # noqa: E402
from jev_use.choosers import Decision  # noqa: E402
from jev_use.driver import Driver, DriverError  # noqa: E402
from jev_use.loop import execute  # noqa: E402


def find(observation: Observation, label: str) -> dict | None:
    """First candidate whose label matches, case-insensitively."""
    for candidate in observation.candidates:
        if candidate["label"].strip().lower() == label.lower():
            return candidate
    for candidate in observation.candidates:
        if label.lower() in candidate["label"].lower():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--act", action="store_true", help="actually click (default: dry run)")
    parser.add_argument("--app", default="gnome-calculator")
    parser.add_argument("--expect", default="42", help="value expected after the sequence")
    parser.add_argument(
        "--sequence",
        default="C,7,×,6,=",
        help="comma-separated button labels (GNOME Calculator uses C and ×, not Clear/Multiply)",
    )
    parser.add_argument("--settle", type=float, default=0.7)
    args = parser.parse_args()
    sequence = [s.strip() for s in args.sequence.split(",") if s.strip()]

    with Driver() as driver:
        print(f"launching {args.app} ...")
        launched = driver.call("launch_app", {"launch_path": args.app}).json()
        pid = launched["pid"]
        time.sleep(3)

        windows = driver.call("list_windows", {}).json().get("windows", [])
        mine = [w for w in windows if w["pid"] == pid]
        if not mine:
            print(
                f"FAIL: pid {pid} is running but exposes no window.\n"
                "  Native Wayland windows need the WinRects helper:\n"
                "    gnome-extensions info winrects@cua   # want State: ACTIVE\n"
                "  If it is not ACTIVE, log out and back in once."
            )
            driver.call("kill_app", {"pid": pid})
            return 2

        window_id = mine[0]["window_id"]
        print(f"target: pid {pid} window {window_id} {mine[0].get('title')!r}")

        observation = observe(driver, pid, window_id)
        print(f"tree: {len(observation.elements)} elements, "
              f"{len(observation.candidates)} actionable")
        if not observation.candidates:
            print("FAIL: no actionable elements. Is renderer accessibility off?")
            driver.call("kill_app", {"pid": pid})
            return 2

        print(f"\n{'ACTING' if args.act else 'DRY RUN'}: {' -> '.join(sequence)}")

        # One snapshot for the whole sequence. Perception costs ~750 ms on this
        # driver and a click ~1350 ms, so re-observing before every press is the
        # single biggest avoidable cost in a keypad-style task.
        started = time.perf_counter()
        observation = observe(driver, pid, window_id)

        resolved = []
        for name in sequence:
            candidate = find(observation, name)
            if candidate is None:
                labels = ", ".join(c["label"] for c in observation.candidates)
                print(f"FAIL: no candidate labelled {name!r}. Available: {labels}")
                driver.call("kill_app", {"pid": pid})
                return 2
            resolved.append((name, candidate))

        for name, candidate in resolved:
            if args.act:
                execute(
                    driver,
                    Decision(kind="click_element", element_id=candidate["id"], confidence=1.0),
                    observation,
                )
            print(f"  {name:<9} -> {'clicked' if args.act else 'would click'} {candidate['description']}")

        elapsed = time.perf_counter() - started
        if args.act:
            print(
                f"\n{len(resolved)} actions from 1 snapshot in {elapsed:.2f}s "
                f"({elapsed / len(resolved) * 1000:.0f} ms/action)"
            )

        if not args.act:
            print("\nDRY RUN complete — nothing was clicked. Re-run with --act.")
            driver.call("kill_app", {"pid": pid})
            return 0

        final = observe(driver, pid, window_id)
        # GNOME Calculator exposes the running total as the LABEL of a text box,
        # not its value, so check both.
        texts = []
        for element in final.elements:
            for field in ("value", "label"):
                candidate = element.get(field)
                if candidate not in (None, "") and str(candidate) not in texts:
                    texts.append(str(candidate))
        print(f"\nreadable text in window: {texts}")

        ok = any(args.expect == text or args.expect in text for text in texts)
        print("PASS" if ok else f"FAIL: {args.expect!r} not found in the window")

        driver.call("kill_app", {"pid": pid})
        print("app closed")
        return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DriverError as exc:
        print(f"driver error: {exc}", file=sys.stderr)
        raise SystemExit(2)
