#!/usr/bin/env python3
"""Fast browser driving on the accessibility path.

The naive version of this script spent most of its wall clock asleep: 8-12 s of
fixed sleep after every navigation, when the actual input work is ~3-5 s. Two
changes remove almost all of it.

1. **Do not sleep per page.** Opening a tab does not depend on the previous tab
   having finished loading. `ctrl+t` works while the last page is still fetching,
   so N navigations can be issued back to back and the loading overlapped. Only
   the genuinely sequential step (search, then click a result) needs to wait.

2. **Poll for readiness, never sleep a constant.** `list_windows` is the cheapest
   signal at ~496 ms; poll the title until it changes, bounded by a timeout.

## Why the AT-SPI path has a ~5 s per-page floor

Both measured on this machine:

    hotkey ctrl+t (foreground)     1.8 - 3.2 s   (varies a lot)
    type_text      (foreground)    ~1.1 s
    press_key      (foreground)    ~1.1 s
    ---
    ~5 s per navigation, and background delivery is unavailable

Three foreground calls are mandatory per navigation: a trailing newline on
type_text does not submit, so type + Enter cannot be collapsed. CDP navigation
(browser_navigate) is ~0.6 s and needs no per-call activation dance at all.

Also: every foreground input call fails if the target window is not on the active
workspace, and a failed attempt still costs ~1-1.8 s. Focus first, once.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jev_use.driver import Driver, DriverError  # noqa: E402

POLL_INTERVAL = 0.35
READY_TIMEOUT = 15.0


def winrects() -> list[dict]:
    out = subprocess.run(
        [
            "gdbus", "call", "--session", "--dest", "org.cua.WinRects",
            "--object-path", "/org/cua/WinRects", "--method", "org.cua.WinRects.GetRects",
        ],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return json.loads(ast.literal_eval(out)[0])
    except Exception:
        return []


def focus_once(driver: Driver, pid: int) -> float:
    """Put the target window on the active workspace.

    Foreground input is refused otherwise, and each refusal costs a second or so,
    so this must happen before anything else.
    """
    started = time.perf_counter()
    for _ in range(6):
        if any(w["visible"] and w["pid"] == pid for w in winrects()):
            break
        driver.call("hotkey", {"keys": ["ctrl", "alt", "left"], "scope": "desktop"})
        time.sleep(1.5)
    return time.perf_counter() - started


class Browser:
    def __init__(self, driver: Driver, pid: int, window_id: int) -> None:
        self.driver = driver
        self.pid = pid
        self.window_id = window_id
        self.args = {"pid": pid, "window_id": window_id, "delivery_mode": "foreground"}

    def title(self) -> str:
        for record in self.driver.call("list_windows", {}).json().get("windows", []):
            if record.get("window_id") == self.window_id:
                return record.get("title") or ""
        return ""

    def wait_ready(self, previous: str = "", timeout: float = READY_TIMEOUT) -> tuple[bool, float]:
        """Poll until the title changes from `previous`, or the timeout expires."""
        started = time.perf_counter()
        while time.perf_counter() - started < timeout:
            current = self.title()
            if current and current != previous:
                return True, time.perf_counter() - started
            time.sleep(POLL_INTERVAL)
        return False, time.perf_counter() - started

    def open(self, url: str, settle: float = 0.5) -> None:
        """Fire a navigation without waiting for the page to finish loading."""
        self.driver.call("hotkey", {**self.args, "keys": ["ctrl", "t"]})
        self.driver.call("type_text", {**self.args, "text": url})
        self.driver.call("press_key", {**self.args, "key": "return"})
        if settle:
            time.sleep(settle)

    def go(self, url: str, timeout: float = READY_TIMEOUT) -> float:
        """Navigate and wait for the title to change. For sequential steps."""
        previous = self.title()
        started = time.perf_counter()
        self.driver.call("hotkey", {**self.args, "keys": ["ctrl", "t"]})
        self.driver.call("type_text", {**self.args, "text": url})
        self.driver.call("press_key", {**self.args, "key": "return"})
        self.wait_ready(previous, timeout)
        return time.perf_counter() - started

    def click(self, x: int, y: int) -> None:
        self.driver.call(
            "click", {"pid": self.pid, "window_id": self.window_id, "x": x, "y": y,
                      "delivery_mode": "foreground"}
        )


def find_chrome(driver: Driver) -> dict:
    for window in driver.call("list_windows", {}).json().get("windows", []):
        if "chrome" in (window.get("app_name") or "").lower():
            return window
    raise DriverError("no Chrome window found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--sequential", action="store_true", help="wait for each page")
    parser.add_argument("--focus-timeout", type=float, default=READY_TIMEOUT)
    args = parser.parse_args()

    with Driver() as driver:
        window = find_chrome(driver)
        browser = Browser(driver, window["pid"], window["window_id"])

        focus = focus_once(driver, window["pid"])
        print(f"focus            {focus:6.2f}s")
        started = time.perf_counter()

        if args.sequential:
            for url in args.urls:
                took = browser.go(url, args.focus_timeout)
                print(f"  {url[:46]:<48} {took:6.2f}s  {browser.title()[:40]}")
        else:
            for url in args.urls:
                browser.open(url)
            print(f"  issued {len(args.urls)} navigations in {time.perf_counter() - started:6.2f}s")
            ok, waited = browser.wait_ready(timeout=args.focus_timeout)
            print(f"  waited for the last one: {waited:6.2f}s")

        total = time.perf_counter() - started
        print(f"total            {total:6.2f}s   ({total / len(args.urls):.2f}s per page)")
        print(f"final title: {browser.title()[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
