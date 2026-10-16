from __future__ import annotations

import json

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials

from .config import Config

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleAuthMissing(RuntimeError):
    pass


def is_service_account(creds) -> bool:
    return isinstance(creds, service_account.Credentials)


def load_credentials(cfg: Config):
    # A service account (robot account) needs no consent screen and never expires,
    # but can only write to calendars shared with it and can't send Gmail.
    if cfg.google_service_account_json:
        return service_account.Credentials.from_service_account_info(
            json.loads(cfg.google_service_account_json), scopes=CALENDAR_SCOPES)
    if cfg.google_service_account_file.exists():
        return service_account.Credentials.from_service_account_file(
            str(cfg.google_service_account_file), scopes=CALENDAR_SCOPES)

    if cfg.google_token_json:  # GitHub Actions: token passed in as a secret
        creds = Credentials.from_authorized_user_info(json.loads(cfg.google_token_json), SCOPES)
        from_file = False
    elif cfg.google_token_file.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.google_token_file), SCOPES)
        from_file = True
    else:
        raise GoogleAuthMissing(
            "No Google access set up. Add service_account.json (see README) "
            "or run: python scripts/google_login.py")

    if not creds.valid:
        if not creds.refresh_token:
            raise GoogleAuthMissing("Google sign-in expired. Run: python scripts/google_login.py")
        creds.refresh(Request())
        if from_file:
            cfg.google_token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds
