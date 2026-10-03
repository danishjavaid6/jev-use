"""Observed Facebook Android account workflow, without model-chosen taps.

Keep generic Android automation in android.py. This adapter handles only the
account picker and the read-only primary-location page, never posts or settings.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from . import android


class Blocked(Exception):
    def __init__(self, state: str, detail: str):
        self.state, self.detail = state, detail
        super().__init__(detail)


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def identity(observation: android.Observation) -> str | None:
    if len(candidates(observation, "Open profile switcher")) != 1:
        return None
    names = {node.label().removesuffix(", see your profile") for node in observation.nodes
             if node.label().endswith(", see your profile")}
    return next(iter(names)) if len(names) == 1 else None


def check_screen(observation: android.Observation) -> None:
    state = android.location_screen_state(observation.read())
    if state:
        raise Blocked(state, {
            "locked": "Unlock the phone, then retry this account.",
            "network_error": "Restore the phone's internet connection, then retry this account.",
            "login": "Facebook requires sign-in; complete it on the phone, then retry.",
        }[state])
    if any(node.password for node in observation.nodes) or any(
            marker in observation.read().casefold() for marker in
            ("confirm your identity", "two-factor authentication", "enter the login code")):
        raise Blocked("authentication_required", "Complete Facebook's password or verification prompt on the phone, then retry. No credentials were filled.")
    if not observation.foreground.startswith(android.FACEBOOK_APP + "/"):
        raise Blocked("wrong_app", "Open Facebook on the phone, then retry.")


def candidates(observation: android.Observation, label: str) -> list[dict]:
    return [target for target in observation.targets if normalize(target["label"]) == normalize(label)]


def account_name(label: str) -> str:
    # Notifications belong to a card, not the account name; do not split every
    # comma, since a real account name may itself contain one.
    return re.sub(r", \d+ notifications?$", "", label).strip()


def menu_target(observation: android.Observation) -> dict | None:
    """Recognize explicitly observed Facebook tab bar geometries.

    No absolute coordinates or screen-size assumptions. Require a complete,
    evenly spaced, same-size row matching an observed layout. Unknown layouts
    fail closed, and opening it must produce a verified self-profile header.
    """
    # Prefer a plainly labeled control when Facebook exposes one.
    labeled = [target for target in observation.targets if normalize(target["label"]) == "menu"]
    if len(labeled) == 1:
        return labeled[0]
    if labeled:
        return None
    width, height = observation.screen
    rows: dict[tuple[int, int], list[dict]] = {}
    for target in observation.targets:
        left, top, right, bottom = target["bounds"]
        if (target["package"] == android.FACEBOOK_APP and target["role"] == "view"
                and (not target["label"] or re.search(r", tab [1-6] of 6(?:,|$)", target["label"]))
                and right > left and bottom > top):
            # Both bottom tab bars and the observed six-item header bar.
            bottom_row = top >= height * .75
            top_header_row = top >= height * .06 and bottom <= height * .16
            if not (bottom_row or top_header_row):
                continue
            rows.setdefault((top, bottom), []).append(target)
    matches = []
    for row in rows.values():
        row.sort(key=lambda target: target["bounds"][0])
        top_header = row[0]["bounds"][1] < height * .2
        if len(row) not in ((6,) if top_header else (5, 6)):
            continue
        expected = width / len(row)
        if all(abs(target["bounds"][0] - index * expected) <= 3
               and abs(target["bounds"][2] - (index + 1) * expected) <= 3
               for index, target in enumerate(row)):
            matches.append(row[-1])
    return matches[0] if len(matches) == 1 else None


class Workflow:
    def __init__(self, serial: str, timeout: float = 30):
        self.serial = serial
        self.timeout = timeout
        self.timings: dict[str, float] = {}
        self.deadline = time.monotonic() + timeout

    def ensure_time(self) -> None:
        if time.monotonic() >= self.deadline:
            raise Blocked("timeout", "The per-account deadline expired; no further action was attempted.")

    def _timed(self, phase: str, callback):
        started = time.monotonic()
        try:
            return callback()
        finally:
            self.timings[phase] = self.timings.get(phase, 0.0) + max(0.0, time.monotonic() - started)

    @staticmethod
    def pending_login(observation: android.Observation) -> str | None:
        matches = []
        for node in observation.nodes:
            match = re.fullmatch(r"Logging in as (.+?)(?:…|\.\.\.)?", node.label().strip())
            if match:
                matches.append(match.group(1).strip())
        if len(matches) > 1:
            raise Blocked("unsupported_ui", "Multiple account-login indicators are visible; no action was taken.")
        return matches[0] if matches else None

    def wait_pending_login(self, requested: str, observation: android.Observation) -> android.Observation:
        pending = self.pending_login(observation)
        if pending is None:
            return observation
        if normalize(pending) != normalize(requested):
            raise Blocked("identity_mismatch", f"Facebook is logging in as {pending!r}, not requested account {requested!r}; no location was read.")

        def finished(obs: android.Observation) -> bool:
            active = self.pending_login(obs)
            if active is None:
                return True
            if normalize(active) != normalize(requested):
                raise Blocked("identity_mismatch", f"Facebook changed its pending login to {active!r}; no location was read.")
            return False

        return self.wait(finished, f"Facebook is still logging in as {requested!r}.")

    def observe(self) -> android.Observation:
        self.ensure_time()
        observation = self._timed("hierarchy_reads", lambda: android.snapshot(self.serial))
        check_screen(observation)
        return observation

    def tap(self, observation: android.Observation, label: str) -> None:
        self.ensure_time()
        matches = candidates(observation, label)
        if len(matches) != 1:
            raise Blocked("unsupported_ui", f"Expected one {label!r} control; found {len(matches)}. Inspect android_read with include_screenshot=true.")
        android.tap(self.serial, *matches[0]["centred"])

    def wait(self, predicate: Callable[[android.Observation], bool], detail: str) -> android.Observation:
        started = time.monotonic()
        deadline = min(self.deadline, started + self.timeout)
        while True:
            observation = self.observe()
            if predicate(observation):
                self.timings["waits"] = self.timings.get("waits", 0.0) + max(0.0, time.monotonic() - started)
                return observation
            if time.monotonic() >= deadline:
                self.timings["waits"] = self.timings.get("waits", 0.0) + max(0.0, time.monotonic() - started)
                raise Blocked("timeout", detail + " The action was already attempted; inspect before retrying.")
            time.sleep(min(.5, max(0.0, deadline - time.monotonic())))

    def menu(self, observation: android.Observation | None = None) -> android.Observation:
        observation = observation or self.observe()
        if identity(observation):
            return observation
        # Dismiss only the observed account-sheet control, or leave an observed
        # primary-location webview. Never blindly press Back through other flows.
        if candidates(observation, "Dismiss"):
            self.tap(observation, "Dismiss")
            observation = self.observe()
            if (not identity(observation) and candidates(observation, "Dismiss")
                    and any("other accounts" in normalize(target["label"]) for target in observation.targets)):
                android.key(self.serial, android.KEYCODES["go_back"])
                observation = self.observe()
        elif "your primary location" in observation.read().casefold():
            android.key(self.serial, android.KEYCODES["go_back"])
            observation = self.observe()
        if identity(observation):
            return observation
        target = menu_target(observation)
        if target is None and self._feed_nav_hidden(observation):
            # The live feed can hide its header/nav row after scrolling. One
            # deterministic upward scroll is allowed only with both feed
            # markers and the Facebook foreground observed in the hierarchy.
            self.ensure_time()
            android.scroll(self.serial, observation.screen, "scroll_up")
            observation = self.observe()
            target = menu_target(observation)
        if not target:
            raise Blocked("unsupported_ui", "Facebook's menu is not identifiable in this layout. Inspect android_read with include_screenshot=true; do not guess taps.")
        self.ensure_time()
        android.tap(self.serial, *target["centred"])
        return self.wait(lambda obs: bool(identity(obs)), "Facebook Menu did not expose a unique active-account name.")

    @staticmethod
    def _feed_nav_hidden(observation: android.Observation) -> bool:
        if not observation.foreground.startswith(android.FACEBOOK_APP + "/"):
            return False
        return (len(candidates(observation, "Go to profile")) == 1
                and len(candidates(observation, "What's on your mind?")) == 1)

    def _open_accounts(self, observation: android.Observation | None = None) -> tuple[str, android.Observation]:
        observation = self.menu(observation)
        current = identity(observation)
        self.tap(observation, "Open profile switcher")
        observation = self.wait(lambda obs: bool(candidates(obs, "Dismiss")), "The profile switcher did not open.")
        other = [target for target in observation.targets if target["label"] in
                 ("Other accounts", "Other accountsRed dot with new notifications")]
        if len(other) == 1:
            android.tap(self.serial, *other[0]["centred"])
            observation = self.wait(lambda obs: "Other accounts" in obs.read().splitlines()
                                    and not any(target["label"] in ("Other accounts", "Other accountsRed dot with new notifications")
                                                for target in obs.targets), "The saved-account list did not open.")
        elif other:
            raise Blocked("unsupported_ui", "The Other accounts control is ambiguous.")
        return current, observation

    @staticmethod
    def _account_cards(observation: android.Observation) -> list[tuple[str, dict]]:
        return [(account_name(target["label"]), target) for target in observation.targets
                if target["role"] == "viewgroup" and target["label"]]

    @staticmethod
    def _picker_scroller(observation: android.Observation):
        scrollables = [node for node in observation.nodes if node.scrollable and node.bounds]
        return scrollables[0] if len(scrollables) == 1 else None

    @staticmethod
    def _scrollable_nodes(observation: android.Observation):
        return [node for node in observation.nodes if node.scrollable and node.bounds]

    def _scroll_picker(self, observation: android.Observation) -> android.Observation:
        self.ensure_time()
        scroller = self._picker_scroller(observation)
        if scroller is None:
            raise Blocked("unsupported_ui", "The account picker has no unique observed scroll container.")
        android.scroll_region(self.serial, scroller.bounds, "scroll_down")
        return self.observe()

    def _leave_picker(self, observation: android.Observation) -> android.Observation:
        dismiss = candidates(observation, "Dismiss")
        if len(dismiss) != 1:
            raise Blocked("unsupported_ui", "The saved-account sheet has no unique Dismiss control; it remains open.")
        self.tap(observation, "Dismiss")
        result = self.observe()
        if not identity(result):
            if candidates(result, "Dismiss"):
                android.key(self.serial, android.KEYCODES["go_back"])
                result = self.observe()
            if (not identity(result) and candidates(result, "Dismiss")
                    and (candidates(result, "Other accounts") or any(
                        "other accounts" in normalize(target["label"]) for target in result.targets))):
                android.key(self.serial, android.KEYCODES["go_back"])
                result = self.observe()
            if not identity(result):
                raise Blocked("unsupported_ui", "The account picker did not return to a verified Menu after bounded recovery.")
        return result

    def accounts(self) -> tuple[str, list[str], android.Observation]:
        current, observation = self._open_accounts()
        names = [account_name(target["label"]) for target in observation.targets
                 if target["role"] == "viewgroup" and target["label"]]
        if not names or normalize(current) not in {normalize(name) for name in names}:
            raise Blocked("unsupported_ui", "The saved-account list could not be verified against the active account.")
        seen = {normalize(name): name for name in names}
        previous_visible = {normalize(name) for name in names}
        if len(previous_visible) != len(names):
            raise Blocked("account_ambiguous", "Duplicate account names are visible in the picker; enumeration is ambiguous.")
        previous_signature = None
        scrollables = self._scrollable_nodes(observation)
        if len(scrollables) > 1:
            raise Blocked("unsupported_ui", "The picker contains multiple scroll containers; its list cannot be bounded safely.")
        scroller = bool(scrollables)
        for _ in range(30):
            signature = tuple((normalize(name), target["bounds"]) for name, target in self._account_cards(observation))
            if len({name for name, _ in self._account_cards(observation)}) != len(self._account_cards(observation)):
                raise Blocked("account_ambiguous", "Duplicate account names are visible in the picker; enumeration is ambiguous.")
            if not scroller:
                break
            next_observation = self._scroll_picker(observation)
            next_cards = self._account_cards(next_observation)
            next_signature = tuple((normalize(name), target["bounds"]) for name, target in next_cards)
            next_visible = [normalize(name) for name, _ in next_cards]
            if len(next_visible) != len(set(next_visible)):
                raise Blocked("account_ambiguous", "Duplicate account names are visible in the picker; enumeration is ambiguous.")
            for name, _ in next_cards:
                if normalize(name) in seen and normalize(name) not in previous_visible:
                    raise Blocked("account_ambiguous", "A duplicate account name reappeared outside the scroll overlap; enumeration is ambiguous.")
                seen.setdefault(normalize(name), name)
            if next_signature == signature or (not next_signature and not signature):
                text = next_observation.read().casefold()
                if not any(marker in text for marker in ("log into another account", "add another account", "add account")):
                    raise Blocked("unsupported_ui", "The account list stopped moving without an observed end marker; completeness is unknown.")
                observation = next_observation
                break
            if previous_signature == next_signature:
                raise Blocked("unsupported_ui", "The account list made no progress before its end could be confirmed.")
            previous_signature, previous_visible, observation = signature, set(next_visible), next_observation
        else:
            raise Blocked("unsupported_ui", "The account list exceeded the bounded scroll limit; completeness is unknown.")
        # Duplicate names at distinct card positions are ambiguous, while a
        # repeated card in a scroll overlap is naturally deduplicated.
        names = list(seen.values())
        self._leave_picker(observation)
        return current, names, observation

    def select(self, requested: str) -> str:
        observation = self.menu()
        current = identity(observation)
        if normalize(current) == normalize(requested):
            return current
        current, observation = self._open_accounts(observation)
        seen: set[str] = set()
        previous_signature = None
        selected = None
        for _ in range(30):
            matches = [(name, target) for name, target in self._account_cards(observation)
                       if normalize(name) == normalize(requested)]
            if len(matches) > 1:
                raise Blocked("account_ambiguous", "The requested saved card appears more than once; no account was selected.")
            if matches:
                selected = matches[0]
                break
            seen.update(normalize(name) for name, _ in self._account_cards(observation))
            scrollables = self._scrollable_nodes(observation)
            if len(scrollables) > 1:
                raise Blocked("unsupported_ui", "The picker contains multiple scroll containers; selection stopped safely.")
            if not scrollables:
                break
            signature = tuple((normalize(name), target["bounds"]) for name, target in self._account_cards(observation))
            next_observation = self._scroll_picker(observation)
            next_signature = tuple((normalize(name), target["bounds"]) for name, target in self._account_cards(next_observation))
            if next_signature == signature or next_signature == previous_signature:
                break
            previous_signature, observation = signature, next_observation
        if selected is None:
            raise Blocked("account_not_found", f"Requested account {requested!r} was not observed in the bounded picker scan; no account was selected.")
        actual_name, target = selected
        self.ensure_time()
        android.tap(self.serial, *target["centred"])
        # The same activity may host the whole login transition. Wait for the
        # spinner to leave the hierarchy, not for the activity name to change.
        observation = self.wait(lambda obs: not any(node.label().startswith("Logging in as ") for node in obs.nodes)
                                and not candidates(obs, "Dismiss"), "Account switching did not finish.")
        observation = self.menu(observation)
        actual = identity(observation)
        if normalize(actual) != normalize(actual_name) or normalize(actual) != normalize(requested):
            raise Blocked("identity_mismatch", f"Requested {requested!r}, but Facebook Menu identifies {actual!r}. No location was attributed.")
        return actual


def run(serial: str, *, action: str, account: str | None = None, timeout: float = 30,
        deadline: float | None = None) -> dict:
    started = time.monotonic()
    result = {"device": serial, "action": action, "state": "unknown"}
    workflow = Workflow(serial, timeout)
    total_deadline = min(deadline, started + timeout) if deadline is not None else started + timeout
    workflow.deadline = total_deadline
    timings: dict[str, float | None] = {"device_discovery": None, "hierarchy_reads": 0.0, "screenshots": None,
               "switcher_navigation": 0.0, "selection": 0.0, "login_wait": 0.0,
               "location_open_and_poll": 0.0, "identity_verification": 0.0,
               "model_round_trip": None}
    try:
        if action == "accounts":
            began = time.monotonic()
            current, names, _ = workflow.accounts()
            timings["switcher_navigation"] += max(0.0, time.monotonic() - began)
            result.update(state="accounts", active_account=current, accounts=names)
        elif action in ("location", "current_location"):
            if not account or not account.strip():
                raise ValueError(f"account is required for action={action}")
            result["requested_account"] = account
            began = time.monotonic()
            if action == "current_location":
                # Resume a delayed login without replaying a possibly completed
                # selection. Verify the currently active profile before reading.
                observed = workflow.observe()
                observed = workflow.wait_pending_login(account, observed)
                current_menu = workflow.menu(observed)
                actual = identity(current_menu)
                timings["identity_verification"] += max(0.0, time.monotonic() - began)
                if normalize(actual) != normalize(account):
                    raise Blocked("identity_mismatch", f"Facebook Menu identifies {actual!r}, not requested account {account!r}; no location was read.")
            else:
                actual = workflow.select(account)
                timings["selection"] += max(0.0, time.monotonic() - began)
            timings["login_wait"] += workflow.timings.get("waits", 0.0)
            began = time.monotonic()
            remaining = max(0.0, total_deadline - time.monotonic() - 5.0)
            found = android.account_location(serial, timeout=remaining)
            timings["location_open_and_poll"] += max(0.0, time.monotonic() - began)
            result["location_timing"] = {key: round(value, 3) for key, value in found.timings.items()}
            result["location_retries"] = found.retries
            result.update(state=found.state, account=actual)
            if found.state == "location":
                # Close the page and verify that the session still belongs to the
                # account we selected. A location is exposed only after both checks.
                began = time.monotonic()
                after = identity(workflow.menu())
                timings["identity_verification"] += max(0.0, time.monotonic() - began)
                if normalize(after) != normalize(actual):
                    raise Blocked("identity_mismatch", "The active account changed during the location read; no location was attributed.")
                result.update(location=found.location, identity_verified=True)
            else:
                result["detail"] = found.describe()
        else:
            raise ValueError("action must be accounts, location, or current_location")
    except Blocked as exc:
        result.update(state=exc.state, detail=exc.detail)
        result.pop("location", None)
    except android.AdbError as exc:
        result.update(state="device_error", detail=str(exc))
    result["seconds"] = round(time.monotonic() - started, 2)
    result["deadline_seconds"] = round(max(0.0, total_deadline - started), 2)
    timings["hierarchy_reads"] += workflow.timings.get("hierarchy_reads", 0.0)
    result["timings"] = {key: round(value, 3) if value is not None else None
                          for key, value in timings.items()}
    result["timing_note"] = "Phase durations may overlap; device discovery, screenshots, and provider/model round-trip are outside this workflow timer."
    return result
