"""What we've already seen, so each email only flags what's actually new."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def empty_seen() -> dict:
    return {"courses": {}, "modules": {}, "discussions": {}, "grades": {}}


class State:
    """Two separate "seen" baselines: one for the daily digest, one for the 2-hourly alerts.
    That way an afternoon alert doesn't stop the same item from showing up in the evening digest."""

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path) -> "State":
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("seen", empty_seen())
        # Alerts start from wherever the digest is, so switching them on doesn't re-announce old things.
        data.setdefault("alert_seen", copy.deepcopy(data["seen"]))
        data.setdefault("warned", {})  # event id -> due time, for "due soon, not submitted" alerts
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
    def alert_seen(self) -> dict:
        return self.data["alert_seen"]

    @alert_seen.setter
    def alert_seen(self, value: dict) -> None:
        self.data["alert_seen"] = value

    @property
    def warned(self) -> dict:
        return self.data["warned"]

    @property
    def gcal(self) -> dict:
        return self.data["gcal"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


@contextmanager
def run_lock(state_file: Path, wait_seconds: int = 900, stale_seconds: int = 1800):
    """Only one run at a time (e.g. the 8 PM digest and a check overlapping). Waits for the other."""
    lock = state_file.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait_seconds
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_seconds:  # left by a crashed run
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise TimeoutError(f"another run is still going ({lock})")
            time.sleep(5)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)
