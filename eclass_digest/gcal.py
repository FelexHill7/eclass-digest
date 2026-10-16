"""Mirror eClass calendar events (due dates, quizzes, exams) into Google Calendar."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .collect import Digest, Event
from .config import Config
from .google_auth import is_service_account

log = logging.getLogger(__name__)


def _google_id(ev: Event) -> str:
    # Google event ids must use base32hex characters (0-9, a-v); "eclass" + digits qualifies.
    return f"eclass{ev.id}"


def event_body(ev: Event, cfg: Config) -> dict:
    end = ev.start + timedelta(seconds=ev.duration)
    title = f"{ev.course_name}: {ev.name}" if ev.course_name else ev.name
    description = "\n\n".join(p for p in (ev.description[:3000], f"Open in eClass: {ev.url}") if p)
    return {
        "summary": title,
        "description": description,
        "start": {"dateTime": ev.start.isoformat(), "timeZone": cfg.timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": cfg.timezone},
        "source": {"title": "eClass", "url": ev.url},
        # Due dates are instants; don't let them show you as busy.
        "transparency": "opaque" if ev.duration else "transparent",
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 24 * 60},
                          {"method": "popup", "minutes": 120}],
        },
    }


def _calendar_id(service, creds, state_gcal: dict, cfg: Config) -> str:
    if cfg.gcal_calendar_id:
        return cfg.gcal_calendar_id
    if is_service_account(creds):
        # A robot account's own calendars are invisible to you; it must use one you shared.
        raise RuntimeError("Set GCAL_CALENDAR_ID in .env to the calendar you shared with the "
                           "service account (see README)")
    cal_id = state_gcal.get("calendar_id")
    if cal_id:
        try:
            service.calendars().get(calendarId=cal_id).execute()
            return cal_id
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
            log.info("Saved calendar is gone; looking it up again")
    page = None
    while True:
        resp = service.calendarList().list(pageToken=page).execute()
        for item in resp.get("items", []):
            if item.get("summary") == cfg.gcal_calendar_name:
                state_gcal["calendar_id"] = item["id"]
                return item["id"]
        page = resp.get("nextPageToken")
        if not page:
            break
    created = service.calendars().insert(
        body={"summary": cfg.gcal_calendar_name, "timeZone": cfg.timezone,
              "description": "Deadlines and events synced daily from York eClass."}
    ).execute()
    log.info("Created Google calendar %r", cfg.gcal_calendar_name)
    state_gcal["calendar_id"] = created["id"]
    return created["id"]


@dataclass
class CalendarChange:
    kind: str  # "added" | "moved" | "removed"
    title: str
    start: datetime
    previous_start: datetime | None = None
    url: str = ""  # the eClass page
    calendar_link: str = ""  # the event in Google Calendar


def sync_calendar(creds, digest: Digest, state_gcal: dict, cfg: Config,
                  service=None) -> list[CalendarChange]:
    """Upserts every eClass event in the lookahead window; deletes ones eClass dropped.

    Returns what changed in a way you'd want to hear about (new, rescheduled, removed).
    """
    service = service or build("calendar", "v3", credentials=creds, cache_discovery=False)
    cal_id = _calendar_id(service, creds, state_gcal, cfg)
    synced: dict = state_gcal.setdefault("events", {})
    tz = ZoneInfo(cfg.timezone)
    changes: list[CalendarChange] = []
    unchanged = 0

    current = set()
    for ev in digest.events:
        gid = _google_id(ev)
        current.add(gid)
        body = event_body(ev, cfg)
        digest_hash = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
        prev = synced.get(gid)
        if prev and prev.get("hash") == digest_hash:
            prev.setdefault("title", body["summary"])  # older state files lack these
            prev.setdefault("url", ev.url)
            unchanged += 1
            continue
        try:
            result = service.events().insert(calendarId=cal_id, body={**body, "id": gid}).execute()
        except HttpError as e:
            if e.resp.status != 409:  # 409 = already exists (possibly deleted) -> update it
                raise
            result = service.events().update(
                calendarId=cal_id, eventId=gid, body={**body, "status": "confirmed"}
            ).execute()
        start = int(ev.start.timestamp())
        link = result.get("htmlLink", "")
        synced[gid] = {"hash": digest_hash, "start": start, "title": body["summary"],
                       "url": ev.url, "link": link}
        if prev is None:
            changes.append(CalendarChange("added", body["summary"], ev.start, url=ev.url,
                                          calendar_link=link))
        elif prev.get("start") != start:
            changes.append(CalendarChange("moved", body["summary"], ev.start,
                                          datetime.fromtimestamp(prev["start"], tz),
                                          url=ev.url, calendar_link=link))

    # Only trust "missing" when the eClass calendar actually loaded.
    if digest.events_ok:
        window_start = int((digest.generated - timedelta(days=1)).timestamp())
        for gid, info in list(synced.items()):
            if gid in current:
                continue
            if info["start"] >= window_start:  # was upcoming, eClass removed it
                try:
                    service.events().delete(calendarId=cal_id, eventId=gid).execute()
                except HttpError as e:
                    if e.resp.status not in (404, 410):
                        raise
                changes.append(CalendarChange(
                    "removed", info.get("title", "An eClass event"),
                    datetime.fromtimestamp(info["start"], tz), url=info.get("url", "")))
            del synced[gid]  # past events stay on the calendar; just stop tracking them

    log.info("Calendar sync: %d changes, %d unchanged", len(changes), unchanged)
    return changes
