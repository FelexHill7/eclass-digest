"""Guided setup. Run it from the project folder:

    python scripts/setup_wizard.py

Each step is tested as you go, and re-running the wizard skips whatever already works.
"""

from __future__ import annotations

import getpass
import json
import platform
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eclass_digest.envfile import read_env, set_env  # noqa: E402
from eclass_digest.moodle import Moodle, MoodleError  # noqa: E402

SERVICE_ACCOUNT = ROOT / "service_account.json"


def heading(n: int, title: str) -> None:
    print(f"\n{'=' * 60}\n Step {n}: {title}\n{'=' * 60}")


def ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    return input(f"{prompt}{shown}: ").strip() or default


def yes(prompt: str, default: bool = True) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


# --- 1. eClass ----------------------------------------------------------------

def step_eclass(env: dict) -> None:
    heading(1, "Connect eClass")
    import get_eclass_token as tok

    url = env.get("ECLASS_URL") or "https://eclass.yorku.ca"
    token = env.get("ECLASS_TOKEN", "")
    if token:
        try:
            info = Moodle(url, token).site_info()
            print(f"Already connected as {info.get('fullname')}. Skipping.")
            return
        except MoodleError:
            print("Your saved eClass token stopped working; let's get a new one.")

    print("A browser window will open. Log in to eClass as usual (Passport York + Duo).\n"
          "Once you reach your dashboard, the token is captured automatically.")
    input("Press Enter to open the browser...")
    token = tok.capture_with_browser(url)
    while True:
        if not token:
            print("\nCouldn't grab it automatically. In eClass open your profile picture >\n"
                  "Preferences > Security keys and copy the 'Moodle mobile web service' key.")
            token = tok.token_from_text(input("Paste it here: ").strip())
        if token:
            try:
                info = Moodle(url, token).site_info()
                set_env("ECLASS_TOKEN", token)
                print(f"Connected as {info.get('fullname')}.")
                return
            except MoodleError as e:
                print(f"eClass rejected that token ({e}).")
        token = None


# --- 2. Recipient -------------------------------------------------------------

def step_recipient(env: dict) -> None:
    heading(2, "Where should the digest go?")
    print("Tip: double-check this. Your York address may have a number in it (e.g. jsmith7@my.yorku.ca).")
    while True:
        to = ask("Email address to send the digest to", env.get("DIGEST_TO", ""))
        if "@" in to and yes(f"Send to {to}?"):
            set_env("DIGEST_TO", to)
            return


# --- 3. Gmail -----------------------------------------------------------------

def step_gmail(env: dict) -> None:
    heading(3, "A Gmail account to send from")
    user, password = env.get("SMTP_USER", ""), env.get("SMTP_PASSWORD", "")
    if user and password and _smtp_ok(user, password):
        print(f"Already set up to send from {user}. Skipping.")
        return
    print("""The digest is sent from a personal Gmail using an "app password".
School accounts (like @my.yorku.ca) usually block app passwords, so use a personal Gmail.

  1. Turn on 2-Step Verification:  https://myaccount.google.com/signinoptions/twosv
  2. Create an app password:       https://myaccount.google.com/apppasswords
     (name it "eClass Digest" and copy the 16-letter code)
""")
    while True:
        user = ask("Gmail address", user)
        password = getpass.getpass("App password (typing is hidden): ").replace(" ", "")
        if _smtp_ok(user, password):
            set_env("EMAIL_METHOD", "smtp")
            set_env("SMTP_USER", user)
            set_env("SMTP_PASSWORD", password)
            print("Gmail login works.")
            return
        print("Gmail rejected that. Check it's an app password (not your normal password) "
              "for this exact Gmail account.")


def _smtp_ok(user: str, password: str) -> bool:
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(user, password.replace(" ", ""))
        return True
    except (smtplib.SMTPException, OSError):
        return False


# --- 4. Google Calendar -------------------------------------------------------

def step_calendar(env: dict) -> None:
    heading(4, "Google Calendar")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if not SERVICE_ACCOUNT.exists():
        print("""The digest writes to your calendar through a free Google "service account"
(a robot account). Create one:

  1. https://console.cloud.google.com/  > create a project (Parent resource: No organization)
  2. APIs & Services > Library > "Google Calendar API" > Enable
  3. IAM & Admin > Service Accounts > + Create service account
     (any name; skip the optional role step; Done)
  4. Click the account's email > Keys tab > Add key > Create new key > JSON
  5. Rename the downloaded file to  service_account.json  and put it here:
     """ + str(ROOT))
        while not SERVICE_ACCOUNT.exists():
            input("Press Enter once service_account.json is in the folder...")

    info = json.loads(SERVICE_ACCOUNT.read_text(encoding="utf-8"))
    if info.get("type") != "service_account":
        sys.exit("service_account.json isn't a service-account key. Create a JSON key (step 4).")
    robot = info["client_email"]
    creds = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT), scopes=["https://www.googleapis.com/auth/calendar"])
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    cal_id = env.get("GCAL_CALENDAR_ID", "")
    if cal_id and _calendar_ok(svc, cal_id, quiet=True):
        print("Calendar already connected. Skipping.")
        return
    print(f"""Now make a calendar for your deadlines and share it with the robot:

  1. Open https://calendar.google.com/calendar/r/settings/createcalendar
     (a new *calendar*, not an "appointment schedule"), name it "YorkU eClass", Create.
  2. Click it under "Settings for my calendars" on the left.
  3. "Share with specific people or groups" > Add people:
        {robot}
     Permission: **Make changes to events**  (not "See all event details")
  4. "Event notifications": add the reminders you want (e.g. 1 day, 2 hours).
  5. "Integrate calendar": copy the Calendar ID.
""")
    while True:
        cal_id = ask("Calendar ID", cal_id)
        if cal_id and _calendar_ok(svc, cal_id):
            set_env("GCAL_CALENDAR_ID", cal_id)
            print("The robot can write to your calendar.")
            return


def _calendar_ok(svc, cal_id: str, quiet: bool = False) -> bool:
    from googleapiclient.errors import HttpError

    try:
        t = datetime.now(timezone.utc) + timedelta(days=1)
        ev = svc.events().insert(calendarId=cal_id, body={
            "summary": "eClass digest setup test",
            "start": {"dateTime": t.isoformat()},
            "end": {"dateTime": (t + timedelta(minutes=5)).isoformat()}}).execute()
        svc.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
        return True
    except HttpError as e:
        if not quiet:
            if e.resp.status == 404:
                print("Calendar not found. Check the ID, and that you shared it with the robot email.")
            elif e.resp.status == 403:
                print('The robot can only *see* this calendar. Change its permission to '
                      '"Make changes to events". If that option is greyed out, the calendar '
                      "belongs to a school account; make it in a personal Gmail instead.")
            else:
                print(f"Google said: {e.resp.status} {e.reason}")
        return False


# --- 5. AI summary ------------------------------------------------------------

def step_ai(env: dict) -> None:
    heading(5, "AI summary (optional, the only part that costs money)")
    print("With an Anthropic API key, each digest starts with a short written summary and\n"
          "to-do list (about a few cents a day). Without one, everything else still works.")
    if env.get("ANTHROPIC_API_KEY"):
        print("A key is already set. Skipping.")
        return
    key = getpass.getpass("Anthropic API key (press Enter to skip): ").strip()
    if not key:
        print("Skipped. You can add ANTHROPIC_API_KEY to .env any time.")
        return
    import anthropic

    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError:
        print("That key was rejected; skipping the summary for now.")
        return
    set_env("ANTHROPIC_API_KEY", key)
    set_env("AI_SUMMARY", "auto")
    print("Key works.")


# --- 6. First run + schedule --------------------------------------------------

def step_finish() -> None:
    heading(6, "First digest and daily schedule")
    if yes("Send your first digest now?"):
        subprocess.run([sys.executable, "-m", "eclass_digest"], cwd=ROOT)

    if platform.system() == "Windows":
        if yes("Run it automatically every morning on this PC?"):
            when = ask("What time? (24h, HH:MM)", "07:00")
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File",
                            str(ROOT / "scripts" / "install_task.ps1"), "-Time", when,
                            "-Python", str(pythonw if pythonw.exists() else sys.executable)])
    else:
        print("\nTo run it every morning at 7, add this line with `crontab -e`:\n"
              f"  0 7 * * * cd '{ROOT}' && '{sys.executable}' -m eclass_digest\n")

    print("\nWant it to run in the cloud even when your computer is off? See README:\n"
          "'Run it in the cloud with GitHub Actions' (one command: scripts/push_github_secrets.py).")
    print("\nAll done.")


def main() -> int:
    print("eClass Digest setup. Press Ctrl+C any time to stop; re-run to continue.")
    step_eclass(read_env())
    step_recipient(read_env())
    step_gmail(read_env())
    step_calendar(read_env())
    step_ai(read_env())
    step_finish()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Re-run the wizard to pick up where you left off.")
        sys.exit(130)
