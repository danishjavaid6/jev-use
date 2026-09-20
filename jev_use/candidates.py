"""Observation and candidate construction.

The client — not the model — decides what actions are possible. Every candidate
here is a complete, executable Cua Driver action. The chooser only ever picks
one by id.

Design rule that matters most: candidates must be *mutually exclusive*. Jev's
`confidence` measures how concentrated the probability distribution is, so two
options that mean the same thing always read as doubt. Duplicate role+label
elements are therefore disambiguated by screen position instead of being
offered as near-identical choices.
"""

from __future__ import annotations

from typing import Any

MAX_CANDIDATES = 120  # Jev Choice supports 255; we stay well clear
DEFAULT_MAX_ELEMENTS = 400
DEFAULT_MAX_DEPTH = 12
ANCESTOR_PATH_LIMIT = 3

# AT-SPI actions that actually activate an element. An element that only offers
# e.g. `set_value` or `focus` is not a click target, so offering it as one makes
# the choice set larger and less meaningful than it needs to be.
ACTIVATION_ACTIONS = frozenset(
    {
        "press",
        "click",
        "activate",
        "activate_default",
        "jump",
        "open",
        "pick",
        "confirm",
        "toggle",
        "select",
        "expand_or_contract",
    }
)

KINDS = {
    "click_element": "Activate exactly one of the listed elements",
    "press_key": "Send one named key to the focused element",
    "scroll_down": "The target is further down the page, scroll to reveal it",
    "scroll_up": "The target is further up the page, scroll to reveal it",
    "wait": "The screen is still loading or animating",
    "done": "The goal is already satisfied by what is visible now",
    "impossible": "Nothing available can make progress toward the goal",
}

KEYS = {
    "return": "Confirm the current field or dialog",
    "escape": "Dismiss the current dialog or menu",
    "tab": "Move focus to the next control",
    "down": "Move the selection down",
    "up": "Move the selection up",
}


def available_operations(observation: "Observation") -> dict[str, str]:
    """Only offer operations this observation can actually carry out.

    An operation with no legal target is noise: it enlarges the choice set and
    invites a selection the client would have to refuse.
    """
    operations = dict(KINDS)
    if not observation.targets:
        operations.pop("click_element", None)
    return operations


class Observation:
    """One window snapshot, reduced to what the decision needs."""

    def __init__(
        self,
        pid: int,
        window_id: int,
        title: str,
        elements: list[dict[str, Any]],
        bounds: dict[str, Any] | None = None,
    ) -> None:
        self.pid = pid
        self.window_id = window_id
        self.title = title
        self.elements = elements
        self.bounds = bounds or {}
        self.candidates = build_candidates(elements, self.bounds)

    def by_id(self, candidate_id: str) -> dict[str, Any] | None:
        for candidate in self.candidates:
            if candidate["id"] == candidate_id:
                return candidate
        return None

    @property
    def targets(self) -> list[dict[str, Any]]:
        """Elements that can actually be activated — the click_target option set."""
        return [c for c in self.candidates if c["clickable"]]

    def option_map(self) -> dict[str, str]:
        """Full table handed to the model as state: everything observed."""
        return {c["id"]: c["description"] for c in self.candidates}

    def target_map(self) -> dict[str, str]:
        """Narrow option set for the click_target question: compatible only."""
        return {c["id"]: c["description"] for c in self.targets}

    def operations(self) -> dict[str, str]:
        """Operations this observation can actually carry out."""
        return available_operations(self)

    def target_heads(self) -> dict[str, dict[str, str]]:
        """One option set per operation that takes a target.

        The community rule that this encodes: each target head contains only
        elements compatible with its operation. One shared list would let the model
        pick a text field for a click, and padding an option set with illegal
        choices is exactly what makes a confidence score unreadable.
        """
        return {"click_target": self.target_map()}

    def targets_for(self, operation: str) -> list[dict[str, Any]]:
        """The candidates legal for one operation."""
        if operation == "click_element":
            return self.targets
        return []

    def readouts(self, limit: int = 24) -> list[str]:
        """Readable text the window is currently showing.

        This is the "what does the window say" signal, and it is what closes the
        loop for a model that decides one action at a time: a calculator cannot
        tell that its last click landed unless the display is visible in state.

        Excludes only *activation targets* (buttons and the like) rather than
        every candidate. A display field is frequently a candidate — it supports
        clipboard, selection and focus actions — while being the single most
        important thing the model needs to see.
        """
        controls = {c["element_index"] for c in self.targets}
        out: list[str] = []
        for element in self.elements:
            if element.get("element_index") in controls:
                continue
            text = element.get("value") or element.get("label")
            if text is None:
                continue
            text = str(text).strip()
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def to_payload(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "window_id": self.window_id,
            "window_title": self.title,
            "window_bounds": self.bounds,
            "elements": self.elements,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Observation":
        return cls(
            pid=payload["pid"],
            window_id=payload["window_id"],
            title=payload.get("window_title", "") or payload.get("app_name", ""),
            elements=payload.get("elements", []),
            bounds=payload.get("window_bounds"),
        )


def observe(driver: Any, pid: int, window_id: int, with_screenshot: bool = False) -> Observation:
    """Snapshot one window. Default is tree-only: no image ever leaves the box."""
    arguments: dict[str, Any] = {
        "pid": pid,
        "window_id": window_id,
        "include_screenshot": with_screenshot,
        "max_elements": DEFAULT_MAX_ELEMENTS,
        "max_depth": DEFAULT_MAX_DEPTH,
    }
    result = driver.call("get_window_state", arguments)
    payload = result.json() or {}

    elements = payload.get("elements", [])
    if not elements and isinstance(payload.get("structuredContent"), dict):
        elements = payload["structuredContent"].get("elements", [])

    return Observation(
        pid=pid,
        window_id=window_id,
        title=payload.get("window_title", "") or payload.get("app_name", ""),
        elements=elements,
        bounds=payload.get("window_bounds"),
    )


def build_candidates(
    elements: list[dict[str, Any]], bounds: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Turn raw AX elements into executable, mutually exclusive candidates.

    Each candidate carries the three fields that let a model choose well:
    *what* (role + label), *where* (ancestor path) and *supports* (which actions
    the element actually exposes). A model that can see `supports` picks the
    operation and the target together instead of guessing one then the other.
    """
    actionable = [e for e in elements if e.get("actions")]
    if not actionable:
        return []

    actionable = actionable[:MAX_CANDIDATES]
    by_index = {
        e["element_index"]: e for e in elements if e.get("element_index") is not None
    }

    seen: dict[str, int] = {}
    for element in actionable:
        seen[describe(element)] = seen.get(describe(element), 0) + 1

    candidates: list[dict[str, Any]] = []
    for element in actionable:
        token = element.get("element_token")
        if not token:
            continue

        actions = list(element.get("actions") or [])
        base = describe(element)
        where = ancestor_path(element, by_index)
        supports = ", ".join(actions)

        description = base
        if seen[base] > 1:
            # Keep the option set mutually exclusive: a duplicate label with no
            # positional hint splits the vote and reads as model uncertainty.
            hint = position_hint(element.get("frame"), bounds)
            if hint:
                description = f"{base} ({hint})"
            else:
                description = f"{base} (element {element.get('element_index')})"
        if where:
            description += f" - in {where}"
        kind_hint = label_class(element.get("label") or "")
        if kind_hint:
            description += f" - {kind_hint}"
        if supports:
            description += f" - supports {supports}"

        candidates.append(
            {
                "id": token,
                "kind": "click_element",
                "role": element.get("role", ""),
                "label": element.get("label", ""),
                "value": element.get("value"),
                "actions": actions,
                "frame": element.get("frame"),
                "element_index": element.get("element_index"),
                "where": where,
                "supports": supports,
                "clickable": bool(ACTIVATION_ACTIONS.intersection(actions)),
                "description": description,
            }
        )
    return candidates


def ancestor_path(
    element: dict[str, Any],
    by_index: dict[int, dict[str, Any]],
    limit: int = ANCESTOR_PATH_LIMIT,
) -> str:
    """A short `where` string: nearest named ancestors, outermost first."""
    names: list[str] = []
    visited: set[int] = set()
    start = element.get("element_index")
    if start is not None:
        # Seed with the element itself so a tree that loops back to its own node
        # terminates instead of walking the cycle until the limit.
        visited.add(start)
    current = element
    while len(names) < limit:
        parent_id = current.get("parent_index")
        if parent_id is None or parent_id in visited:
            break
        visited.add(parent_id)
        parent = by_index.get(parent_id)
        if parent is None:
            break
        name = (parent.get("label") or parent.get("role") or "").strip()
        if name:
            names.append(name)
        current = parent
    return " > ".join(reversed(names))


def label_class(label: str) -> str:
    """A coarse character-class hint for short labels.

    Found by the hard test suite: asked for `9 times 6`, the chooser picked π —
    `button "9"` and `button "π"` are both one glyph in a grid, and nothing in the
    option text says one is a digit and the other is a constant. Naming the class
    is generic (it reads the label, not the app) and removes the ambiguity.
    """
    text = (label or "").strip()
    if not text or len(text) > 2:
        return ""
    if text.isdigit():
        return "digit"
    if text in {"+", "-", "\u2212", "*", "/", "\u00d7", "\u00f7", "=", "^", "%"}:
        return "operator"
    # ASCII only: str.isalpha() is True for Greek, so π would be called a "letter"
    # and the hint would actively mislead.
    if text.isascii() and text.isalpha():
        return "letter"
    return "symbol"


def describe(element: dict[str, Any]) -> str:
    role = (element.get("role") or "element").strip()
    label = (element.get("label") or "").strip()
    value = element.get("value")

    if not label and value:
        label = str(value).strip()
    if not label:
        return f"{role} (unlabelled)"

    text = f'{role} "{label}"'
    if value and str(value).strip() and str(value).strip() != label:
        text += f" currently showing {str(value).strip()!r}"
    return text


def position_hint(frame: dict[str, Any] | None, bounds: dict[str, Any] | None) -> str:
    """A coarse, stable spatial descriptor used only to break label ties."""
    if not frame or not bounds:
        return ""
    try:
        cx = frame["x"] + frame.get("w", 0) / 2
        cy = frame["y"] + frame.get("h", 0) / 2
        width = bounds.get("width") or bounds.get("w") or 0
        height = bounds.get("height") or bounds.get("h") or 0
        if not width or not height:
            return ""
        horizontal = "left" if cx < width / 3 else "right" if cx > 2 * width / 3 else "centre"
        vertical = "top" if cy < height / 3 else "bottom" if cy > 2 * height / 3 else "middle"
        return f"{vertical}-{horizontal}"
    except (KeyError, TypeError, ZeroDivisionError):
        return ""
