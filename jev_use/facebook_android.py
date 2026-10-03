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
    """Recognize the two bottom tab bars observed on the connected Facebook app.

    No absolute coordinates or screen-size assumptions. Require a complete,
    evenly spaced, same-size row of five/six native tabs in the bottom quarter.
    Its last tab was visually verified as Menu in both variants. Unknown layouts
    fail closed, and opening it must produce a verified self-profile header.
    """
    width, height = observation.screen
    rows: dict[tuple[int, int], list[dict]] = {}
    for target in observation.targets:
        left, top, right, bottom = target["bounds"]
        if (target["package"] == android.FACEBOOK_APP and target["role"] == "view"
                and not target["label"] and top >= height * .75
                and right > left and bottom > top):
            rows.setdefault((top, bottom), []).append(target)
    matches = []
    for row in rows.values():
        row.sort(key=lambda target: target["bounds"][0])
        if len(row) not in (5, 6):
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

    def observe(self) -> android.Observation:
        observation = android.snapshot(self.serial)
        check_screen(observation)
        return observation

    def tap(self, observation: android.Observation, label: str) -> None:
        matches = candidates(observation, label)
        if len(matches) != 1:
            raise Blocked("unsupported_ui", f"Expected one {label!r} control; found {len(matches)}. Inspect android_read with include_screenshot=true.")
        android.tap(self.serial, *matches[0]["centred"])

    def wait(self, predicate: Callable[[android.Observation], bool], detail: str) -> android.Observation:
        deadline = time.monotonic() + self.timeout
        while True:
            observation = self.observe()
            if predicate(observation):
                return observation
            if time.monotonic() >= deadline:
                raise Blocked("timeout", detail + " The action was already attempted; inspect before retrying.")
            time.sleep(.5)

    def menu(self) -> android.Observation:
        observation = self.observe()
        if identity(observation):
            return observation
        # Dismiss only the observed account-sheet control, or leave an observed
        # primary-location webview. Never blindly press Back through other flows.
        if candidates(observation, "Dismiss"):
            self.tap(observation, "Dismiss")
            observation = self.observe()
        elif "your primary location" in observation.read().casefold():
            android.key(self.serial, android.KEYCODES["go_back"])
            observation = self.observe()
        if identity(observation):
            return observation
        target = menu_target(observation)
        if not target:
            raise Blocked("unsupported_ui", "Facebook's menu is not identifiable in this layout. Inspect android_read with include_screenshot=true; do not guess taps.")
        android.tap(self.serial, *target["centred"])
        return self.wait(lambda obs: bool(identity(obs)), "Facebook Menu did not expose a unique active-account name.")

    def accounts(self) -> tuple[str, list[str], android.Observation]:
        observation = self.menu()
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
        names = [account_name(target["label"]) for target in observation.targets
                 if target["role"] == "viewgroup" and target["label"]]
        if not names or normalize(current) not in {normalize(name) for name in names}:
            raise Blocked("unsupported_ui", "The saved-account list could not be verified against the active account.")
        if len({normalize(name) for name in names}) != len(names):
            raise Blocked("account_ambiguous", "Multiple saved cards have the same name; select the intended account manually.")
        # Do not truncate a scrollable picker and call it the complete list.
        if any(node.scrollable for node in observation.nodes):
            raise Blocked("unsupported_ui", "The account list is scrollable; only visible accounts were observed. Inspect it before claiming all accounts were listed.")
        return current, names, observation

    def select(self, requested: str) -> str:
        observation = self.menu()
        current = identity(observation)
        if normalize(current) == normalize(requested):
            return current
        _, names, observation = self.accounts()
        matched = [name for name in names if normalize(name) == normalize(requested)]
        if len(matched) != 1:
            raise Blocked("account_not_found", f"Requested account {requested!r} is not in the observed saved-account list.")
        target = [target for target in observation.targets
                  if normalize(account_name(target["label"])) == normalize(matched[0])]
        if len(target) != 1:
            raise Blocked("account_ambiguous", "The requested saved card is ambiguous; no account was selected.")
        android.tap(self.serial, *target[0]["centred"])
        # The same activity may host the whole login transition. Wait for the
        # spinner to leave the hierarchy, not for the activity name to change.
        self.wait(lambda obs: not any(node.label().startswith("Logging in as ") for node in obs.nodes)
                  and not candidates(obs, "Dismiss"), "Account switching did not finish.")
        observation = self.menu()
        actual = identity(observation)
        if normalize(actual) != normalize(requested):
            raise Blocked("identity_mismatch", f"Requested {requested!r}, but Facebook Menu identifies {actual!r}. No location was attributed.")
        return actual


def run(serial: str, *, action: str, account: str | None = None, timeout: float = 30) -> dict:
    started = time.monotonic()
    result = {"device": serial, "action": action, "state": "unknown"}
    workflow = Workflow(serial, timeout)
    try:
        if action == "accounts":
            current, names, _ = workflow.accounts()
            result.update(state="accounts", active_account=current, accounts=names)
        elif action == "location":
            if not account or not account.strip():
                raise ValueError("account is required for action=location; use action=accounts first")
            result["requested_account"] = account
            actual = workflow.select(account)
            found = android.account_location(serial, timeout=timeout)
            result.update(state=found.state, account=actual)
            if found.state == "location":
                # Close the page and verify that the session still belongs to the
                # account we selected. A location is exposed only after both checks.
                after = identity(workflow.menu())
                if normalize(after) != normalize(actual):
                    raise Blocked("identity_mismatch", "The active account changed during the location read; no location was attributed.")
                result.update(location=found.location, identity_verified=True)
            else:
                result["detail"] = found.describe()
        else:
            raise ValueError("action must be accounts or location")
    except Blocked as exc:
        result.update(state=exc.state, detail=exc.detail)
        result.pop("location", None)
    except android.AdbError as exc:
        result.update(state="device_error", detail=str(exc))
    result["seconds"] = round(time.monotonic() - started, 2)
    return result
