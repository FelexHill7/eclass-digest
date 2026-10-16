"""One-time: sign in with Google so the digest can add calendar events and send email.

Needs credentials.json (an OAuth "Desktop app" client) in the project folder; see README.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from eclass_digest.config import load_config  # noqa: E402
from eclass_digest.google_auth import SCOPES  # noqa: E402


def main() -> int:
    cfg = load_config()
    if not cfg.google_client_secrets.exists():
        print(f"Missing {cfg.google_client_secrets}. Download your OAuth client JSON from Google "
              "Cloud Console (see README, step 3) and save it there.")
        return 1
    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.google_client_secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    cfg.google_token_file.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved {cfg.google_token_file}")
    print("For GitHub Actions, paste that file's contents into a secret named GOOGLE_TOKEN_JSON.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
