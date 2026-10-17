from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, "").strip() or default)
    return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class Config:
    eclass_url: str
    eclass_token: str
    course_classification: str
    exclude_courses: tuple[str, ...]
    include_courses: tuple[str, ...]

    digest_to: str
    email_method: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    send_if_nothing_new: bool
    notify_calendar_changes: bool
    due_soon_hours: int
    morning_send_if_empty: bool
    include_full_index: bool

    ai_enabled: bool
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]

    google_client_secrets: Path
    google_token_file: Path
    google_token_json: str
    google_service_account_file: Path
    google_service_account_json: str
    gcal_calendar_name: str
    gcal_calendar_id: str
    lookahead_days: int
    deadline_window_days: int
    timezone: str

    download_files: bool
    download_dir: Path

    state_file: Path
    output_dir: Path
    log_dir: Path


def load_config() -> Config:
    # Real environment variables win over .env (that's how GitHub Actions secrets get in).
    load_dotenv(ROOT / ".env")

    gemini_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    ai_setting = os.getenv("AI_SUMMARY", "auto").strip().lower()
    ai_enabled = bool(gemini_key) if ai_setting == "auto" else ai_setting in ("1", "true", "yes", "on")
    fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-2.5-flash")

    def _list(name: str) -> tuple[str, ...]:
        return tuple(s.strip().lower() for s in os.getenv(name, "").split(",") if s.strip())

    exclude = _list("EXCLUDE_COURSES")

    return Config(
        eclass_url=os.getenv("ECLASS_URL", "https://eclass.yorku.ca").rstrip("/"),
        eclass_token=os.getenv("ECLASS_TOKEN", "").strip(),
        course_classification=os.getenv("COURSE_CLASSIFICATION", "inprogress").strip(),
        exclude_courses=exclude,
        include_courses=_list("INCLUDE_COURSES"),
        digest_to=os.getenv("DIGEST_TO", "").strip(),
        email_method=os.getenv("EMAIL_METHOD", "smtp").strip().lower(),
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=os.getenv("SMTP_USER", ""),
        # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
        smtp_password=os.getenv("SMTP_PASSWORD", "").replace(" ", ""),
        email_from=os.getenv("EMAIL_FROM", "") or os.getenv("SMTP_USER", ""),
        send_if_nothing_new=_bool("SEND_IF_NOTHING_NEW", True),
        notify_calendar_changes=_bool("NOTIFY_CALENDAR_CHANGES", True),
        due_soon_hours=_int("DUE_SOON_HOURS", 24),
        morning_send_if_empty=_bool("MORNING_SEND_IF_EMPTY", False),
        include_full_index=_bool("INCLUDE_FULL_INDEX", True),
        ai_enabled=ai_enabled,
        gemini_api_key=gemini_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
        gemini_fallback_models=tuple(m.strip() for m in fallbacks.split(",") if m.strip()),
        google_client_secrets=_path("GOOGLE_CLIENT_SECRETS", "credentials.json"),
        google_token_file=_path("GOOGLE_TOKEN_FILE", "token.json"),
        google_token_json=os.getenv("GOOGLE_TOKEN_JSON", "").strip(),
        google_service_account_file=_path("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"),
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
        gcal_calendar_name=os.getenv("GCAL_CALENDAR_NAME", "YorkU eClass").strip(),
        gcal_calendar_id=os.getenv("GCAL_CALENDAR_ID", "").strip(),
        lookahead_days=_int("LOOKAHEAD_DAYS", 120),
        deadline_window_days=_int("DEADLINE_WINDOW_DAYS", 14),
        timezone=os.getenv("TIMEZONE", "America/Toronto").strip(),
        download_files=_bool("DOWNLOAD_FILES", False),
        download_dir=_path("DOWNLOAD_DIR", "downloads"),
        state_file=_path("STATE_FILE", "state/state.json"),
        output_dir=_path("OUTPUT_DIR", "output"),
        log_dir=_path("LOG_DIR", "logs"),
    )
