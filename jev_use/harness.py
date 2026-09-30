"""browser-harness transport: one persistent CDP websocket, no per-call spawn.

Why this exists. Driving a page through the driver's `page` tool is one
`cua-driver mcp` process per call: spawn, discovery, a `list_windows` bind, the CDP
call, then teardown. A harness that follows `browser_use` with `browser_read` and
`browser_extract` pays all of that three times for one page.

browser-harness (Browser Use, MIT) is the fix, and this module adopts its
transport: `cdp-use`, the library browser-harness is built on and pins. Its
`CDPClient` holds ONE CDP websocket and multiplexes every command over it, so an
action costs a frame on an open socket instead of a process.

We deliberately do not run browser-harness's daemon. That daemon exists so that
many *short-lived CLI invocations* can share one connection; this MCP server is
already a single long-lived process, so the daemon would add a socket hop and a
lifecycle to supervise for no gain. The transport is the part that mattered.

It also lands GoLogin where it belongs. Orbita (GoLogin's Chromium) exposes CDP on
a port GoLogin picks at random, and browser-harness resolves exactly this shape
from `BU_CDP_URL`. `Harness(port=…)` is the same idea: point it at the port
`gologin.launch()` returned and the session is the profile's real browser, with its
fingerprint, proxy and cookies untouched. We only ever ATTACH — GoLogin launches.

Two deliberate omissions keep the CDP surface as small as it can be, because every
domain we enable is another signal:

* no `Runtime.enable`. It is the documented CDP leak (it makes an object's
  `.stack` getter fire on a debugger-driven browser and not on a normal one) and
  we do not need it — `Runtime.evaluate` works without it.
* no `Emulation.*` and no `Page.addScriptToEvaluateOnNewDocument`, ever. Each
  would overwrite a value GoLogin set, which is the one way this transport could
  actually damage the profile's identity.

`cdp-use` is async-only, so the connection lives on one event loop in a background
thread and every call is bridged with `run_coroutine_threadsafe`. Imports are lazy
so the dependency stays optional.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

from .driver import DriverError

#: Page targets whose URL starts with these are not pages we can drive.
_INTERNAL = (
    "chrome://",
    "chrome-untrusted://",
    "devtools://",
    "chrome-extension://",
    "about:",
)

#: The handshake and the first `Target.getTargets` include a cold import of the
#: generated CDP library, so the default is generous. Ordinary calls are
#: milliseconds; a wedged one must not hang the tool until the harness gives up.
DEFAULT_TIMEOUT = 20.0

INSTALL_HINT = (
    "the browser-harness transport is not installed. Install it with:\n"
    "    pip install 'jev-use[harness]'\n"
    "(or: pip install browser-harness, which depends on the same client)"
)


class HarnessError(DriverError):
    """Anything that stops the harness connecting or answering.

    A `DriverError` subclass on purpose: every caller already resets a failed
    browser session and reports the message on `DriverError`, and this is the same
    kind of failure — the transport died — so it must take the same path.
    """


def available() -> bool:
    """Whether the transport is importable (not whether a browser answers)."""
    try:
        import cdp_use.client  # noqa: F401
    except Exception:
        return False
    return True


def ws_url_for(port: int, timeout: float = 5.0) -> str:
    """The websocket debugger URL behind a CDP port, via `/json/version`.

    The same resolution browser-harness does for `BU_CDP_URL`: the port alone is
    not the websocket (its path carries a per-instance UUID), and only the browser
    can hand that back.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HarnessError(
            f"port {port} is not answering /json/version, so there is no CDP "
            f"endpoint to attach to ({exc})."
        ) from exc
    url = (payload or {}).get("webSocketDebuggerUrl")
    if not url:
        raise HarnessError(f"port {port} answered /json/version without a webSocketDebuggerUrl")
    return str(url)


def _default_client_factory(url: str) -> Any:
    from cdp_use.client import CDPClient  # imported late: the dependency is optional

    return CDPClient(url)


def _evaluate_value(response: Any) -> Any:
    """The value out of a CDP `Runtime.evaluate` result.

    An exception thrown by the page is a real failure and is raised, not swallowed:
    a script that threw has not told us anything about the page, and reporting its
    absence as "the page is empty" is the bug that hides itself.
    """
    if not isinstance(response, dict):
        return response
    details = response.get("exceptionDetails")
    if details:
        description = (
            (details.get("exception") or {}).get("description")
            or details.get("text")
            or "the page script raised"
        )
        raise HarnessError(f"page script failed: {str(description).strip()[:200]}")
    result = response.get("result")
    if not isinstance(result, dict):
        return ""
    if "value" in result:
        return result["value"]
    if "unserializableValue" in result:
        return result["unserializableValue"]
    return ""


def _as_text(value: Any) -> str:
    """Normalise an evaluated value to the text envelope `browser` already parses.

    Every script this engine sends returns `JSON.stringify(...)`, so the value is
    normally already the JSON text. Anything else (a number, an object Chrome
    managed to serialise) is re-encoded so `_parse_js_payload` still sees text.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)


class Harness:
    """A persistent CDP session against one browser, backed by `cdp-use`.

    Quacks like the parts of `Driver` that `browser.py` uses: `start()`, `alive`,
    `close()`, and `evaluate(javascript, url_hint=…)` in place of
    `call("page", …)`. `browser._js` dispatches on `evaluate` existing, so nothing
    else needs to know which transport it was handed.
    """

    def __init__(
        self,
        port: int,
        *,
        url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.port = int(port)
        self._url = url
        self._timeout = float(timeout)
        self._client_factory = client_factory
        if client_factory is None and not available():
            raise HarnessError(INSTALL_HINT)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._dead = False
        #: url_hint -> CDP session id. Keyed by hint because one connection serves
        #: several tabs (`browser_read_many` gives each worker its own), and
        #: `target_url_contains` is how a caller says which one a call reaches.
        self._sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Harness":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> "Harness":
        if self._client is not None:
            return self
        ws_url = self._url or ws_url_for(self.port)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, args=(self._loop,), daemon=True,
            name=f"jev-harness-{self.port}",
        )
        self._thread.start()
        factory = self._client_factory or _default_client_factory
        self._client = factory(ws_url)
        try:
            self._submit(self._client.start())
        except Exception as exc:
            self.close()
            raise HarnessError(
                f"could not attach to port {self.port} over CDP ({exc})."
            ) from exc
        return self

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _submit(self, coro: Any) -> Any:
        """Run one coroutine on the loop thread and wait for it."""
        if self._loop is None:
            raise HarnessError("harness is not started; call start() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError as exc:
            # `concurrent.futures.TimeoutError`, not the builtin: they are only the
            # same class from 3.11, and this project supports 3.10.
            future.cancel()
            self._dead = True
            raise HarnessError(
                f"the browser on port {self.port} did not answer within "
                f"{self._timeout:g}s; the session was dropped."
            ) from exc

    @property
    def alive(self) -> bool:
        return (
            self._client is not None
            and not self._dead
            and self._thread is not None
            and self._thread.is_alive()
        )

    def close(self) -> None:
        client, self._client = self._client, None
        loop, self._loop = self._loop, None
        if client is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(client.stop(), loop).result(timeout=5)
            except Exception:
                pass
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        with self._lock:
            self._sessions.clear()

    # -- page work ---------------------------------------------------------

    def evaluate(self, javascript: str, url_hint: str | None = None) -> str:
        """Run `javascript` in the target tab and return its value as text.

        The transport-compatible face of `Driver.call("page", …)`. `url_hint`
        selects the tab, exactly as `Target.url_hint` does for the driver.
        """
        value = self._submit(self._evaluate(javascript, url_hint))
        return _as_text(value)

    async def _evaluate(self, javascript: str, url_hint: str | None) -> Any:
        try:
            session_id = await self._session_for(url_hint)
            response = await self._client.send_raw(
                "Runtime.evaluate",
                {"expression": javascript, "returnByValue": True, "awaitPromise": True},
                session_id=session_id,
            )
        except HarnessError:
            raise
        except Exception as exc:
            self._dead = True
            raise HarnessError(
                f"the CDP session on port {self.port} failed: {exc}"
            ) from exc
        return _evaluate_value(response)

    def click_at(self, x: float, y: float, url_hint: str | None = None) -> None:
        """Click at viewport coordinates through the browser's input pipeline.

        This is the real thing: CDP `Input.dispatchMouseEvent` enters the browser
        where a mouse would, so the events the page receives are trusted. A
        synthetic `element.click()` from `Runtime.evaluate` is not — it yields
        `isTrusted: false`, and sites that check that (file inputs, drag/drop,
        payment and upload widgets) ignore it entirely. Every other automation
        library clicks this way; the DOM click is the fallback, not the default.
        """
        self._submit(self._click_at(x, y, url_hint))

    async def _click_at(self, x: float, y: float, url_hint: str | None) -> None:
        try:
            session_id = await self._session_for(url_hint)
            for event_type in ("mousePressed", "mouseReleased"):
                await self._client.send_raw(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": x,
                        "y": y,
                        "button": "left",
                        "clickCount": 1,
                    },
                    session_id=session_id,
                )
        except HarnessError:
            raise
        except Exception as exc:
            self._dead = True
            raise HarnessError(
                f"the CDP session on port {self.port} failed while clicking: {exc}"
            ) from exc

    async def _session_for(self, url_hint: str | None) -> str:
        """The flattened CDP session for a tab, attaching on first use.

        An empty hint means "the page we are driving" — the first real page — and
        is cached under "". A non-empty hint matches a page by URL substring, which
        is how `browser_read_many` pins a worker to the tab it opened.
        """
        key = url_hint or ""
        with self._lock:
            cached = self._sessions.get(key)
        if cached:
            return cached

        targets = (await self._client.send_raw("Target.getTargets"))["targetInfos"]
        pages = [t for t in targets if t.get("type") == "page"]
        matched = None
        if url_hint:
            matched = next((t for t in pages if url_hint in (t.get("url") or "")), None)
        chosen = matched or next(
            (t for t in pages if not (t.get("url") or "").startswith(_INTERNAL)), None
        )
        if chosen is None:
            chosen = pages[0] if pages else None
        if chosen is None:
            raise HarnessError(f"no page target to attach to on port {self.port}")

        session_id = str(
            (
                await self._client.send_raw(
                    "Target.attachToTarget", {"targetId": chosen["targetId"], "flatten": True}
                )
            )["sessionId"]
        )
        # Only remember a tab we can trust. A hint that matched nothing yet fell back
        # to another page, and caching that would pin this worker to the wrong tab for
        # the rest of the run — so a later probe must be free to re-resolve it.
        if not url_hint or matched is not None:
            with self._lock:
                self._sessions[key] = session_id
        return session_id
