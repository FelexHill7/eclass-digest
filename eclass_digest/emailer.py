"""Render the digest as an email and send it (Gmail API or SMTP)."""

from __future__ import annotations

import base64
import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path

import markdown

from .collect import Digest
from .config import Config

RED = "#E31837"  # York red
MUTED = "#6b6b6b"
ANNOUNCEMENT_PREVIEW_CHARS = 1500

FILE_ICONS = {".pdf": "📄", ".ppt": "📊", ".pptx": "📊", ".doc": "📝", ".docx": "📝",
              ".xls": "📈", ".xlsx": "📈", ".zip": "🗜️", ".mp4": "🎬", ".mp3": "🎧"}
MODULE_ICONS = {"assign": "📝", "quiz": "❓", "forum": "💬", "url": "🔗", "page": "📃",
                "folder": "📁", "resource": "📄", "lti": "🧩", "h5pactivity": "🧩",
                "book": "📚", "choice": "☑️", "feedback": "🗳️", "zoom": "🎥"}


def _when(dt: datetime) -> str:
    return dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def _relative(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _link(url: str, text: str) -> str:
    return f'<a href="{escape(url)}" style="color:{RED};text-decoration:none">{escape(text)}</a>'


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _badge(status: str) -> str:
    if not status:
        return ""
    color = {"new": "#1a7f37", "updated": "#9a6700", "recent": "#0969da"}[status]
    return (f' <span style="background:{color};color:#fff;border-radius:3px;padding:0 5px;'
            f'font-size:11px;text-transform:uppercase">{status}</span>')


def _h2(text: str) -> str:
    return (f'<h2 style="font-size:17px;margin:28px 0 8px;padding-bottom:4px;'
            f'border-bottom:2px solid {RED}">{text}</h2>')


def subject_line(digest: Digest, cfg: Config) -> str:
    parts = []
    n_ann = len(digest.fresh_announcements())
    n_new = len(digest.changed_modules())
    n_due = len(digest.upcoming(min(cfg.deadline_window_days, 7)))
    if n_ann:
        parts.append(f"{n_ann} announcement{'s' * (n_ann != 1)}")
    if n_new:
        parts.append(f"{n_new} new/updated item{'s' * (n_new != 1)}")
    if n_due:
        parts.append(f"{n_due} due this week")
    tail = ", ".join(parts) or "nothing new"
    return f"eClass digest · {digest.generated:%a %b %d} · {tail}"


def render_html(digest: Digest, cfg: Config) -> str:
    now = digest.generated
    h: list[str] = []
    h.append(f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
             f'max-width:720px;margin:0 auto;color:#1f1f1f;font-size:14px;line-height:1.5">')
    h.append(f'<div style="background:{RED};color:#fff;padding:16px 20px;border-radius:6px">'
             f'<div style="font-size:20px;font-weight:600">eClass Daily Digest</div>'
             f'<div style="opacity:.9">{now:%A, %B %d, %Y}</div></div>')

    if digest.errors:
        h.append('<div style="background:#fff4e5;border:1px solid #f0b46a;padding:10px 14px;'
                 'margin-top:16px;border-radius:6px"><b>Some things couldn\'t be fetched:</b><ul>')
        h.extend(f"<li>{escape(e)}</li>" for e in digest.errors)
        h.append("</ul></div>")

    if digest.first_run:
        h.append(f'<p style="color:{MUTED}">First run: everything currently on eClass is '
                 f'listed below as a baseline. From tomorrow, only changes are flagged.</p>')

    # --- AI summary -----------------------------------------------------------
    if digest.summary_md:
        md_html = markdown.markdown(digest.summary_md, extensions=["sane_lists"])
        md_html = md_html.replace("[ ]", "☐").replace("<a ", f'<a style="color:{RED}" ')
        h.append(_h2("Summary"))
        h.append(f'<div style="background:#fafafa;border-radius:6px;padding:4px 16px">{md_html}</div>')

    # --- Deadlines ------------------------------------------------------------
    due = digest.upcoming(cfg.deadline_window_days)
    h.append(_h2(f"Coming up (next {cfg.deadline_window_days} days)"))
    if due:
        h.append('<table style="border-collapse:collapse;width:100%">')
        for e in due:
            soon = (e.start - now).total_seconds() < 3 * 86400
            weight = "font-weight:600;" if soon else ""
            h.append(
                f'<tr style="border-bottom:1px solid #eee">'
                f'<td style="padding:6px 16px 6px 0;width:1%;white-space:nowrap;vertical-align:top;{weight}">'
                f'{escape(_when(e.start))}<br><span style="color:{MUTED};font-weight:400">'
                f'{_relative(e.start, now)}</span></td>'
                f'<td style="padding:6px 0;vertical-align:top"><span style="color:{MUTED}">'
                f'{escape(e.course_name or "Personal")}</span><br>{_link(e.url, e.name)}</td></tr>'
            )
        h.append("</table>")
    else:
        h.append(f'<p style="color:{MUTED}">Nothing due.</p>')

    # --- Announcements --------------------------------------------------------
    anns = digest.fresh_announcements()
    h.append(_h2("Announcements"))
    if not anns:
        h.append(f'<p style="color:{MUTED}">No new announcements.</p>')
    for course, a in anns:
        body = a.text
        more = ""
        if len(body) > ANNOUNCEMENT_PREVIEW_CHARS:
            body = body[:ANNOUNCEMENT_PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"
            more = f'<br>{_link(a.url, "Read the full announcement →")}'
        h.append(
            f'<div style="border-left:3px solid {RED};padding:2px 12px;margin:12px 0">'
            f'<div><b>{_link(a.url, a.subject)}</b>{_badge(a.status)}</div>'
            f'<div style="color:{MUTED};font-size:12px">{escape(course.shortname)} · '
            f'{escape(a.author)} · {escape(_when(a.posted))}</div>'
            f'<div style="margin-top:6px">{escape(body).replace(chr(10), "<br>")}{more}</div></div>'
        )

    # --- New / updated resources ---------------------------------------------
    changed = digest.changed_modules()
    h.append(_h2("New &amp; updated on eClass"))
    if not changed:
        h.append(f'<p style="color:{MUTED}">Nothing new since the last digest.</p>')
    by_course: dict[int, list] = {}
    for course, m in changed:
        by_course.setdefault(course.id, [course]).append(m)
    for course, *mods in by_course.values():
        h.append(f'<h3 style="font-size:15px;margin:14px 0 4px">{_link(course.url, course.shortname)}</h3><ul style="margin:0;padding-left:20px">')
        for m in mods:
            h.append(f"<li>{_module_line(m, only_changed=True)}</li>")
        h.append("</ul>")

    # --- Full index -----------------------------------------------------------
    if cfg.include_full_index:
        h.append(_h2("All course resources"))
        for course in digest.courses:
            h.append(f'<h3 style="font-size:15px;margin:18px 0 2px">{_link(course.url, course.shortname)}'
                     f'</h3><div style="color:{MUTED};font-size:12px">{escape(course.fullname)}</div>')
            if course.announcements:
                h.append('<div style="margin:6px 0 0"><b>Recent announcements</b></div>'
                         '<ul style="margin:0;padding-left:20px">')
                for a in course.announcements[:5]:
                    h.append(f'<li>{_link(a.url, a.subject)} <span style="color:{MUTED}">'
                             f'· {a.posted:%b %d}</span></li>')
                h.append("</ul>")
            for section, mods in course.sections:
                h.append(f'<div style="margin:8px 0 0"><b>{escape(section)}</b></div>'
                         f'<ul style="margin:0;padding-left:20px">')
                for m in mods:
                    h.append(f"<li>{_module_line(m)}</li>")
                h.append("</ul>")

    h.append(f'<p style="color:{MUTED};font-size:12px;margin-top:32px">Links open in eClass and '
             f'need you to be signed in. Generated {escape(_when(now))}.</p></div>')
    return "\n".join(h)


def _module_line(m, only_changed: bool = False) -> str:
    icon = MODULE_ICONS.get(m.modname, "")
    line = f"{icon} {_link(m.url, m.name)}{_badge(m.status)}".lstrip()
    if only_changed:  # out of context, "Slides" alone doesn't say which week
        line += f' <span style="color:{MUTED};font-size:12px">· {escape(m.section)}</span>'

    files = [f for f in m.files if f.status] if only_changed else m.files
    # A single-file resource is already linked by its module; only list files when it adds info.
    if m.modname == "folder" or len(files) > 1:
        items = []
        for f in files:
            fi = FILE_ICONS.get(Path(f.name).suffix.lower(), "📎")
            items.append(f'<li>{fi} {_link(f.url, f.name)} <span style="color:{MUTED}">'
                         f'{_size(f.size)}</span>{_badge(f.status)}</li>')
        if items:
            line += '<ul style="margin:0;padding-left:18px">' + "".join(items) + "</ul>"
    elif files and m.modname == "resource":
        f = files[0]
        line += f' <span style="color:{MUTED}">· {_link(f.url, "download")} {_size(f.size)}</span>'
    return line


def render_text(digest: Digest, cfg: Config) -> str:
    lines = [f"eClass Daily Digest — {digest.generated:%A, %B %d, %Y}", ""]
    for e in digest.errors:
        lines.append(f"! {e}")
    if digest.summary_md:
        lines += ["SUMMARY", digest.summary_md, ""]
    lines.append(f"COMING UP (next {cfg.deadline_window_days} days)")
    for e in digest.upcoming(cfg.deadline_window_days):
        lines.append(f"- {_when(e.start)}  {e.course_name or 'Personal'}: {e.name}\n  {e.url}")
    lines += ["", "ANNOUNCEMENTS"]
    for course, a in digest.fresh_announcements():
        lines.append(f"- [{course.shortname}] {a.subject} ({_when(a.posted)})\n  {a.url}")
    lines += ["", "NEW & UPDATED"]
    for course, m in digest.changed_modules():
        lines.append(f"- [{course.shortname}] {m.name} ({m.status})\n  {m.url}")
    return "\n".join(lines)


CHANGE_LABELS = {"added": ("Added", "#1a7f37"), "moved": ("Rescheduled", "#9a6700"),
                 "removed": ("Removed", "#cf222e")}


def render_calendar_email(changes, cfg: Config) -> tuple[str, str, str]:
    """Subject, HTML and text for the "your calendar changed" email."""
    counts = {k: sum(c.kind == k for c in changes) for k in CHANGE_LABELS}
    parts = []
    if counts["added"]:
        parts.append(f"{counts['added']} new deadline{'s' * (counts['added'] != 1)}")
    if counts["moved"]:
        parts.append(f"{counts['moved']} rescheduled")
    if counts["removed"]:
        parts.append(f"{counts['removed']} removed")
    subject = f"📅 Calendar updated · {', '.join(parts)}"

    h = [f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
         f'max-width:640px;margin:0 auto;color:#1f1f1f;font-size:14px;line-height:1.5">',
         f'<div style="background:{RED};color:#fff;padding:14px 18px;border-radius:6px;'
         f'font-size:18px;font-weight:600">Your eClass calendar was updated</div>',
         '<table style="border-collapse:collapse;width:100%;margin-top:12px">']
    text = ["Your eClass calendar was updated", ""]
    for c in sorted(changes, key=lambda c: c.start):
        label, color = CHANGE_LABELS[c.kind]
        when = escape(_when(c.start))
        if c.previous_start:
            when = f"<s style='color:{MUTED}'>{escape(_when(c.previous_start))}</s><br>{when}"
        links = [_link(c.url, "Open in eClass")] if c.url else []
        if c.calendar_link:
            links.append(_link(c.calendar_link, "View in Calendar"))
        title = f"<s>{escape(c.title)}</s>" if c.kind == "removed" else escape(c.title)
        h.append(
            f'<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:8px 14px 8px 0;width:1%;white-space:nowrap;vertical-align:top">'
            f'<span style="background:{color};color:#fff;border-radius:3px;padding:0 6px;'
            f'font-size:11px;text-transform:uppercase">{label}</span></td>'
            f'<td style="padding:8px 14px 8px 0;width:1%;white-space:nowrap;vertical-align:top">{when}</td>'
            f'<td style="padding:8px 0;vertical-align:top">{title}<br>'
            f'<span style="font-size:12px">{" · ".join(links)}</span></td></tr>')
        moved = f" (was {_when(c.previous_start)})" if c.previous_start else ""
        text.append(f"- {label}: {_when(c.start)}{moved}  {c.title}\n  {c.url}")
    h.append("</table></div>")
    return subject, "\n".join(h), "\n".join(text)


def build_message(cfg: Config, subject: str, html: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = cfg.digest_to
    msg["Subject"] = subject
    if cfg.email_from:
        msg["From"] = cfg.email_from
    msg.set_content(text)
    msg.add_alternative(f"<html><body>{html}</body></html>", subtype="html")
    return msg


def send(cfg: Config, msg: EmailMessage, google_creds=None) -> None:
    if cfg.email_method == "smtp":
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as s:
            s.starttls()
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)
        return
    from .google_auth import is_service_account

    if google_creds is None or is_service_account(google_creds):
        raise RuntimeError("EMAIL_METHOD=gmail_api needs scripts/google_login.py; with a service "
                           "account, set EMAIL_METHOD=smtp and a Gmail app password (see README)")
    from googleapiclient.discovery import build

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail = build("gmail", "v1", credentials=google_creds, cache_discovery=False)
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
