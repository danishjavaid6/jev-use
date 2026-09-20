"""Ask Jev typed questions about a page, and get typed answers back.

`browser_read` returns a page's raw text, which for a dashboard is 10-20 KB of noise
that the harness model then has to summarize into prose. This asks the questions
instead, and returns values.

It is also more accurate than summarizing: Jev cannot generate a number it did not
read, so a figure in the answer came from the page. The three primitives map onto
what a dashboard question actually needs:

    choice   which plan is this / which account is selected
    score    how much of the quota is used
    noul     is this page logged out?

Every question is evaluated in parallel in one call, so asking six costs about the
same latency as asking one.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_MODEL = "jev-1.13.0"
MAX_STATE_CHARS = 60_000


class ExtractError(RuntimeError):
    """Raised when the extraction cannot be performed."""


def _build_question(spec: dict[str, Any], types: Any) -> Any:
    kind = str(spec.get("type", "")).lower()
    instructions = spec.get("instructions") or spec.get("question") or ""
    if not instructions:
        raise ExtractError(f"a question is missing 'instructions': {spec!r}")

    if kind == "choice":
        criteria = spec.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise ExtractError(
                "a choice question needs 'criteria' as an object of option -> meaning"
            )
        return types.Choice(instructions=instructions, criteria=criteria)

    if kind == "score":
        criteria = spec.get("criteria")
        if not isinstance(criteria, list) or len(criteria) < 2:
            raise ExtractError(
                "a score question needs 'criteria' as a list of at least two ordered levels"
            )
        return types.Score(instructions=instructions, criteria=criteria)

    if kind == "noul":
        return types.Noul(instructions=instructions)

    raise ExtractError(
        f"unknown question type {kind!r}; use choice, score or noul"
    )


def _answer_value(answer: Any) -> dict[str, Any]:
    """Normalise one SDK answer into plain JSON."""
    out: dict[str, Any] = {}
    for field in ("choice", "score", "noul"):
        value = getattr(answer, field, None)
        if value is not None:
            out["value"] = value
            out["type"] = field
            break
    confidence = getattr(answer, "confidence", None)
    if confidence is not None:
        out["confidence"] = round(float(confidence), 4)
    probabilities = getattr(answer, "probabilities", None)
    if probabilities:
        out["probabilities"] = probabilities
    legend = getattr(answer, "legend", None)
    if legend:
        out["legend"] = legend
    return out


def ask(
    text: str,
    questions: dict[str, dict[str, Any]],
    *,
    model: str | None = None,
    client: Any | None = None,
    max_chars: int = MAX_STATE_CHARS,
) -> dict[str, Any]:
    """Ask typed questions about `text`. Returns one entry per question.

    `questions` maps a caller-chosen name to a spec:

        {"total":   {"type": "score",  "instructions": "...", "criteria": [...]},
         "logged_out": {"type": "noul", "instructions": "..."},
         "plan":    {"type": "choice", "instructions": "...", "criteria": {...}}}
    """
    if not questions:
        raise ExtractError("no questions given")
    if not text or not text.strip():
        raise ExtractError("the page returned no text to ask about")

    try:
        from typesafe_sdk import Choice, Noul, Score, TypeSafeClient
    except ImportError as exc:  # pragma: no cover - depends on env
        raise ExtractError(
            "typesafe-sdk is not installed. Run: uv add typesafe-sdk"
        ) from exc

    namespace = type("_T", (), {"Choice": Choice, "Score": Score, "Noul": Noul})
    built = {name: _build_question(spec, namespace) for name, spec in questions.items()}

    if client is None:
        client = TypeSafeClient(model=model or os.environ.get("JEV_USE_MODEL", DEFAULT_MODEL))

    try:
        response = client.system_one(state=text[:max_chars], questions=built)
    except Exception as exc:  # noqa: BLE001 - surface provider failures as one type
        raise ExtractError(f"Jev call failed: {type(exc).__name__}: {exc}") from exc

    answers = getattr(response, "answers", None)
    if answers is None:
        raise ExtractError("Jev returned no answers")

    out: dict[str, Any] = {}
    for name in questions:
        try:
            answer = answers[name]
        except (KeyError, TypeError):
            out[name] = {"error": "no answer returned"}
            continue
        out[name] = _answer_value(answer)

    return {
        "model": getattr(response, "model", None) or model or DEFAULT_MODEL,
        "chars_asked": min(len(text), max_chars),
        "answers": out,
    }
