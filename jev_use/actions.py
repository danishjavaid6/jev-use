"""Exact one-step browser actions; Jev remains the ambiguous-goal chooser."""
from . import browser
from .driver import DriverError


def perform(session, page, action: str, label: str = "", text: str | None = None) -> str:
    if action in ("click", "fill"):
        if not label.strip():
            raise DriverError("label is required; use the exact visible control label")
        if action == "fill" and text is None:
            raise DriverError("text is required for fill")
        view = browser.snapshot(session, page, label, can_write=action == "fill")
        candidates = view.targets_for("type_text" if action == "fill" else "click_element")
        normal = lambda value: " ".join(value.split()).casefold()
        matches = [item for item in candidates if normal(item["label"]) == normal(label)]
        if len(matches) != 1:
            raise DriverError(
                f"{len(matches)} controls match {label!r}; nothing was changed. "
                "Use browser_use with a goal describing the intended control."
            )
        ref = matches[0]["id"]
        if action == "fill":
            browser.type_into(session, page, ref, text)
        else:
            browser.click(session, page, int(ref))
    elif action in ("scroll_down", "scroll_up"):
        delta = browser.SCROLL_PIXELS if action == "scroll_down" else -browser.SCROLL_PIXELS
        browser._js(session, page, browser.scroll_js(delta))
    elif action == "back":
        browser._js(session, page, "history.back(); JSON.stringify({ok:true})")
    else:
        raise DriverError(f"unknown action {action!r}")
    content = browser.read(session, page)
    return f"acted={action} port={page.port} url={page.url}\n\n{content[:4000]}"
