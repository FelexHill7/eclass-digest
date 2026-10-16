"""What we've already seen, so each email only flags what's actually new."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def empty_seen() -> dict:
    return {"courses": {}, "modules": {}, "discussions": {}}


class State:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path) -> "State":
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("seen", empty_seen())
        data.setdefault("gcal", {"calendar_id": None, "events": {}})
        data.setdefault("last_run", None)
        return cls(path, data)

    @property
    def seen(self) -> dict:
        return self.data["seen"]

    @seen.setter
    def seen(self, value: dict) -> None:
        self.data["seen"] = value

    @property
    def gcal(self) -> dict:
        return self.data["gcal"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
