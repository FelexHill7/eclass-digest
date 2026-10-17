"""Render the emails (evening digest, 2-hourly alerts, morning "due today") and send them."""

from __future__ import annotations

import base64
import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path

import markdown

from .collect import Digest, Event
from .config import Config

RED = "#E31837"  # York red
WARN = "#cf222e"
OK = "#1a7f37"
AMBER = "#9a6700"
MUTED = "#6b6b6b"
ANNOUNCEMENT_PREVIEW_CHARS = 1500

FILE_ICONS = {".pdf": "📄", ".ppt": "📊", ".pptx": "📊", ".doc": "📝", ".docx": "📝",
              ".xls": "📈", ".xlsx": "📈", ".zip": "🗜️", ".mp4": "🎬", ".mp3": "🎧"}
MODULE_ICONS = {"assign": "📝", "quiz": "❓", "forum": "💬", "url": "🔗", "page": "📃",
                "folder": "📁", "resource": "📄", "lti": "🧩", "h5pactivity": "🧩",
                "book": "📚", "choice": "☑️", "feedback": "🗳️", "zoom": "🎥"}
CHANGE_LABELS = {"added": ("Added", OK), "moved": ("Rescheduled", AMBER), "removed": ("Removed", WARN)}


# --- Small pieces ---------------------------------------------------------------

def _when(dt: datetime) -> str:
    return dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def _time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _relative(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    if days == 0:
        return "tonight" if dt.hour >= 18 else "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _hours_left(dt: datetime, now: datetime) -> str:
    hours = (dt - now).total_seconds() / 3600
    return f"{int(hours * 60)} min left" if hours < 1 else f"{hours:.0f} h left"


def _link(url: str, text: str) -> str:
    return f'<a href="{escape(url)}" style="color:{RED};text-decoration:none">{escape(text)}</a>'


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _pill(text: str, color: str) -> str:
    return (f' <span style="background:{color};color:#fff;border-radius:3px;padding:0 5px;'
            f'font-size:11px;text-transform:uppercase;white-space:nowrap">{escape(text)}</span>')


def _badge(status: str) -> str:
    if not status:
        return ""
    return _pill(status, {"new": OK, "updated": AMBER, "recent": "#0969da"}[status])


def _submission_badge(e: Event, now: datetime, cfg: Config) -> str:
    """Green when submitted; red when it's due within DUE_SOON_HOURS and not submitted."""
    if e.submission == "submitted":
        return _pill("✓ submitted", OK)
    if not e.needs_submission:
        return ""
    urgent = (e.start - now).total_seconds() <= cfg.due_soon_hours * 3600
    label = "draft, not submitted" if e.submission == "draft" else "not submitted"
    if urgent:
        return _pill(f"⚠ {label}", WARN)
    return f' <span style="color:{MUTED};font-size:12px">· {label} yet</span>'


def _h2(text: str, color: str = RED) -> str:
    return (f'<h2 style="font-size:17px;margin:28px 0 8px;padding-bottom:4px;'
            f'border-bottom:2px solid {color}">{text}</h2>')


def _page(title: str, subtitle: str, body: list[str], now: datetime) -> str:
    return "\n".join([
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        f'max-width:720px;margin:0 auto;color:#1f1f1f;font-size:14px;line-height:1.5">',
        f'<div style="background:{RED};color:#fff;padding:16px 20px;border-radius:6px">'
        f'<div style="font-size:20px;font-weight:600">{escape(title)}</div>'
        f'<div style="opacity:.9">{escape(subtitle)}</div></div>',
        *body,
        f'<p style="color:{MUTED};font-size:12px;margin-top:32px">Links open in eClass and '
        f'need you to be signed in. Generated {escape(_when(now))}.</p></div>'])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


# --- Sections -------------------------------------------------------------------

def _errors(digest: Digest) -> list[str]:
    if not digest.errors:
        return []
    return ['<div style="background:#fff4e5;border:1px solid #f0b46a;padding:10px 14px;'
            'margin-top:16px;border-radius:6px"><b>Some things couldn\'t be fetched:</b><ul>',
            *(f"<li>{escape(e)}</li>" for e in digest.errors), "</ul></div>"]


def _urgent_box(events: list[Event], now: datetime) -> list[str]:
    if not events:
        return []
    rows = "".join(
        f'<li style="margin:4px 0"><b>{_link(e.url, e.name)}</b> · {escape(e.course_name)}<br>'
        f'due {escape(_when(e.start))} <b>({_hours_left(e.start, now)})</b>'
        f'{" · you have a draft that isn&#39;t submitted" if e.submission == "draft" else ""}</li>'
        for e in events)
    return [f'<div style="background:#ffebe9;border:1px solid {WARN};border-left:5px solid {WARN};'
            f'padding:10px 14px;margin-top:16px;border-radius:6px;color:#1f1f1f">'
            f'<div style="color:{WARN};font-weight:700">⚠ Due soon and not submitted</div>'
            f'<ul style="margin:6px 0 0;padding-left:18px">{rows}</ul></div>']


def _deadline_table(events: list[Event], now: datetime, cfg: Config) -> list[str]:
    h = ['<table style="border-collapse:collapse;width:100%">']
    for e in events:
        soon = (e.start - now).total_seconds() < 3 * 86400
        weight = "font-weight:600;" if soon else ""
        h.append(
            f'<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:6px 16px 6px 0;width:1%;white-space:nowrap;vertical-align:top;{weight}">'
            f'{escape(_when(e.start))}<br><span style="color:{MUTED};font-weight:400">'
            f'{_relative(e.start, now)}</span></td>'
            f'<td style="padding:6px 0;vertical-align:top"><span style="color:{MUTED}">'
            f'{escape(e.course_name or "Personal")}</span><br>{_link(e.url, e.name)}'
            f'{_submission_badge(e, now, cfg)}</td></tr>')
    h.append("</table>")
    return h


def _grades(items) -> list[str]:
    h = []
    for course, g in items:
        feedback = ""
        if g.feedback:
            feedback = (f'<div style="margin-top:4px;padding:6px 10px;background:#f6f8fa;border-radius:4px">'
                        f'<b>Feedback:</b> {escape(g.feedback).replace(chr(10), "<br>")}</div>')
        h.append(f'<div style="border-left:3px solid {OK};padding:2px 12px;margin:10px 0">'
                 f'<b>{_link(g.url, g.name)}</b>{_badge(g.status)}<br>'
                 f'<span style="font-size:16px;font-weight:600">{escape(g.display)}</span>'
                 f' <span style="color:{MUTED};font-size:12px">· {escape(course.shortname)}'
                 f'{" · graded " + escape(_when(g.graded)) if g.graded else ""}</span>{feedback}</div>')
    return h


def _announcements(items) -> list[str]:
    h = []
    for course, a in items:
        body, more = a.text, ""
        if len(body) > ANNOUNCEMENT_PREVIEW_CHARS:
            body = body[:ANNOUNCEMENT_PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"
            more = f'<br>{_link(a.url, "Read the full announcement →")}'
        h.append(f'<div style="border-left:3px solid {RED};padding:2px 12px;margin:12px 0">'
                 f'<div><b>{_link(a.url, a.subject)}</b>{_badge(a.status)}</div>'
                 f'<div style="color:{MUTED};font-size:12px">{escape(course.shortname)} · '
                 f'{escape(a.author)} · {escape(_when(a.posted))}</div>'
                 f'<div style="margin-top:6px">{escape(body).replace(chr(10), "<br>")}{more}</div></div>')
    return h


def _changed(items) -> list[str]:
    by_course: dict[int, list] = {}
    for course, m in items:
        by_course.setdefault(course.id, [course]).append(m)
    h = []
    for course, *mods in by_course.values():
        h.append(f'<h3 style="font-size:15px;margin:14px 0 4px">{_link(course.url, course.shortname)}</h3>'
                 f'<ul style="margin:0;padding-left:20px">')
        h.extend(f"<li>{_module_line(m, only_changed=True)}</li>" for m in mods)
        h.append("</ul>")
    return h


def _calendar_changes(changes) -> list[str]:
    h = ['<table style="border-collapse:collapse;width:100%">']
    for c in sorted(changes, key=lambda c: c.start):
        label, color = CHANGE_LABELS[c.kind]
        when = escape(_when(c.start))
        if c.previous_start:
            when = f"<s style='color:{MUTED}'>{escape(_when(c.previous_start))}</s><br>{when}"
        links = [_link(c.url, "Open in eClass")] if c.url else []
        if c.calendar_link:
            links.append(_link(c.calendar_link, "View in Calendar"))
        title = f"<s>{escape(c.title)}</s>" if c.kind == "removed" else escape(c.title)
        h.append(f'<tr style="border-bottom:1px solid #eee">'
                 f'<td style="padding:8px 14px 8px 0;width:1%;white-space:nowrap;vertical-align:top">'
                 f'{_pill(label, color)}</td>'
                 f'<td style="padding:8px 14px 8px 0;width:1%;white-space:nowrap;vertical-align:top">{when}</td>'
                 f'<td style="padding:8px 0;vertical-align:top">{title}<br>'
                 f'<span style="font-size:12px">{" · ".join(links)}</span></td></tr>')
    h.append("</table>")
    return h


def _module_line(m, only_changed: bool = False) -> str:
    icon = MODULE_ICONS.get(m.modname, "")
    line = f"{icon} {_link(m.url, m.name)}{_badge(m.status)}".lstrip()
    if only_changed:  # out of context, "Slides" alone doesn't say which week
        line += f' <span style="color:{MUTED};font-size:12px">· {escape(m.section)}</span>'
    files = [f for f in m.files if f.status] if only_changed else m.files
    summaries = only_changed  # what each new file covers, under it
    # A single-file resource is already linked by its module; only list files when it adds info.
    if m.modname == "folder" or len(files) > 1:
        items = [f'<li>{FILE_ICONS.get(Path(f.name).suffix.lower(), "📎")} {_link(f.url, f.name)} '
                 f'<span style="color:{MUTED}">{_size(f.size)}</span>{_badge(f.status)}'
                 f'{_file_summary(f) if summaries else ""}</li>' for f in files]
        if items:
            line += '<ul style="margin:0;padding-left:18px">' + "".join(items) + "</ul>"
        return line
    if files and m.modname == "resource":
        f = files[0]
        line += f' <span style="color:{MUTED}">· {_link(f.url, "download")} {_size(f.size)}</span>'
    if files and summaries:
        line += _file_summary(files[0])
    return line


def _file_summary(f) -> str:
    if not f.summary:
        return ""
    md = markdown.markdown(f.summary, extensions=["sane_lists"])
    return (f'<div style="margin:4px 0 8px;padding:4px 12px;background:#f6f8fa;border-radius:4px;'
            f'font-size:13px">{md}</div>')


def _index(digest: Digest) -> list[str]:
    h = [_h2("All course resources")]
    for course in digest.courses:
        h.append(f'<h3 style="font-size:15px;margin:18px 0 2px">{_link(course.url, course.shortname)}'
                 f'</h3><div style="color:{MUTED};font-size:12px">{escape(course.fullname)}</div>')
        if course.announcements:
            h.append('<div style="margin:6px 0 0"><b>Recent announcements</b></div>'
                     '<ul style="margin:0;padding-left:20px">')
            h.extend(f'<li>{_link(a.url, a.subject)} <span style="color:{MUTED}">· {a.posted:%b %d}</span></li>'
                     for a in course.announcements[:5])
            h.append("</ul>")
        if course.grades:
            h.append('<div style="margin:6px 0 0"><b>Grades</b></div><ul style="margin:0;padding-left:20px">')
            h.extend(f'<li>{_link(g.url, g.name)}: {escape(g.display)}</li>' for g in course.grades)
            h.append("</ul>")
        for section, mods in course.sections:
            h.append(f'<div style="margin:8px 0 0"><b>{escape(section)}</b></div>'
                     f'<ul style="margin:0;padding-left:20px">')
            h.extend(f"<li>{_module_line(m)}</li>" for m in mods)
            h.append("</ul>")
    return h


def _text_lines(digest: Digest, changes, urgent: list[Event], cfg: Config) -> list[str]:
    lines = [f"! {e}" for e in digest.errors]
    if urgent:
        lines += ["DUE SOON AND NOT SUBMITTED"]
        lines += [f"- {_when(e.start)}  {e.course_name}: {e.name}\n  {e.url}" for e in urgent]
        lines.append("")
    for course, g in digest.fresh_grades():
        lines.append(f"GRADE [{course.shortname}] {g.name}: {g.display}"
                     + (f"\n  Feedback: {g.feedback}" if g.feedback else "") + f"\n  {g.url}")
    for course, a in digest.fresh_announcements():
        lines.append(f"ANNOUNCEMENT [{course.shortname}] {a.subject} ({_when(a.posted)})\n  {a.url}")
    for course, m in digest.changed_modules():
        lines.append(f"{m.status.upper()} [{course.shortname}] {m.name} ({m.section})\n  {m.url}")
    for c in changes:
        lines.append(f"CALENDAR {CHANGE_LABELS[c.kind][0]}: {_when(c.start)}  {c.title}")
    return lines


# --- The three emails --------------------------------------------------------------

def render_digest(digest: Digest, changes, cfg: Config) -> tuple[str, str, str]:
    """The full digest (scheduled for the evening): due tonight/tomorrow, everything new, summary."""
    now = digest.generated
    urgent = digest.urgent(cfg.due_soon_hours)
    due_soon = digest.due_by_end_of(1)
    later = [e for e in digest.upcoming(cfg.deadline_window_days) if e not in due_soon]
    grades, anns, changed = digest.fresh_grades(), digest.fresh_announcements(), digest.changed_modules()

    h = _errors(digest) + _urgent_box(urgent, now)
    if digest.first_run:
        h.append(f'<p style="color:{MUTED}">First run: everything currently on eClass is listed '
                 f'below as a baseline. From now on, only changes are flagged.</p>')
    h.append(_h2("Due tonight &amp; tomorrow"))
    h += _deadline_table(due_soon, now, cfg) if due_soon else \
        [f'<p style="color:{MUTED}">Nothing due tonight or tomorrow.</p>']
    if digest.summary_md:
        md = markdown.markdown(digest.summary_md, extensions=["sane_lists"])
        md = md.replace("[ ]", "☐").replace("<a ", f'<a style="color:{RED}" ')
        h += [_h2("Summary"), f'<div style="background:#fafafa;border-radius:6px;padding:4px 16px">{md}</div>']
    if grades:
        h += [_h2("New grades &amp; feedback", OK)] + _grades(grades)
    h.append(_h2("Announcements"))
    h += _announcements(anns) or [f'<p style="color:{MUTED}">No new announcements.</p>']
    h.append(_h2("New &amp; updated on eClass"))
    h += _changed(changed) or [f'<p style="color:{MUTED}">Nothing new since the last digest.</p>']
    if changes and cfg.notify_calendar_changes:
        h += [_h2("Calendar changes")] + _calendar_changes(changes)
    h.append(_h2(f"Coming up (next {cfg.deadline_window_days} days)"))
    h += _deadline_table(later, now, cfg) if later else [f'<p style="color:{MUTED}">Nothing else due.</p>']
    if cfg.include_full_index:
        h += _index(digest)

    parts = []
    if urgent:
        parts.append(f"⚠ {len(urgent)} not submitted")
    if grades:
        parts.append(_plural(len(grades), "grade"))
    if anns:
        parts.append(_plural(len(anns), "announcement"))
    if changed:
        parts.append(f"{_plural(len(changed), 'new/updated item')}")
    if due_soon:
        parts.append(f"{len(due_soon)} due by tomorrow")
    subject = f"eClass digest · {now:%a %b %d} · {', '.join(parts) or 'nothing new'}"
    html = _page("eClass Daily Digest", f"{now:%A, %B %d, %Y}", h, now)
    text = [f"eClass Daily Digest — {now:%A, %B %d, %Y}", ""]
    if digest.summary_md:
        text += ["SUMMARY", digest.summary_md, ""]
    text += ["DUE TONIGHT & TOMORROW"] + [f"- {_when(e.start)}  {e.course_name}: {e.name}" for e in due_soon]
    text += [""] + _text_lines(digest, changes, urgent, cfg)
    return subject, html, "\n".join(text)


def render_alert(digest: Digest, changes, urgent: list[Event], cfg: Config) -> tuple[str, str, str]:
    """The short 2-hourly "something changed" email. Only sent when there's something in it."""
    now = digest.generated
    grades, anns, changed = digest.fresh_grades(), digest.fresh_announcements(), digest.changed_modules()
    h = _errors(digest) + _urgent_box(urgent, now)
    if grades:
        h += [_h2("New grades &amp; feedback", OK)] + _grades(grades)
    if anns:
        h += [_h2("Announcements")] + _announcements(anns)
    if changed:
        h += [_h2("New &amp; updated on eClass")] + _changed(changed)
    if changes and cfg.notify_calendar_changes:
        h += [_h2("Calendar changes")] + _calendar_changes(changes)

    parts = []
    if urgent:
        parts.append(f"⚠ {len(urgent)} due soon, not submitted")
    if grades:
        parts.append(_plural(len(grades), "new grade"))
    if anns:
        parts.append(_plural(len(anns), "announcement"))
    if changed:
        parts.append(_plural(len(changed), "new file") if all(m.files for _, m in changed)
                     else _plural(len(changed), "new item"))
    if changes and cfg.notify_calendar_changes:
        parts.append(_plural(len(changes), "calendar change"))
    courses = sorted({c.shortname for c, _ in grades + anns + changed})
    where = f" in {', '.join(courses)}" if courses and len(courses) <= 2 else ""
    subject = f"🔔 eClass: {', '.join(parts)}{where}"
    html = _page("New on eClass", f"{now:%A %b %d, %I:%M %p}".replace(" 0", " "), h, now)
    return subject, html, "\n".join(["New on eClass", ""] + _text_lines(digest, changes, urgent, cfg))


def render_morning(digest: Digest, changes, cfg: Config) -> tuple[str, str, str]:
    """The early "due today" email."""
    now = digest.generated
    today = digest.due_by_end_of(0)
    open_today = [e for e in today if e.needs_submission]
    h = _errors(digest) + _urgent_box(digest.urgent(cfg.due_soon_hours), now)
    h.append(_h2("Due today"))
    h += _deadline_table(today, now, cfg) if today else [f'<p style="color:{MUTED}">Nothing due today.</p>']
    events_today = [e for e in digest.upcoming(1) if not e.is_deadline and e.start.date() == now.date()]
    if events_today:
        h += [_h2("Also today")] + _deadline_table(events_today, now, cfg)
    if changes and cfg.notify_calendar_changes:
        h += [_h2("Calendar changes")] + _calendar_changes(changes)
    tail = f" ({len(open_today)} not submitted)" if open_today else ""
    subject = (f"☀ Due today: {_plural(len(today), 'item')}{tail}" if today
               else "☀ Nothing due today")
    html = _page("Due today", f"{now:%A, %B %d}", h, now)
    text = ["DUE TODAY"] + [f"- {_time(e.start)}  {e.course_name}: {e.name}"
                            f"{' (NOT SUBMITTED)' if e.needs_submission else ''}\n  {e.url}" for e in today]
    return subject, html, "\n".join(text)


# --- Sending ------------------------------------------------------------------------

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
