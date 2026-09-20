"""The two jobs Jev structurally cannot do: plan, and write text.

Jev returns typed decisions and never generates strings. That is the point, and it
leaves exactly two gaps this module fills:

* **decompose** a compound goal into an ordered list of unambiguous subgoals. Jev
  decides one action at a time from the current state, so "compute 7 times 8" is
  hard for it (it pressed `7, 8, ×, =`) while "press 7" is trivial. This is the
  accuracy fix, and it is what the shipping projects do — paulsmith splits compound
  goals first, and vinilana's harness turns goals into "verifiable subgoals".
* **write** a string for a text field, because typing requires generation.

Both go through one OpenAI-compatible endpoint so the same config works with
OpenRouter, Ollama, vLLM, llama.cpp, OpenAI, or anything else that speaks the
protocol. Stdlib only — no new dependency.

Deliberately optional. With no model configured both methods return nothing useful
and the caller degrades gracefully: `decompose` returns the goal unchanged and
`write` refuses. The browser remains fully usable for click-and-read work.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 30.0
MAX_SUBGOALS = 8

DECOMPOSE_SYSTEM = (
    "You split a browser task into the shortest ordered list of unambiguous steps. "
    "Each step must be a single action a person could take without thinking: "
    "'click the Sign in link', 'type the email address', 'scroll down'. "
    "Never merge two actions into one step. Never invent a step the task does not "
    "imply. Never include a step that types a secret you were not given. "
    "Reply with ONLY a JSON array of strings, nothing else."
)

WRITE_SYSTEM = (
    "You write the exact text to enter into one web form field. "
    "Reply with ONLY a JSON object of the form {\"text\": \"...\"} or {\"text\": null} "
    "if the field should not be filled (for example a password, a credit card, or a "
    "field you cannot answer from the goal). Never explain. Never invent credentials."
)


class TextUnavailable(RuntimeError):
    """Raised when a text job is asked for but no model is configured."""


class TextModel:
    """A minimal OpenAI-compatible chat client for planning and writing."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        reasoning: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("JEV_USE_TEXT_MODEL", "")
        self.base_url = (
            base_url or os.environ.get("JEV_USE_TEXT_BASE_URL", "https://openrouter.ai/api/v1")
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("JEV_USE_TEXT_API_KEY", "")
        # Some models emit reasoning tokens that wreck strict JSON parsing; the
        # jev-ultrafast demo disables them for exactly this reason.
        self.reasoning = reasoning or os.environ.get("JEV_USE_TEXT_REASONING", "")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.model)

    def _chat(self, system: str, user: str) -> str:
        if not self.available:
            raise TextUnavailable(
                "no text model configured. Set JEV_USE_TEXT_MODEL (and "
                "JEV_USE_TEXT_BASE_URL / JEV_USE_TEXT_API_KEY for a hosted provider) "
                "to enable planning and typing."
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 700,
        }
        if self.reasoning:
            body["reasoning"] = {"effort": self.reasoning}

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key or 'not-needed'}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.URLError as exc:
            raise TextUnavailable(f"text model unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TextUnavailable(f"text model returned non-JSON: {exc}") from exc

        try:
            return payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise TextUnavailable(f"unexpected text model response shape: {exc}") from exc

    # -- jobs ---------------------------------------------------------------

    def decompose(self, goal: str, page_summary: str = "") -> list[str]:
        """Split a goal into ordered subgoals. Returns [goal] if unavailable.

        Failing open is deliberate: a browser task that could work without a plan
        should not be blocked by a missing planner.
        """
        if not self.available:
            return [goal]
        prompt = f"Task: {goal}\n"
        if page_summary:
            prompt += f"\nWhat the page currently shows:\n{page_summary[:1200]}\n"
        try:
            raw = self._chat(DECOMPOSE_SYSTEM, prompt)
            steps = _parse_string_array(raw)
        except TextUnavailable:
            return [goal]

        steps = [s.strip() for s in steps if s and s.strip()]
        # A single step adds a round trip and no information.
        if len(steps) < 2:
            return [goal]
        return steps[:MAX_SUBGOALS]

    def write(self, goal: str, field: str, page_summary: str = "") -> str | None:
        """The string to type into one field, or None to refuse."""
        if not self.available:
            return None
        prompt = f"Task: {goal}\nField: {field}\n"
        if page_summary:
            prompt += f"\nWhat the page currently shows:\n{page_summary[:800]}\n"
        try:
            raw = self._chat(WRITE_SYSTEM, prompt)
            value = _parse_text_object(raw)
        except TextUnavailable:
            # Fail closed: a malformed reply means type NOTHING. Never guess a
            # value into a form field.
            return None
        if not isinstance(value, str) or not value.strip():
            return None
        return value


def _extract_json(raw: str, opener: str, closer: str) -> Any:
    """Pull the first JSON value out of a reply that may be fenced or chatty."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find(opener), text.rfind(closer)
    if start == -1 or end == -1 or end < start:
        raise TextUnavailable(f"no JSON {opener}{closer} found in the reply")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TextUnavailable(f"reply was not valid JSON: {exc}") from exc


def _parse_string_array(raw: str) -> list[str]:
    value = _extract_json(raw, "[", "]")
    if not isinstance(value, list):
        raise TextUnavailable("expected a JSON array of steps")
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _parse_text_object(raw: str) -> Any:
    value = _extract_json(raw, "{", "}")
    if not isinstance(value, dict):
        raise TextUnavailable("expected a JSON object")
    return value.get("text")
