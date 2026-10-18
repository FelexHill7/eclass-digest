"""Run the eClass digest.

    python -m eclass_digest                  # full digest (scheduled for the evening)
    python -m eclass_digest --mode check     # every couple of hours: email only if something's new
    python -m eclass_digest --mode morning   # "due today" email
    python -m eclass_digest --dry-run        # build it and save a preview to output/, send nothing
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from . import emailer
from .collect import Digest, collect
from .config import Config, load_config
from .files import process_files
from .google_auth import load_credentials
from .moodle import Moodle, MoodleError
from .state import State, empty_seen, run_lock

log = logging.getLogger("eclass_digest")


def _setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [
        RotatingFileHandler(cfg.log_dir / "eclass_digest.log", maxBytes=1_000_000,
                            backupCount=3, encoding="utf-8")
    ]
    if sys.stderr is not None:  # pythonw (Task Scheduler) has no console
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    for noisy in ("googleapiclient.discovery_cache", "pypdf", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _send(cfg: Config, subject: str, html: str, text: str, creds) -> bool:
    try:
        emailer.send(cfg, emailer.build_message(cfg, subject, html, text), creds)
        log.info("Emailed %s: %s", cfg.digest_to, subject)
        return True
    except Exception:
        log.exception("Sending the email failed")
        return False


def _token_expired(cfg: Config, state: State, mode: str) -> None:
    """Tell the user once a day, not every two hours."""
    today = datetime.now().date().isoformat()
    if state.data.get("token_alert_sent") == today:
        return
    body = ("Your eClass access token stopped working, so the digest can't check eClass. "
            "On your computer run: python scripts/get_eclass_token.py")
    try:
        creds = load_credentials(cfg) if cfg.email_method == "gmail_api" else None
    except Exception:
        creds = None
    if _send(cfg, "eClass digest: please refresh your eClass token", f"<p>{body}</p>", body, creds):
        state.data["token_alert_sent"] = today
        state.save()


def _save_preview(cfg: Config, name: str, subject: str, html: str) -> None:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.output_dir / name
    out.write_text(f"<!doctype html><meta charset='utf-8'><title>{subject}</title>{html}", encoding="utf-8")
    log.info("Saved %s", out)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eclass_digest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["digest", "check", "morning"], default="digest")
    p.add_argument("--dry-run", action="store_true",
                   help="build it and save a preview to output/, but don't email, touch the "
                        "calendar or update state")
    p.add_argument("--no-email", action="store_true")
    p.add_argument("--no-calendar", action="store_true")
    p.add_argument("--no-ai", action="store_true", help="skip the AI summary")
    p.add_argument("--reset", action="store_true",
                   help="forget what's been seen (next run is a fresh baseline)")
    args = p.parse_args(argv)
    if sys.stdout is not None:  # Windows consoles can't always print the emoji in subjects
        sys.stdout.reconfigure(errors="replace")

    cfg = load_config()
    _setup_logging(cfg)
    if not cfg.eclass_token:
        log.error("ECLASS_TOKEN is empty. Run: python scripts/setup_wizard.py")
        return 2
    if not cfg.digest_to and not (args.no_email or args.dry_run):
        log.error("DIGEST_TO is empty. Set it in .env to the address the digest should go to")
        return 2
    try:
        with run_lock(cfg.state_file):
            return _run(cfg, args)
    except TimeoutError as e:
        log.error("Skipping this run: %s", e)
        return 4
    except Exception:
        # Scheduled runs have no console, so make sure a crash ends up in the log file.
        log.exception("The %s run crashed", args.mode)
        return 1


def _run(cfg: Config, args) -> int:
    state = State.load(cfg.state_file)
    if args.reset:
        state.seen, state.alert_seen = empty_seen(), empty_seen()
    mode = args.mode

    moodle = Moodle(cfg.eclass_url, cfg.eclass_token)
    try:
        info = moodle.site_info()
    except MoodleError as e:
        if e.is_auth_error:
            log.error("eClass rejected the token (%s)", e)
            if not args.dry_run and not args.no_email:
                _token_expired(cfg, state, mode)
            return 3
        raise
    log.info("[%s] Signed in to %s as %s", mode, info.get("sitename"), info.get("fullname"))

    # The digest and the 2-hourly checks each diff against their own "already seen" baseline.
    baseline = state.alert_seen if mode == "check" else state.seen
    digest, new_seen = collect(moodle, copy.deepcopy(baseline), cfg)

    use_ai = mode == "digest" and cfg.ai_enabled and not args.no_ai
    if use_ai or (mode in ("digest", "check") and cfg.download_files and not args.dry_run):
        process_files(moodle, digest, cfg, want_text=use_ai)  # checks grab new files within 2 h
    if mode in ("digest", "check") and cfg.include_full_index:
        from .resources_page import write as write_resources_page

        try:
            write_resources_page(digest, cfg)
        except Exception:
            log.exception("Couldn't write the course resources page")
    if use_ai:
        from .summarize import summarize

        try:
            digest.summary_md = summarize(digest, cfg)
        except Exception as e:
            log.exception("AI summary failed")
            digest.errors.append(f"AI summary unavailable today: {e}")

    creds, changes = None, []
    needs_google = not args.dry_run and (
        not args.no_calendar or (not args.no_email and cfg.email_method == "gmail_api"))
    if needs_google:
        try:
            creds = load_credentials(cfg)
        except Exception as e:
            log.exception("Google sign-in failed")
            digest.errors.append(f"Google sign-in failed: {e}")
    if creds and not args.no_calendar:
        from .gcal import sync_calendar

        try:
            changes = sync_calendar(creds, digest, state.gcal, cfg)
        except Exception as e:
            log.exception("Calendar sync failed")
            digest.errors.append(f"Google Calendar sync failed: {e}")
    state.save()  # calendar bookkeeping is kept whatever happens with the email

    if mode == "check":
        return _check(cfg, args, state, digest, new_seen, changes, creds)
    if mode == "morning":
        return _morning(cfg, args, state, digest, changes, creds)
    code = _digest(cfg, args, state, digest, new_seen, changes, creds)
    if not args.dry_run:
        _obsidian(cfg, moodle, digest)  # after the email, so a slow AI doesn't delay it
    return code


def _obsidian(cfg: Config, moodle: Moodle, digest: Digest) -> None:
    """Notes for newly posted lectures, plus a few from the backlog each evening."""
    if cfg.obsidian_vault is None or not cfg.ai_enabled:
        return
    from .obsidian import sync

    try:
        written = sync(moodle, digest.courses, cfg, limit=cfg.obsidian_max_per_run)
        log.info("Obsidian: %d new note(s) in %s", len(written), cfg.obsidian_vault)
    except Exception:
        log.exception("Writing Obsidian notes failed")


def _remember_warned(state: State, digest: Digest, urgent) -> None:
    now = digest.generated.timestamp()
    state.warned.update({str(e.id): int(e.start.timestamp()) for e in urgent})
    for key, due in list(state.warned.items()):  # forget deadlines that have passed
        if due < now:
            del state.warned[key]


def _digest(cfg, args, state, digest, new_seen, changes, creds) -> int:
    subject, html, text = emailer.render_digest(digest, changes, cfg)
    out = _save_preview(cfg, f"digest-{digest.generated:%Y-%m-%d}.html", subject, html)
    if args.dry_run:
        print(f"Dry run. Subject: {subject}\nPreview: {out}")
        return 0
    should_send = digest.has_news or digest.errors or cfg.send_if_nothing_new or digest.first_run
    sent = should_send and not args.no_email and _send(cfg, subject, html, text, creds)
    # Only mark things as seen once you've actually been told about them.
    if sent or args.no_email or not should_send:
        state.seen = new_seen
        _remember_warned(state, digest, digest.urgent(cfg.due_soon_hours))
        state.data["last_run"] = datetime.now().isoformat(timespec="seconds")
    state.save()
    return 0 if (sent or args.no_email or not should_send) else 1


def _check(cfg, args, state, digest, new_seen, changes, creds) -> int:
    # "Due soon, not submitted" is announced once per deadline; the evening digest repeats it.
    urgent = [e for e in digest.urgent(cfg.due_soon_hours) if str(e.id) not in state.warned]
    calendar_news = bool(changes) and cfg.notify_calendar_changes
    if not (digest.has_news or urgent or calendar_news):
        log.info("Check: nothing new")
        state.alert_seen = new_seen
        state.save()
        return 0
    subject, html, text = emailer.render_alert(digest, changes, urgent, cfg)
    out = _save_preview(cfg, f"alert-{digest.generated:%Y-%m-%d-%H%M}.html", subject, html)
    if args.dry_run:
        print(f"Dry run. Subject: {subject}\nPreview: {out}")
        return 0
    sent = not args.no_email and _send(cfg, subject, html, text, creds)
    if sent or args.no_email:
        state.alert_seen = new_seen
        _remember_warned(state, digest, urgent)
    state.save()
    return 0 if (sent or args.no_email) else 1


def _morning(cfg, args, state, digest, changes, creds) -> int:
    subject, html, text = emailer.render_morning(digest, changes, cfg)
    out = _save_preview(cfg, f"morning-{digest.generated:%Y-%m-%d}.html", subject, html)
    if args.dry_run:
        print(f"Dry run. Subject: {subject}\nPreview: {out}")
        return 0
    has_something = digest.due_by_end_of(0) or (changes and cfg.notify_calendar_changes) or digest.errors
    if not (has_something or cfg.morning_send_if_empty):
        log.info("Morning: nothing due today, no email")
        return 0
    sent = not args.no_email and _send(cfg, subject, html, text, creds)
    if sent:
        _remember_warned(state, digest, digest.urgent(cfg.due_soon_hours))
        state.save()
    return 0 if (sent or args.no_email) else 1


if __name__ == "__main__":
    sys.exit(main())
