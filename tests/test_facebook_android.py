"""Exercise full account flows with a phone state machine, no live mutations."""
import html

import pytest

from jev_use import android, facebook_android as fb


def node(label="", *, cls="android.widget.Button", bounds=(20, 200, 1000, 290), clickable=True, **attrs):
    attributes = {"class": cls, "text": label, "package": android.FACEBOOK_APP,
                  "bounds": f"[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]",
                  "clickable": str(clickable).lower(), "enabled": "true", **attrs}
    return "<node " + " ".join(f'{key}="{html.escape(value, quote=True)}"' for key, value in attributes.items()) + " />"


def screen(nodes):
    return android.Observation("S", android.parse_nodes("<hierarchy>" + "".join(nodes) + "</hierarchy>"),
                               (1080, 2160), android.FACEBOOK_APP + "/.Main")


class Phone:
    def __init__(self, monkeypatch, tabs=6):
        self.current = "Afiza Parween"
        self.names = ["Afiza Parween", "Tahir Shah", "Name, With Comma"]
        self.state = "feed"
        self.tabs = tabs
        self.actions = []
        self.tick = 0
        self.login_reads = 0
        self.freeze_login = False
        self.wrong_account = False
        self.after_account = None
        self.scrollable = False
        monkeypatch.setattr(android, "snapshot", self.snapshot)
        monkeypatch.setattr(android, "tap", self.tap)
        monkeypatch.setattr(android, "key", self.back)
        monkeypatch.setattr(android, "account_location", self.location)
        monkeypatch.setattr(fb.time, "monotonic", lambda: self.tick)
        monkeypatch.setattr(fb.time, "sleep", lambda seconds: None)

    def observation(self):
        if self.state == "feed":
            size = 1080 // self.tabs
            return screen([node(cls="android.view.View", bounds=(i * size, 1920, (i + 1) * size, 2060),
                                **{"resource-id": "app:id/nav"}) for i in range(self.tabs)])
        if self.state == "menu":
            return screen([node(self.current + ", see your profile", clickable=False),
                           node("Open profile switcher")])
        if self.state == "switcher":
            return screen([node("Dismiss", bounds=(0, 0, 100, 100)), node(self.current, cls="android.view.ViewGroup"),
                           node("Other accountsRed dot with new notifications", bounds=(20, 600, 1000, 690))])
        if self.state == "accounts":
            nodes = [node("Dismiss", bounds=(0, 0, 100, 100)), node("Other accounts", clickable=False)]
            nodes += [node(name + (", 19 notifications" if name != self.current else ""), cls="android.view.ViewGroup",
                           bounds=(20, 400 + i * 150, 1000, 500 + i * 150)) for i, name in enumerate(self.names)]
            if self.scrollable:
                nodes.append(node(clickable=False, scrollable="true", bounds=(0, 300, 1080, 2000)))
            return screen(nodes)
        if self.state == "logging":
            return screen([node("Logging in as " + self.current + "…", clickable=False)])
        if self.state == "location":
            return screen([node("Your Primary Location", clickable=False)])
        return screen([node(self.state, clickable=False)])

    def snapshot(self, serial):
        self.tick += 1
        if self.state == "logging" and not self.freeze_login:
            self.login_reads += 1
            if self.login_reads >= 3:
                self.state = "feed"
        return self.observation()

    def tap(self, serial, x, y):
        observation = self.observation()
        targets = [target for target in observation.targets if target["centred"] == (x, y)]
        assert len(targets) == 1
        label = targets[0]["label"]
        self.actions.append(label or "menu-tab")
        if self.state == "feed":
            self.state = "menu"
        elif label == "Dismiss":
            self.state = "menu"
        elif label == "Open profile switcher":
            self.state = "switcher"
        elif label.startswith("Other accounts"):
            self.state = "accounts"
        else:
            if not self.wrong_account:
                self.current = fb.account_name(label)
            self.state = "logging"

    def back(self, serial, code):
        assert code == android.KEYCODES["go_back"]
        self.actions.append("back")
        if self.after_account:
            self.current = self.after_account
        self.state = "menu"

    def location(self, serial, timeout):
        self.actions.append("location-read")
        self.state = "location"
        return android.AccountLocation(serial, "fresh-url", "Lahore, Punjab 54", "location")


@pytest.mark.parametrize("tabs", [5, 6])
def test_lists_observed_accounts_without_a_model(monkeypatch, tabs):
    phone = Phone(monkeypatch, tabs)
    result = fb.run("S", action="accounts")
    assert result["state"] == "accounts"
    assert result["accounts"] == phone.names
    assert result["active_account"] == "Afiza Parween"
    assert phone.actions == ["menu-tab", "Open profile switcher", "Other accountsRed dot with new notifications"]


def test_switches_once_waits_for_login_and_verifies_location_identity(monkeypatch):
    phone = Phone(monkeypatch)
    result = fb.run("S", action="location", account="Tahir Shah")
    assert result["state"] == "location"
    assert result["account"] == "Tahir Shah"
    assert result["location"] == "Lahore, Punjab 54"
    assert result["identity_verified"] is True
    assert phone.actions.count("Tahir Shah, 19 notifications") == 1
    assert phone.login_reads == 3
    assert phone.state == "menu"


def test_current_account_skips_switching_and_resumes_from_open_picker(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "accounts"
    result = fb.run("S", action="location", account="Afiza Parween")
    assert result["state"] == "location"
    assert phone.actions == ["Dismiss", "location-read", "back"]


def test_timeout_never_repeats_account_selection(monkeypatch):
    phone = Phone(monkeypatch)
    phone.freeze_login = True
    result = fb.run("S", action="location", account="Tahir Shah", timeout=2)
    assert result["state"] == "timeout"
    assert phone.actions.count("Tahir Shah, 19 notifications") == 1
    assert "location-read" not in phone.actions
    assert "location" not in result


@pytest.mark.parametrize("after_read", [False, True])
def test_does_not_attribute_location_to_wrong_account(monkeypatch, after_read):
    phone = Phone(monkeypatch)
    phone.wrong_account = not after_read
    phone.after_account = "Someone Else" if after_read else None
    result = fb.run("S", action="location", account="Tahir Shah")
    assert result["state"] == "identity_mismatch"
    assert "location" not in result
    assert "identity_verified" not in result
    assert ("location-read" in phone.actions) == after_read


@pytest.mark.parametrize("message,state", [("Unlock, Use fingerprint to unlock", "locked"),
                                          ("Connection lost, Tap to retry", "network_error"),
                                          ("Log into Facebook", "login")])
def test_blockers_stop_without_taps(monkeypatch, message, state):
    phone = Phone(monkeypatch)
    phone.state = message
    result = fb.run("S", action="location", account="Tahir Shah")
    assert result["state"] == state
    assert phone.actions == []


def test_unknown_nav_layout_does_not_guess_menu(monkeypatch):
    phone = Phone(monkeypatch, tabs=4)
    result = fb.run("S", action="accounts")
    assert result["state"] == "unsupported_ui"
    assert phone.actions == []


@pytest.mark.parametrize("problem,state", [("missing", "account_not_found"), ("duplicate", "account_ambiguous"),
                                          ("scrollable", "unsupported_ui")])
def test_incomplete_or_ambiguous_picker_is_not_claimed_complete(monkeypatch, problem, state):
    phone = Phone(monkeypatch)
    if problem == "duplicate":
        phone.names.append("Tahir Shah")
    if problem == "scrollable":
        phone.scrollable = True
    result = fb.run("S", action="location", account="Missing" if problem == "missing" else "Tahir Shah")
    assert result["state"] == state
    assert "location-read" not in phone.actions


def test_account_names_preserve_commas_and_strip_only_notification_suffixes():
    assert fb.account_name("Name, With Comma, 19 notifications") == "Name, With Comma"
    assert fb.account_name("Name, With Comma") == "Name, With Comma"


def test_identity_requires_menu_control_not_just_profile_like_post_text():
    observation = screen([node('Someone Else, see your profile', clickable=False)])
    assert fb.identity(observation) is None


def test_password_prompt_stops_without_filling_or_selecting(monkeypatch):
    phone = Phone(monkeypatch)
    monkeypatch.setattr(android, 'snapshot', lambda serial: screen([
        node('Enter password', cls='android.widget.EditText', password='true')]))
    result = fb.run('S', action='location', account='Tahir Shah')
    assert result['state'] == 'authentication_required'
    assert phone.actions == []
