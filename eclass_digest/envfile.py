"""Read and update the project's .env file (used by the setup scripts)."""

from __future__ import annotations

from .config import ROOT

ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


def ensure_env() -> None:
    if not ENV.exists():
        ENV.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")


def read_env() -> dict[str, str]:
    ensure_env()
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def set_env(key: str, value: str) -> None:
    """Set KEY=value in .env, replacing an existing line or appending a new one."""
    ensure_env()
    lines = ENV.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
