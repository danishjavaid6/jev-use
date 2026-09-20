"""A tiny on-disk cache of winning action plans.

This is the action-caching idea from Stagehand: once a goal has been solved
against a window, the sequence of actions that solved it is worth keeping.
A repeat run then costs one snapshot and zero model calls, which is the
difference between ~7 s and ~1 s for the same task.

Plans are keyed by (window title, goal) and store labels, never element tokens,
because tokens are snapshot-scoped and meaningless on the next run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(".jev-cache.json")


class PlanCache:
    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._data: dict[str, list[dict[str, str]]] = {}
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(raw, dict):
            self._data = {k: v for k, v in raw.items() if isinstance(v, list)}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except OSError:
            pass

    @staticmethod
    def key(window_title: str, goal: str) -> str:
        return f"{window_title.strip().lower()}::{goal.strip().lower()}"

    def get(self, window_title: str, goal: str) -> list[dict[str, str]] | None:
        plan = self._data.get(self.key(window_title, goal))
        if plan:
            self.hits += 1
            return plan
        self.misses += 1
        return None

    def put(self, window_title: str, goal: str, plan: list[dict[str, str]]) -> None:
        if not plan:
            return
        self._data[self.key(window_title, goal)] = plan
        self._save()

    def drop(self, window_title: str, goal: str) -> None:
        if self._data.pop(self.key(window_title, goal), None) is not None:
            self._save()

    def __len__(self) -> int:
        return len(self._data)

    def summary(self) -> dict[str, Any]:
        return {"entries": len(self._data), "hits": self.hits, "misses": self.misses}
