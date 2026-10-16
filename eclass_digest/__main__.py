"""Run the daily digest: python -m eclass_digest [--dry-run] [--no-email] [--no-calendar] [--no-ai]"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from . import emailer
from .collect import collect
from .config import Config, load_config
from .files import process_files
from .google_auth import load_credentials
from .moodle import Moodle, MoodleError
from .state import State, empty_seen

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


def _alert(cfg: Config, subject: str, body: str) -> None:
    """Best-effort email when the run can't proceed (e.g. the eClass token expired)."""
    try:
        creds = load_credentials(cfg) if cfg.email_method == "gmail_api" else None
        msg = emailer.build_message(cfg, subject, f"<p>{body}</p>", body)
        emailer.send(cfg, msg, creds)
    except Exception:
        log.exception("Couldn't send the alert email either")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eclass_digest", description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="build the digest and save it to output/, but don't email, "
                        "touch the calendar or update state")
    p.add_argument("--no-email", action="store_true")
    p.add_argument("--no-calendar", action="store_true")
    p.add_argument("--no-ai", action="store_true", help="skip the AI summary")
    p.add_argument("--reset", action="store_true",
                   help="forget what's been seen (next digest is a fresh baseline)")
    args = p.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg)
    if not cfg.eclass_token:
        log.error("ECLASS_TOKEN is empty. Run: python scripts/setup.py")
        return 2
    if not cfg.digest_to and not (args.no_email or args.dry_run):
        log.error("DIGEST_TO is empty. Set it in .env to the address the digest should go to")
        return 2

    state = State.load(cfg.state_file)
    if args.reset:
        state.seen = empty_seen()

    moodle = Moodle(cfg.eclass_url, cfg.eclass_token)
    try:
        info = moodle.site_info()
    except MoodleError as e:
        if e.is_auth_error:
            log.error("eClass rejected the token (%s)", e)
            if not args.dry_run:
                _alert(cfg, "eClass digest: please refresh your eClass token",
                       "Your eClass access token stopped working, so today's digest wasn't "
                       "built. On your computer run: python scripts/get_eclass_token.py")
            return 3
        raise
    log.info("Signed in to %s as %s", info.get("sitename"), info.get("fullname"))

    digest, new_seen = collect(moodle, state.seen, cfg)

    use_ai = cfg.ai_enabled and not args.no_ai
    if use_ai or cfg.download_files:
        process_files(moodle, digest, cfg, want_text=use_ai)
    if use_ai:
        from .summarize import summarize

        try:
            digest.summary_md = summarize(digest, cfg)
        except Exception as e:
            log.exception("AI summary failed")
            digest.errors.append(f"AI summary unavailable today: {e}")

    creds = None
    needs_google = (not args.dry_run) and (
        not args.no_calendar or (not args.no_email and cfg.email_method == "gmail_api"))
    if needs_google:
        try:
            creds = load_credentials(cfg)
        except Exception as e:
            log.exception("Google sign-in failed")
            digest.errors.append(f"Google sign-in failed: {e}")

    if creds and not args.no_calendar and not args.dry_run:
        from .gcal import sync_calendar

        try:
            changes = sync_calendar(creds, digest, state.gcal, cfg)
        except Exception as e:
            log.exception("Calendar sync failed")
            digest.errors.append(f"Google Calendar sync failed: {e}")
            changes = []
        if changes and cfg.notify_calendar_changes and not args.no_email:
            c_subject, c_html, c_text = emailer.render_calendar_email(changes, cfg)
            try:
                emailer.send(cfg, emailer.build_message(cfg, c_subject, c_html, c_text), creds)
                log.info("Emailed calendar changes: %s", c_subject)
            except Exception:
                log.exception("Sending the calendar email failed")

    subject = emailer.subject_line(digest, cfg)
    html = emailer.render_html(digest, cfg)
    text = emailer.render_text(digest, cfg)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.output_dir / f"digest-{digest.generated:%Y-%m-%d}.html"
    out.write_text(f"<!doctype html><meta charset='utf-8'><title>{subject}</title>{html}",
                   encoding="utf-8")
    log.info("Saved %s", out)

    if args.dry_run:
        print(f"Dry run. Subject: {subject}\nPreview: {out}")
        return 0

    sent = False
    should_send = digest.has_news or digest.errors or cfg.send_if_nothing_new or digest.first_run
    if not args.no_email and should_send:
        try:
            emailer.send(cfg, emailer.build_message(cfg, subject, html, text), creds)
            sent = True
            log.info("Emailed %s: %s", cfg.digest_to, subject)
        except Exception:
            log.exception("Sending the email failed")

    # Only mark things as seen once you've actually been told about them.
    if sent or args.no_email or not should_send:
        state.seen = new_seen
        state.data["last_run"] = datetime.now().isoformat(timespec="seconds")
    state.save()
    return 0 if (sent or args.no_email or not should_send) else 1


if __name__ == "__main__":
    sys.exit(main())
