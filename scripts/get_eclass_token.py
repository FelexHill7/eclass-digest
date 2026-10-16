"""One-time: sign in to eClass (Passport York + Duo) in a real browser and capture your
mobile-app web-service token, then save it to .env as ECLASS_TOKEN.

    python scripts/get_eclass_token.py            # opens Edge/Chrome, you log in
    python scripts/get_eclass_token.py --paste    # paste a token or moodlemobile:// link yourself
"""

from __future__ import annotations

import argparse
import base64
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eclass_digest.config import load_config  # noqa: E402
from eclass_digest.envfile import set_env  # noqa: E402
from eclass_digest.moodle import Moodle, MoodleError  # noqa: E402

LAUNCH_RE = re.compile(r"moodlemobile://token=([A-Za-z0-9+/=_-]+)")
HEX_TOKEN_RE = re.compile(r"\b[0-9a-f]{32}\b")


def decode_launch_token(b64: str) -> str | None:
    """moodlemobile://token=<base64 of 'signature:::token[:::privatetoken]'>"""
    try:
        raw = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode()
    except Exception:
        return None
    parts = raw.split(":::")
    return parts[1] if len(parts) >= 2 and HEX_TOKEN_RE.fullmatch(parts[1]) else None


def token_from_text(text: str) -> str | None:
    m = LAUNCH_RE.search(text)
    if m:
        return decode_launch_token(m.group(1))
    m = HEX_TOKEN_RE.search(text)
    return m.group(0) if m else None


def _launch_browser(p):
    for channel in ("msedge", "chrome", None):
        try:
            return p.chromium.launch(channel=channel, headless=False) if channel \
                else p.chromium.launch(headless=False)
        except Exception:
            continue
    raise SystemExit("Couldn't start a browser. Run: python -m playwright install chromium")


def capture_with_browser(base: str) -> str | None:
    from playwright.sync_api import sync_playwright

    passport = random.randint(10_000, 99_999_999)
    launch = (f"{base}/admin/tool/mobile/launch.php?service=moodle_mobile_app"
              f"&passport={passport}&urlscheme=moodlemobile")
    found: dict[str, str] = {}

    with sync_playwright() as p:
        browser = _launch_browser(p)
        ctx = browser.new_context()
        page = ctx.new_page()

        def on_response(resp):
            m = LAUNCH_RE.search(resp.headers.get("location", ""))
            if m and decode_launch_token(m.group(1)):
                found["token"] = decode_launch_token(m.group(1))

        page.on("response", on_response)
        page.goto(f"{base}/login/index.php")
        print("\n>>> Log in to eClass in the browser window (Passport York + Duo).")
        print("    Waiting until you reach your eClass dashboard...\n")
        deadline = time.time() + 600
        while time.time() < deadline:
            url = page.url
            if url.startswith(base) and not any(s in url for s in ("/login/", "/auth/")):
                break
            page.wait_for_timeout(1000)
        else:
            browser.close()
            return None
        print("Logged in. Requesting a mobile-app token...")

        # 1) Ask launch.php directly (shares the browser's login cookies).
        try:
            resp = ctx.request.get(launch, max_redirects=0)
            t = token_from_text(resp.headers.get("location", "")) or \
                (LAUNCH_RE.search(resp.text()) and token_from_text(resp.text()))
            if t:
                found["token"] = t
        except Exception as e:
            print(f"  direct request didn't work ({e}); trying in the page")

        # 2) Open it in the page; some sites show a "Continue/Open the app" button first.
        if "token" not in found:
            try:
                page.goto(launch, wait_until="commit")
            except Exception:
                pass  # navigating to moodlemobile:// aborts; the response hook has it
            print("  If the page asks you to confirm or open the app, click the button.")
            for _ in range(60):
                if "token" in found:
                    break
                try:
                    t = LAUNCH_RE.search(page.content()) and token_from_text(page.content())
                    if t:
                        found["token"] = t
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)

        # 3) Preferences > Security keys lists the "Moodle mobile web service" key.
        if "token" not in found:
            try:
                page.goto(f"{base}/user/managetoken.php")
                for row in page.query_selector_all("table tr"):
                    cells = row.inner_text()
                    if "mobile" in cells.lower():
                        t = token_from_text(cells)
                        if t:
                            found["token"] = t
                            break
            except Exception:
                pass
        browser.close()
    return found.get("token")


def save_token(token: str) -> None:
    set_env("ECLASS_TOKEN", token)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paste", action="store_true", help="paste a token instead of logging in")
    args = ap.parse_args()
    cfg = load_config()

    token = None
    if not args.paste:
        token = capture_with_browser(cfg.eclass_url)
    if not token:
        print("\nCouldn't capture it automatically. In eClass go to your profile picture >\n"
              "Preferences > Security keys, copy the 'Moodle mobile web service' key and\n"
              "paste it here (a moodlemobile://token=... link also works).")
        token = token_from_text(input("Token: ").strip())
    if not token:
        print("That doesn't look like a token.")
        return 1

    try:
        info = Moodle(cfg.eclass_url, token).site_info()
    except MoodleError as e:
        print(f"eClass rejected that token: {e}")
        return 1
    save_token(token)
    print(f"\nWorks: signed in as {info.get('fullname')} on {info.get('sitename')}.")
    print("Saved ECLASS_TOKEN to .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
