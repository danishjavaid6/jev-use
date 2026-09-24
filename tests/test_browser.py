"""Tests for the browser-only engine and its MCP surface."""

from __future__ import annotations

import json

import pytest

from jev_use import browser, mcp_server
from jev_use.driver import DriverError
from jev_use.profiles import LocalProfile


# -- parsing ----------------------------------------------------------------


def test_snapshot_js_is_valid_javascript_shape() -> None:
    """The table script must call querySelectorAll and return JSON."""
    assert "querySelectorAll" in browser.TABLE_JS
    assert "data-jev-ref" in browser.TABLE_JS
    assert "JSON.stringify" in browser.TABLE_JS


def test_click_js_uses_an_integer_ref_never_page_text() -> None:
    js = browser.click_js(7)
    assert "'[data-jev-ref=\"7\"]'" in js
    assert "e.click()" in js


def test_click_js_refuses_a_non_integer_ref() -> None:
    """Fail closed. A ref is always one of our own integers; anything else is a bug,
    and interpolating it into the script would be an injection."""
    with pytest.raises(ValueError):
        browser.click_js("1']; alert(1); //")  # type: ignore[arg-type]


def test_parse_js_payload_handles_the_prose_envelope() -> None:
    envelope = '\u2705 Ran script:\n{"url":"https://x.test","title":"t","elements":[]}'
    payload = browser._parse_js_payload(envelope)
    assert payload["url"] == "https://x.test"


def test_parse_js_payload_handles_bare_json() -> None:
    payload = browser._parse_js_payload('{"url":"https://y.test","elements":[{"ref":0}]}')
    assert payload["url"] == "https://y.test"


def test_parse_js_payload_rejects_nonsense() -> None:
    with pytest.raises(DriverError):
        browser._parse_js_payload("no json here at all")


# -- discovery --------------------------------------------------------------


def test_running_profiles_parses_the_process_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The table now comes from `host`, so the seam is platform-neutral: the
    Windows implementation feeds the same (pid, argv) rows as `ps` does."""
    table = [
        (
            43886,
            "/opt/google/chrome/chrome --remote-debugging-port=9222 "
            "--user-data-dir=/home/h/.config/google-chrome",
        ),
        (
            43895,
            "/opt/google/chrome/chrome --type=renderer "
            "--user-data-dir=/home/h/.config/google-chrome",
        ),
        (43939, "/opt/google/chrome/chrome --type=gpu-process --user-data-dir=/x"),
        (1, "/sbin/init"),
    ]
    monkeypatch.setattr(browser, "_process_table", lambda *a, **k: table)
    profiles = browser.running_profiles()

    assert len(profiles) == 1, "renderers, gpu and unrelated processes are filtered out"
    assert profiles[0].pid == 43886
    assert profiles[0].port == 9222
    assert profiles[0].cdp is True
    assert "google-chrome" in profiles[0].profile_dir


def test_running_profiles_matches_the_real_binary_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chrome's surviving process is /opt/google/chrome/chrome, not the wrapper.

    Matching only 'google-chrome' works while the launch argv is intact and then
    silently stops — discovery must use the real path too.
    """
    monkeypatch.setattr(
        browser,
        "_process_table",
        lambda *a, **k: [
            (
                11035,
                "/opt/google/chrome/chrome --remote-debugging-port=9333 "
                "--user-data-dir=/tmp/jev-cdp-test",
            )
        ],
    )
    profiles = browser.running_profiles()
    assert len(profiles) == 1
    assert profiles[0].port == 9333
    assert profiles[0].profile_dir == "/tmp/jev-cdp-test"


def test_running_profiles_reads_a_windows_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows shape: a quoted argv[0] with spaces, `chrome.exe`, and a quoted
    --user-data-dir value. A bare `\\S+` capture would keep the quotes and the
    profile path would never match a real directory."""
    command_line = (
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        '--remote-debugging-port=9222 '
        '--user-data-dir="C:\\Users\\me\\AppData\\Local\\jev-use\\profiles\\default"'
    )
    monkeypatch.setattr(
        browser,
        "_process_table",
        lambda *a, **k: [
            (4242, command_line),
            (
                4243,
                '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                "--type=renderer",
            ),
        ],
    )
    profiles = browser.running_profiles()
    assert len(profiles) == 1, "the renderer child is filtered out"
    assert profiles[0].pid == 4242
    assert profiles[0].port == 9222
    assert profiles[0].profile_dir == "C:\\Users\\me\\AppData\\Local\\jev-use\\profiles\\default"
    assert '"' not in profiles[0].profile_dir, "the surrounding quotes are stripped"


def test_running_profiles_ignores_other_tools_named_chrome() -> None:
    """The measured bug: chrome-devtools-mcp is an MCP server, not a browser.

    Substring-matching "chrome" anywhere in the argv reported phantom profiles for
    it, which showed up as extra rows in browser_profiles and made the setup script
    refuse forever with Chrome closed.
    """
    assert browser._is_browser_process("npm exec chrome-devtools-mcp@latest") is False
    assert browser._is_browser_process("sh -c chrome-devtools-mcp") is False
    assert browser._is_browser_process(
        "node /home/h/.npm/_npx/abc/node_modules/chrome-devtools-mcp/build/index.js"
    ) is False
    assert browser._is_browser_process("pgrep -x chrome") is False


def test_running_profiles_accepts_real_browser_binaries() -> None:
    assert browser._is_browser_process("/opt/google/chrome/chrome --remote-debugging-port=9333")
    assert browser._is_browser_process("google-chrome --user-data-dir=/tmp/x")
    assert browser._is_browser_process("/usr/bin/chromium-browser")
    assert browser._is_browser_process("/snap/bin/brave-browser --foo")


def test_running_profiles_accepts_windows_binaries() -> None:
    """`chrome.exe` is the same browser as `chrome`; only the extension differs,
    and Windows quotes argv[0] whenever the path contains a space."""
    assert browser._is_browser_process(
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        "--remote-debugging-port=9222"
    )
    assert browser._is_browser_process(r"C:\Chrome\chrome.exe")
    assert browser._is_browser_process(
        r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe --user-data-dir=C:\x"
    )


def test_windows_devtools_helper_is_not_a_browser() -> None:
    """The phantom-profile bug, in its Windows spelling."""
    assert (
        browser._is_browser_process(
            '"C:\\Program Files\\nodejs\\node.exe" '
            "C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\chrome-devtools-mcp\\index.js"
        )
        is False
    )


def test_running_profiles_defaults_the_profile_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser, "_process_table", lambda *a, **k: [(99, "/usr/bin/google-chrome")])
    profiles = browser.running_profiles()
    assert profiles[0].profile_dir == str(browser.DEFAULT_PROFILE)
    assert profiles[0].port is None
    assert profiles[0].cdp is False


def test_cdp_alive_is_false_when_nothing_answers() -> None:
    assert browser.cdp_alive(9, timeout=0.2) is False


# -- attach -----------------------------------------------------------------


def test_attach_explains_the_fix_when_no_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "running_profiles", lambda: [])

    class FakeDriver:
        def call(self, *a, **k):
            raise AssertionError("must not touch the driver when there is no port")

    with pytest.raises(DriverError) as excinfo:
        browser.attach(FakeDriver())
    message = str(excinfo.value)
    assert "enable-cdp.sh" in message, "the error must name the remediation"
    assert "/json/version" in message or "CDP" in message


def test_attach_rejects_a_live_but_unresponsive_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: port == 1111)

    class FakeDriver:
        def call(self, *a, **k):
            raise AssertionError("must not proceed")

    with pytest.raises(DriverError):
        browser.attach(FakeDriver(), port=2222)


def test_attach_refuses_rather_than_driving_the_wrong_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured bug: a stray Chrome with a CDP port must NOT be used when the
    caller asked for their real profile, which has none."""
    real = browser.Profile(
        pid=100, profile_dir=str(browser.DEFAULT_PROFILE), port=None
    )
    stray = browser.Profile(pid=200, profile_dir="/tmp/some-other-profile", port=9999)
    monkeypatch.setattr(browser, "running_profiles", lambda: [real, stray])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: port == 9999)

    class FakeDriver:
        def call(self, *a, **k):
            raise AssertionError("must refuse before touching the driver")

    with pytest.raises(DriverError) as excinfo:
        browser.attach(FakeDriver())
    assert "without a CDP endpoint" in str(excinfo.value)
    assert "enable-cdp.sh" in str(excinfo.value)


def test_attach_honours_an_explicit_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    wanted = browser.Profile(pid=300, profile_dir="/home/h/.config/google-chrome-work", port=9223)
    default = browser.Profile(pid=100, profile_dir=str(browser.DEFAULT_PROFILE), port=None)
    monkeypatch.setattr(browser, "running_profiles", lambda: [default, wanted])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: True)

    class FakeDriver:
        def call(self, tool, args=None):
            class R:
                def json(self):
                    return {"windows": [{"pid": 300, "window_id": 7, "app_name": "Google Chrome", "title": "x"}]}

            return R()

    target = browser.attach(FakeDriver(), profile="work")
    assert target.port == 9223
    assert target.pid == 300


def test_attach_names_available_profiles_on_a_bad_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser, "running_profiles",
        lambda: [browser.Profile(pid=1, profile_dir="/home/h/.config/google-chrome", port=9222)],
    )
    with pytest.raises(DriverError) as excinfo:
        browser.attach(object(), profile="firefox-profile")  # type: ignore[arg-type]
    assert "google-chrome" in str(excinfo.value)


def test_attach_requires_a_chrome_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: True)
    monkeypatch.setattr(
        browser, "running_profiles",
        lambda: [browser.Profile(pid=1, profile_dir="/home/h/.config/google-chrome", port=9222)],
    )

    class FakeDriver:
        def call(self, tool, args=None):
            class R:
                def json(self):
                    return {"windows": [{"pid": 1, "window_id": 2, "app_name": "Files", "title": "Home"}]}

            return R()

    with pytest.raises(DriverError) as excinfo:
        browser.attach(FakeDriver(), port=9222)
    assert "no Chrome window" in str(excinfo.value)


# -- observation ------------------------------------------------------------


PAYLOAD = {
    "url": "https://example.test/page",
    "title": "Example",
    "elements": [
        {"ref": 0, "tag": "a", "text": "Learn more"},
        {"ref": 1, "tag": "input", "text": "Search"},
        {"ref": 2, "tag": "button", "text": ""},
    ],
}


def make_observation(navigate_url: str | None = None) -> browser.Observation:
    target = browser.Target(port=9222, pid=1, window_id=2)
    return browser.Observation(target, PAYLOAD, navigate_url)


def test_candidates_are_page_scoped_and_described() -> None:
    observation = make_observation()
    assert len(observation.candidates) == 3
    assert observation.candidates[0]["description"] == 'a "Learn more"'
    assert observation.candidates[2]["description"] == "button", "unlabelled falls back to tag"


def test_target_map_is_keyed_by_ref() -> None:
    assert set(make_observation().target_map()) == {"0", "1", "2"}


def test_operations_hide_click_when_the_page_offers_nothing() -> None:
    target = browser.Target(port=9222, pid=1, window_id=2)
    empty = browser.Observation(target, {"url": "u", "elements": []})
    assert "click_element" not in empty.operations()

    assert "click_element" in make_observation().operations()


def test_navigate_is_only_offered_when_the_goal_names_a_url() -> None:
    assert "navigate" not in make_observation().operations()
    assert "navigate" in make_observation("https://target.test").operations()


FIELD_PAYLOAD = {
    "url": "https://example.test/login",
    "title": "Sign in",
    "elements": [
        {"ref": 0, "tag": "a", "text": "Forgot password"},
        {"ref": 1, "tag": "input", "text": "Email", "fillable": True},
        {"ref": 2, "tag": "input", "text": "Password", "fillable": True},
        {"ref": 3, "tag": "button", "text": "Sign in"},
    ],
}


def make_field_observation(can_write: bool = True) -> browser.Observation:
    target = browser.Target(port=9222, pid=1, window_id=2)
    return browser.Observation(target, FIELD_PAYLOAD, can_write=can_write)


def test_type_text_is_withheld_without_a_writer() -> None:
    """Jev cannot generate the string, so offering the operation would be a trap."""
    assert "type_text" not in make_field_observation(can_write=False).operations()


def test_type_text_is_offered_when_a_writer_exists() -> None:
    assert "type_text" in make_field_observation(can_write=True).operations()


def test_type_text_is_withheld_when_there_is_nowhere_to_type() -> None:
    target = browser.Target(port=9222, pid=1, window_id=2)
    no_fields = browser.Observation(
        target,
        {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "link"}]},
        can_write=True,
    )
    assert "type_text" not in no_fields.operations()


def test_text_head_contains_only_fillable_elements() -> None:
    heads = make_field_observation().target_heads()
    assert set(heads["text_target"]) == {"1", "2"}, "only the two inputs"
    assert set(heads["click_target"]) == {"0", "1", "2", "3"}, "all are clickable"


def test_targets_for_returns_the_legal_set_per_operation() -> None:
    observation = make_field_observation()
    assert {c["id"] for c in observation.targets_for("type_text")} == {"1", "2"}
    assert len(observation.targets_for("click_element")) == 4


def test_type_js_encodes_the_value_rather_than_interpolating() -> None:
    js = browser.type_js(3, 'he said "hi"; alert(1)')
    assert "alert(1)" in js, "the text is present"
    assert '\\"hi\\"' in js or '\\"' in js, "but escaped, as a JSON string literal"
    assert "data-jev-ref=\"3\"" in js


def test_type_js_dispatches_the_events_frameworks_listen_for() -> None:
    """Setting .value alone is invisible to React."""
    js = browser.type_js(0, "x")
    assert "getOwnPropertyDescriptor" in js
    assert "new Event('input'" in js
    assert "new Event('change'" in js


# -- readiness --------------------------------------------------------------


class FakeJsDriver:
    """Feeds scripted snapshots so the wait logic can be tested without a browser."""

    def __init__(self, samples: list[dict]) -> None:
        self.samples = samples
        self.calls = 0

    def call(self, tool, args=None):
        sample = self.samples[min(self.calls, len(self.samples) - 1)]
        self.calls += 1

        class R:
            text = "cdp.runtime.evaluate.user_gesture: " + json.dumps(json.dumps(sample))

        return R()


def target() -> browser.Target:
    return browser.Target(port=1, pid=2, window_id=3)


def test_read_always_settles_even_on_a_large_page() -> None:
    """The measured Cloudflare bug: 1603 characters of nav chrome cleared a
    "substantial" shortcut before any billing figure had rendered. Length cannot
    tell a shell from a page."""
    shell = "Billing R2 Object Storage Storage and databases " * 40  # > 1000 chars
    full = shell + " " + ("Total due $1.41 " * 20)
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "nodes": 300, "text": shell},
            {"url": "u", "state": "complete", "nodes": 900, "text": full},
            {"url": "u", "state": "complete", "nodes": 900, "text": full},
        ]
    )
    text = browser.read(driver, target(), timeout=5)
    assert "Total due" in text, "must not return the shell just because it is long"


def test_read_keeps_waiting_while_the_page_is_still_blank() -> None:
    """The measured regression: settling on empty text returned 0 characters from
    Cloudflare instead of waiting for it to fill in."""
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "nodes": 40, "text": ""},
            {"url": "u", "state": "complete", "nodes": 40, "text": ""},
            {"url": "u", "state": "complete", "nodes": 800, "text": "billing total $1.41"},
        ]
    )
    assert "billing total" in browser.read(driver, target(), timeout=5)


def test_read_is_not_fooled_by_a_ticking_clock() -> None:
    """Text that never stops changing must not block the read: settle on the DOM."""
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "nodes": 500, "text": "page 12:00:01"},
            {"url": "u", "state": "complete", "nodes": 500, "text": "page 12:00:02"},
        ]
    )
    assert browser.read(driver, target(), timeout=5) == "page 12:00:02"


def test_read_waits_for_a_shell_to_fill_in() -> None:
    """The measured Vercel bug: a 271-char nav sidebar returned while the data table
    was still loading, and the agent blamed a stuck filter."""
    shell = "Find Projects Deployments Logs Analytics Overview Settings" * 4
    full = shell + " " + ("deployment row ready " * 100)
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "text": shell},
            {"url": "u", "state": "complete", "text": full},
        ]
    )
    text = browser.read(driver, target(), timeout=5)
    assert "deployment row ready" in text


def test_read_waits_past_a_blank_spa_shell() -> None:
    """The measured bug: Cloudflare and Vercel both read back empty."""
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "nodes": 10, "text": ""},
            {"url": "u", "state": "complete", "nodes": 200, "text": "loading"},
            {"url": "u", "state": "complete", "nodes": 900, "text": "the actual page content " * 4},
        ]
    )
    text = browser.read(driver, target(), timeout=5)
    assert "the actual page content" in text
    assert driver.calls >= 3


def test_read_returns_a_settled_short_page_rather_than_timing_out() -> None:
    """A genuinely sparse page must not cost the full timeout."""
    driver = FakeJsDriver([{"url": "u", "state": "complete", "nodes": 9, "text": "hi"}])
    assert browser.read(driver, target(), timeout=5) == "hi"
    assert driver.calls == 2, "returns once the text has settled"


def test_read_can_skip_waiting_entirely() -> None:
    driver = FakeJsDriver([{"url": "u", "state": "loading", "text": ""}])
    assert browser.read(driver, target(), wait=False) == ""
    assert driver.calls == 1


def test_readouts_lead_with_url_and_title() -> None:
    readouts = make_observation().readouts()
    assert readouts[0] == "https://example.test/page"
    assert readouts[1] == "Example"


# -- the MCP surface --------------------------------------------------------


def test_surface_is_browser_and_android_only() -> None:
    """The desktop tools were removed deliberately; they are not coming back here.

    Android is not the desktop surface returning under another name: it goes
    through adb, so it needs no window binding and no accessibility tree.
    """
    names = [t["name"] for t in mcp_server.TOOLS]
    assert names == [
        "browser_profiles",
        "browser_open",
        "browser_use",
        "browser_extract",
        "browser_read",
        "android_devices",
        "android_use",
        "android_read",
        "android_location",
    ]
    assert not any("computer_use" in n for n in names)
    assert "list_windows" not in names
    assert "get_window_state" not in names


def test_every_tool_has_a_description_and_schema() -> None:
    for tool in mcp_server.TOOLS:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_browser_profiles_is_advertised_as_the_first_call() -> None:
    tools = {t["name"]: t for t in mcp_server.TOOLS}
    assert "FIRST" in tools["browser_profiles"]["description"]


def test_browser_open_is_declared_with_the_copy_caveat() -> None:
    """The description must warn that a copy is a snapshot, not a live mirror."""
    tools = {t["name"]: t for t in mcp_server.TOOLS}
    description = tools["browser_open"]["description"]
    assert "SNAPSHOT" in description
    assert "refresh" in description, "must name the escape hatch"
    assert "default data directory" in description, "must say WHY it copies"


def test_browser_open_reports_an_unknown_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_wanted):
        raise ValueError("no profile matches 'nope'. Available: Work, Home")

    monkeypatch.setattr(mcp_server, "find_profile", boom)
    text = mcp_server.tool_browser_open({"profile": "nope"})
    assert "Available: Work, Home" in text


def test_browser_open_short_circuits_when_already_drivable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    already = LocalProfile(directory="Profile 1", name="Work", prepared=True, port=9222)
    monkeypatch.setattr(mcp_server, "find_profile", lambda _w: already)
    monkeypatch.setattr(mcp_server, "cdp_alive", lambda port, timeout=1.5: True)
    text = mcp_server.tool_browser_open({"profile": "Work"})
    assert "already open" in text


# -- caching ----------------------------------------------------------------


def test_plan_stores_descriptions_never_refs() -> None:
    """Refs are snapshot-scoped integers; a description survives a page reload."""
    payload = {
        "url": "https://x.test",
        "elements": [{"ref": 7, "tag": "a", "text": "Learn more"}],
    }
    target = browser.Target(port=1, pid=2, window_id=3)
    observation = browser.Observation(target, payload)
    from jev_use.choosers import Decision

    step = browser.Step(0, Decision(kind="click_element", element_id="7"), observation, True)
    plan = browser.plan_from(browser.Result(goal="g", steps=[step]))
    assert plan == [{"kind": "click_element", "target": 'a "Learn more"'}]
    assert "7" not in str(plan)


def test_plan_skips_unexecuted_steps() -> None:
    payload = {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "x"}]}
    target = browser.Target(port=1, pid=2, window_id=3)
    observation = browser.Observation(target, payload)
    from jev_use.choosers import Decision

    step = browser.Step(0, Decision(kind="click_element", element_id="0"), observation, False)
    assert browser.plan_from(browser.Result(goal="g", steps=[step])) == []


def test_replay_aborts_when_a_description_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale plan must stop, not guess."""
    payload = {"url": "u", "elements": [{"kind": "a", "ref": 0, "text": "something else"}]}
    monkeypatch.setattr(browser, "snapshot", lambda *a, **k: browser.Observation(
        browser.Target(port=1, pid=2, window_id=3), payload
    ))
    call = {"driver": object()}
    result = browser.replay(
        call["driver"], browser.Target(port=1, pid=2, window_id=3), "g",
        [{"kind": "click_element", "target": 'a "Learn more"'}], act=True,
    )
    assert result.outcome == "replay_miss"
    assert "no element described" in result.steps[0].note


def test_browser_use_exposes_the_decompose_switch() -> None:
    schema = {t["name"]: t for t in mcp_server.TOOLS}["browser_use"]["inputSchema"]["properties"]
    assert schema["decompose"]["default"] is True


def test_browser_use_points_at_browser_read_for_questions() -> None:
    """Routing detail that cost a real task: stepping is not reading."""
    tools = {t["name"]: t for t in mcp_server.TOOLS}
    assert "browser_read" in tools["browser_use"]["description"]


def test_defaults_are_single_sourced() -> None:
    schema = {t["name"]: t for t in mcp_server.TOOLS}["browser_use"]["inputSchema"]["properties"]
    assert schema["max_steps"]["default"] == mcp_server.DEFAULT_MAX_STEPS
    assert schema["min_confidence"]["default"] == mcp_server.DEFAULT_MIN_CONFIDENCE
    assert schema["settle"]["default"] == mcp_server.DEFAULT_SETTLE


def test_unknown_tool_is_an_error_result() -> None:
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}
    )
    assert response["result"]["isError"] is True


def test_driver_refusal_returns_as_readable_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is operational guidance, not a stack trace."""

    def refuse(_args):
        raise DriverError("No CDP endpoint found. Run scripts/enable-cdp.sh")

    monkeypatch.setitem(mcp_server.HANDLERS, "browser_use", refuse)
    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "browser_use", "arguments": {"goal": "x"}},
        }
    )
    body = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is True
    assert "enable-cdp.sh" in body
    assert "Traceback" not in body


def test_browser_profiles_reports_nothing_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: [])
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])
    text = mcp_server.tool_browser_profiles({"only_running": True})
    assert "none drivable" in text


def test_browser_profiles_names_the_route_when_no_cdp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running Chrome cannot expose CDP on its default profile, so the answer is
    browser_open, not 'try again'."""
    profile = browser.Profile(pid=1, profile_dir="/home/h/.config/google-chrome", port=None)
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: [profile])
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])
    text = mcp_server.tool_browser_profiles({})
    assert "none drivable" in text
    assert "browser_open" in text
    assert "cdp=no" in text
    assert "not answering" not in text, "no port is not the same as a dead port"


def test_browser_profiles_lists_one_line_per_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several processes share one profile; that is one profile, not three."""
    shared = "/home/h/.config/google-chrome"
    profiles = [
        browser.Profile(pid=1, profile_dir=shared, port=None),
        browser.Profile(pid=2, profile_dir=shared, port=None),
        browser.Profile(pid=3, profile_dir=shared, port=None),
    ]
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: profiles)
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])
    text = mcp_server.tool_browser_profiles({})
    assert text.count(shared) == 1


def test_browser_profiles_keeps_the_cdp_serving_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "/home/h/.config/google-chrome"
    profiles = [
        browser.Profile(pid=1, profile_dir=shared, port=None),
        browser.Profile(pid=2, profile_dir=shared, port=9222),
    ]
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: profiles)
    monkeypatch.setattr(mcp_server, "cdp_alive", lambda port, timeout=1.5: True)
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])
    text = mcp_server.tool_browser_profiles({})
    assert "pid=2" in text, "the process that actually serves CDP must be the one shown"
    assert "none drivable" not in text
