"""Tests for the browser-harness transport and the routing that selects it.

The transport is exercised against an injected async client, so nothing here needs
`cdp-use` (or a browser) to be installed — which is also what keeps the dependency
optional in production.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from jev_use import browser, harness, mcp_server
from jev_use.driver import DriverError


class FakeHarness:
    """The `evaluate` face of the transport, with no socket behind it."""

    def __init__(self, value: str = "{}") -> None:
        self.value = value
        self.calls: list[tuple[str, str | None]] = []

    def evaluate(self, javascript: str, url_hint: str | None = None) -> str:
        self.calls.append((javascript, url_hint))
        return self.value


class FakeCDP:
    """An async stand-in for `cdp_use.client.CDPClient`."""

    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.pages = pages if pages is not None else [
            {"targetId": "T1", "type": "page", "url": "https://one.test/a"},
            {"targetId": "T2", "type": "page", "url": "https://two.test/b"},
        ]
        self.started = False
        self.stopped = False
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.value: Any = '{"ok":true}'
        self.exception: dict[str, Any] | None = None

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_raw(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {}), session_id))
        if method == "Target.getTargets":
            return {"targetInfos": self.pages}
        if method == "Target.attachToTarget":
            return {"sessionId": f"S-{(params or {})['targetId']}"}
        if method == "Runtime.evaluate":
            if self.exception is not None:
                return {"exceptionDetails": self.exception}
            return {"result": {"type": "string", "value": self.value}}
        if method.startswith("Input."):
            return {}  # input dispatch has no result payload
        raise AssertionError(f"unexpected CDP method {method}")


def started(fake: FakeCDP) -> harness.Harness:
    """A started Harness whose connection is `fake` (no `cdp-use` involved)."""
    return harness.Harness(
        53142,
        url="ws://127.0.0.1:53142/devtools/browser/x",
        client_factory=lambda _url: fake,
    ).start()


def methods(fake: FakeCDP) -> list[str]:
    return [name for name, _params, _session in fake.calls]


# -- the optional dependency ------------------------------------------------


def test_available_matches_whether_the_client_imports() -> None:
    try:
        import cdp_use.client  # noqa: F401
    except Exception:
        assert harness.available() is False
    else:
        assert harness.available() is True


def test_a_harness_error_is_a_driver_error() -> None:
    """The session cache resets on `DriverError`, so a dead transport must be one."""
    assert issubclass(harness.HarnessError, DriverError)


@pytest.mark.skipif(harness.available(), reason="the CDP client is installed here")
def test_a_missing_client_names_the_install() -> None:
    with pytest.raises(harness.HarnessError) as excinfo:
        harness.Harness(53142)
    assert "jev-use[harness]" in str(excinfo.value)


def test_an_injected_client_needs_no_optional_dependency() -> None:
    """The seam that makes this testable without the dependency installed."""
    connection = FakeCDP()
    assert started(connection) is not None
    assert connection.started is True


# -- resolving the websocket -------------------------------------------------


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_ws_url_for_reads_the_debugger_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        harness.urllib.request,
        "urlopen",
        lambda url, timeout=0: FakeResponse(
            {"webSocketDebuggerUrl": "ws://127.0.0.1:53142/devtools/browser/abc"}
        ),
    )
    assert harness.ws_url_for(53142).endswith("/devtools/browser/abc")


def test_ws_url_for_explains_an_unreachable_port(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, timeout: float = 0) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(harness.urllib.request, "urlopen", boom)
    with pytest.raises(harness.HarnessError) as excinfo:
        harness.ws_url_for(53142)
    assert "53142" in str(excinfo.value)


# -- evaluating --------------------------------------------------------------


def test_evaluate_attaches_the_first_real_page_and_returns_its_value() -> None:
    fake = FakeCDP()
    fake.value = '{"url":"https://one.test/a"}'
    session = started(fake)

    assert session.evaluate("script") == '{"url":"https://one.test/a"}'
    assert methods(fake) == ["Target.getTargets", "Target.attachToTarget", "Runtime.evaluate"]
    assert fake.calls[-1][2] == "S-T1", "the eval must run on the attached session"
    assert fake.calls[-1][1]["returnByValue"] is True


def test_the_tab_session_is_attached_once_and_reused() -> None:
    fake = FakeCDP()
    session = started(fake)

    session.evaluate("a")
    session.evaluate("b")

    assert methods(fake).count("Target.attachToTarget") == 1
    assert methods(fake).count("Target.getTargets") == 1


def test_a_url_hint_selects_that_tab() -> None:
    """`browser_read_many` pins each worker to the tab it opened, by URL."""
    fake = FakeCDP()
    session = started(fake)

    session.evaluate("script", url_hint="https://two.test/b")
    assert fake.calls[-1][2] == "S-T2"


def test_a_hint_that_matches_nothing_is_not_cached_to_the_fallback() -> None:
    """The tab may still be being created; caching the fallback would pin this
    worker to the wrong page for the rest of the run."""
    fake = FakeCDP()
    session = started(fake)

    session.evaluate("script", url_hint="https://not-open-yet.test/")
    assert fake.calls[-1][2] == "S-T1", "it falls back for this call"

    # The tab arrives; the next call must resolve to it, not reuse the fallback.
    fake.pages.append({"targetId": "T3", "type": "page", "url": "https://not-open-yet.test/"})
    session.evaluate("script", url_hint="https://not-open-yet.test/")
    assert fake.calls[-1][2] == "S-T3"


def test_an_internal_page_is_never_the_default_target() -> None:
    fake = FakeCDP(
        [
            {"targetId": "C1", "type": "page", "url": "chrome://newtab"},
            {"targetId": "T9", "type": "page", "url": "https://real.test/"},
        ]
    )
    session = started(fake)

    session.evaluate("script")
    assert fake.calls[-1][2] == "S-T9"


def test_click_at_sends_real_mouse_events() -> None:
    """`Input.dispatchMouseEvent` is what makes the click trusted; the events a page
    receives cannot be told from a mouse's."""
    fake = FakeCDP()
    session = started(fake)

    session.click_at(12.5, 40, url_hint=None)

    pressed = [c for c in fake.calls if c[0] == "Input.dispatchMouseEvent"]
    assert [c[1]["type"] for c in pressed] == ["mousePressed", "mouseReleased"]
    assert (pressed[0][1]["x"], pressed[0][1]["y"]) == (12.5, 40)
    assert pressed[0][1]["button"] == "left"
    assert pressed[0][2] == "S-T1", "the click must land on the attached tab"


def test_a_page_exception_is_reported_not_swallowed() -> None:
    """A script that threw has told us nothing; reporting it as an empty page is the
    bug that hides itself."""
    fake = FakeCDP()
    fake.exception = {"text": "Uncaught TypeError: boom"}
    session = started(fake)

    with pytest.raises(harness.HarnessError) as excinfo:
        session.evaluate("bad()")
    assert "boom" in str(excinfo.value)
    assert "page script failed" in str(excinfo.value)


def test_a_wedged_call_gives_up_and_retires_the_session() -> None:
    """A call that never answers must not hang the tool until the harness gives up —
    a page-owned dialog is the observed cause of exactly that."""

    class Wedged(FakeCDP):
        async def send_raw(self, method, params=None, session_id=None):
            if method == "Runtime.evaluate":
                await asyncio.sleep(30)
            return await super().send_raw(method, params, session_id)

    session = harness.Harness(
        53142, url="ws://x", timeout=0.2, client_factory=lambda _url: Wedged()
    ).start()

    try:
        with pytest.raises(harness.HarnessError) as excinfo:
            session.evaluate("hang()")
        assert "did not answer" in str(excinfo.value)
        assert session.alive is False, "a dead session must not be reused"
    finally:
        session.close()


def test_closing_stops_the_client_and_retires_the_session() -> None:
    fake = FakeCDP()
    session = started(fake)

    session.close()
    assert fake.stopped is True
    assert session.alive is False


# -- value shaping -----------------------------------------------------------


def test_evaluate_value_returns_the_raw_value() -> None:
    assert harness._evaluate_value({"result": {"value": "hi"}}) == "hi"
    assert harness._evaluate_value({"result": {"unserializableValue": "NaN"}}) == "NaN"
    assert harness._evaluate_value({}) == ""


def test_as_text_keeps_strings_and_re_encodes_anything_else() -> None:
    assert harness._as_text('{"ok":true}') == '{"ok":true}'
    assert json.loads(harness._as_text({"a": 1})) == {"a": 1}


# -- routing -----------------------------------------------------------------


def test_js_routes_by_whichever_transport_is_present() -> None:
    transport = FakeHarness('{"ok":true}')
    target = browser.Target(port=53142, pid=0, window_id=0, url_hint="https://x.test/p")

    assert browser._js(transport, target, "script") == '{"ok":true}'
    assert transport.calls == [("script", "https://x.test/p")]

    class FakeDriver:
        def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
            class R:
                text = 'cdp.runtime.evaluate: "{}"'

            return R()

    assert browser._js(FakeDriver(), browser.Target(port=1, pid=1, window_id=2), "s") == (
        'cdp.runtime.evaluate: "{}"'
    )


def test_attach_skips_the_window_bind_for_a_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness holds the CDP connection, so there is no `list_windows` to do —
    which is what makes a GoLogin profile drivable where no window can be bound."""
    monkeypatch.setattr(browser, "running_profiles", lambda: [])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: port == 53142)

    class FakeTransport:
        def evaluate(self, javascript: str, url_hint: str | None = None) -> str:
            raise AssertionError("attach must not run page code")

    target = browser.attach(FakeTransport(), port=53142)
    assert (target.port, target.pid, target.window_id) == (53142, 0, 0)


# -- selecting the transport -------------------------------------------------

GOLOGIN = browser.Profile(
    pid=9, profile_dir="/tmp/gologin_x", port=53142, vendor="gologin", name="Acme"
)
CHROME = browser.Profile(pid=1, profile_dir="/home/h/.config/google-chrome", port=9222)


def test_the_harness_is_chosen_only_for_an_antidetect_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server.harness, "available", lambda: True)
    monkeypatch.delenv("JEV_USE_TRANSPORT", raising=False)
    monkeypatch.setattr(mcp_server, "_GOLOGIN", {"session": None}, raising=False)
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: [GOLOGIN, CHROME])

    assert mcp_server._harness_wanted(53142) is True
    assert mcp_server._harness_wanted(9222) is False, "Chrome keeps its existing path"
    assert mcp_server._harness_wanted(None) is False


def test_the_transport_can_be_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.harness, "available", lambda: True)

    monkeypatch.setenv("JEV_USE_TRANSPORT", "driver")
    assert mcp_server._harness_wanted(53142) is False

    monkeypatch.setenv("JEV_USE_TRANSPORT", "harness")
    assert mcp_server._harness_wanted(53142) is True
    assert mcp_server._harness_wanted(None) is False


def test_forcing_the_harness_without_the_client_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server.harness, "available", lambda: False)
    monkeypatch.setenv("JEV_USE_TRANSPORT", "harness")

    with pytest.raises(harness.HarnessError):
        mcp_server._harness_wanted(53142)


def test_a_started_session_prefers_the_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class FakeTransport:
        def __init__(self, port: int) -> None:
            seen["port"] = port

        def start(self) -> None:
            seen["started"] = True

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(mcp_server, "_harness_wanted", lambda port: True)
    monkeypatch.setattr(mcp_server.harness, "Harness", FakeTransport)
    monkeypatch.setattr(
        mcp_server, "attach",
        lambda session, port=None, profile=None: browser.Target(port=port, pid=0, window_id=0),
    )

    session, target = mcp_server._start_session(None, 53142)
    assert isinstance(session, FakeTransport)
    assert seen["started"] is True
    assert target.port == 53142
