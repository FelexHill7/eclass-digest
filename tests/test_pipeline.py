"""Offline tests: a fake eClass drives collect -> diff -> render -> calendar bodies."""

from __future__ import annotations

import copy
import dataclasses
import time

import pytest

from eclass_digest import emailer
from eclass_digest.collect import collect, html_to_text
from eclass_digest.config import load_config
from eclass_digest.gcal import event_body
from eclass_digest.moodle import Moodle, _flatten
from eclass_digest.state import empty_seen
from eclass_digest.summarize import build_prompt

BASE = "https://eclass.yorku.ca"
NOW = int(time.time())


def pluginfile(path: str) -> str:
    return f"{BASE}/webservice/pluginfile.php/{path}?forcedownload=1"


CONTENTS = [
    {"id": 1, "name": "General", "modules": [
        {"id": 100, "name": "Announcements", "modname": "forum", "instance": 7,
         "url": f"{BASE}/mod/forum/view.php?id=100"},
        {"id": 101, "name": "Welcome", "modname": "label", "instance": 1},
    ]},
    {"id": 2, "name": "Week 1", "modules": [
        {"id": 200, "name": "Lecture 1 slides", "modname": "resource", "instance": 3,
         "url": f"{BASE}/mod/resource/view.php?id=200",
         "contents": [{"type": "file", "filename": "lec1.pdf", "filepath": "/",
                       "fileurl": pluginfile("1/mod_resource/content/1/lec1.pdf"),
                       "filesize": 2_400_000, "timemodified": NOW - 86400,
                       "mimetype": "application/pdf"}]},
        {"id": 201, "name": "Assignment 1", "modname": "assign", "instance": 9,
         "url": f"{BASE}/mod/assign/view.php?id=201"},
        {"id": 202, "name": "Textbook site", "modname": "url", "instance": 4,
         "url": f"{BASE}/mod/url/view.php?id=202",
         "contents": [{"type": "url", "fileurl": "https://example.com/book"}]},
    ]},
]


class FakeMoodle(Moodle):
    def __init__(self):
        super().__init__(BASE, "x" * 32)
        self.contents = copy.deepcopy(CONTENTS)
        self.discussions_list = [
            {"id": 1, "discussion": 50, "subject": "Welcome to EECS 2030", "userfullname": "Prof X",
             "created": NOW - 3600, "modified": NOW - 3600,
             "message": "<p>Hi all, see the <a href='https://x.org'>syllabus</a>.</p>"},
        ]
        self.grades = [
            {"id": 500, "itemname": "Lab 1", "itemtype": "mod", "itemmodule": "assign", "cmid": 201,
             "gradeformatted": "97.00", "grademax": 100, "gradedategraded": NOW - 86400, "feedback": ""},
            {"id": 501, "itemname": "Assignment 1", "itemtype": "mod", "itemmodule": "assign",
             "cmid": 201, "gradeformatted": "-", "grademax": 100, "gradedategraded": None, "feedback": ""},
            {"id": 599, "itemname": None, "itemtype": "course", "gradeformatted": "97.00"},
        ]
        self.submissions = {9: "not submitted"}

    def site_info(self):
        self.user_id = 1
        return {"userid": 1, "sitename": "eClass", "fullname": "Test Student"}

    def grade_items(self, course_id, user_id):
        return self.grades

    def assignment_submission(self, assign_id):
        return self.submissions.get(assign_id, "")

    def quiz_submission(self, quiz_id):
        return "not submitted"

    def courses(self, classification="inprogress"):
        return [{"id": 42, "shortname": "EECS2030", "fullname": "Advanced OOP",
                 "viewurl": f"{BASE}/course/view.php?id=42"}]

    def course_contents(self, course_id):
        return self.contents

    def forums(self, course_ids):
        return [{"id": 7, "course": 42, "type": "news", "name": "Announcements"}]

    def discussions(self, forum_id, perpage=10):
        return self.discussions_list

    def calendar_events(self, course_ids, start, end):
        return [{"id": 9001, "name": "Assignment 1 is due", "courseid": 42,
                 "modulename": "assign", "instance": 9, "eventtype": "due",
                 "timestart": NOW + 2 * 86400, "timeduration": 0, "visible": 1,
                 "description": "<p>Submit on eClass</p>"}]


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("ECLASS_TOKEN", "x" * 32)
    monkeypatch.setenv("TIMEZONE", "America/Toronto")
    # Ignore personal course filters from a local .env so tests behave the same everywhere.
    return dataclasses.replace(load_config(), state_file=tmp_path / "s.json",
                               include_courses=(), exclude_courses=())


def test_flatten():
    assert _flatten({"events": {"courseids": [3, 4]}, "flag": True}) == {
        "events[courseids][0]": 3, "events[courseids][1]": 4, "flag": 1}


def test_browser_url_strips_token():
    url = Moodle.browser_url(pluginfile("1/a.pdf") + "&token=secret")
    assert url == f"{BASE}/pluginfile.php/1/a.pdf"


def test_first_run_is_baseline_then_diffs(cfg):
    moodle = FakeMoodle()
    digest, seen = collect(moodle, empty_seen(), cfg)
    assert digest.first_run
    assert not digest.changed_modules()
    # Recent announcement still shown on first run, labels skipped
    assert [a.status for _, a in digest.fresh_announcements()] == ["recent"]
    assert all(m.modname != "label" for m in digest.courses[0].modules)
    # Event links to the assignment via (modname, instance) -> cmid
    assert digest.events[0].url == f"{BASE}/mod/assign/view.php?id=201"
    assert digest.courses[0].modules[3].url == "https://example.com/book"

    # Day 2: a new slide deck and an updated file and a new announcement
    moodle.contents[1]["modules"][0]["contents"][0]["timemodified"] = NOW
    moodle.contents[1]["modules"].append(
        {"id": 203, "name": "Lecture 2 slides", "modname": "resource", "instance": 5,
         "url": f"{BASE}/mod/resource/view.php?id=203",
         "contents": [{"type": "file", "filename": "lec2.pptx", "filepath": "/",
                       "fileurl": pluginfile("1/mod_resource/content/1/lec2.pptx"),
                       "filesize": 1000, "timemodified": NOW}]})
    moodle.discussions_list.insert(0, {
        "id": 2, "discussion": 51, "subject": "Midterm room change", "userfullname": "Prof X",
        "created": NOW, "modified": NOW, "message": "<p>Now in CLH A.</p>"})

    digest2, _ = collect(moodle, seen, cfg)
    assert not digest2.first_run
    changed = {m.name: m.status for _, m in digest2.changed_modules()}
    assert changed == {"Lecture 1 slides": "updated", "Lecture 2 slides": "new"}
    assert [a.subject for _, a in digest2.fresh_announcements()] == ["Midterm room change"]

    subject, html, _ = emailer.render_digest(digest2, [], cfg)
    assert "Midterm room change" in html and "lec2.pptx" in html
    assert "token" not in html and "webservice" not in html
    assert "1 announcement" in subject and "2 new/updated items" in subject
    assert "Assignment 1 is due" in build_prompt(digest2, cfg)


def test_calendar_body(cfg):
    digest, _ = collect(FakeMoodle(), empty_seen(), cfg)
    body = event_body(digest.events[0], cfg)
    assert body["summary"] == "EECS2030: Assignment 1 is due"
    assert body["start"] == body["end"]
    assert body["transparency"] == "transparent"
    assert "mod/assign/view.php?id=201" in body["description"]


def test_html_to_text_keeps_links():
    assert html_to_text("<p>See <a href='https://a.b'>this</a></p>") == "See this (https://a.b)"


def test_service_account_is_used_when_present(cfg, tmp_path):
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from eclass_digest.google_auth import is_service_account, load_credentials

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    sa = tmp_path / "service_account.json"
    sa.write_text(json.dumps({
        "type": "service_account", "project_id": "p", "private_key_id": "1", "private_key": pem,
        "client_email": "bot@p.iam.gserviceaccount.com", "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token"}))
    creds = load_credentials(dataclasses.replace(cfg, google_service_account_file=sa,
                                                 google_service_account_json=""))
    assert is_service_account(creds)
    assert creds.service_account_email == "bot@p.iam.gserviceaccount.com"

    with pytest.raises(RuntimeError, match="EMAIL_METHOD=smtp"):
        emailer.send(dataclasses.replace(cfg, email_method="gmail_api"),
                     emailer.build_message(cfg, "s", "<p>h</p>", "t"), creds)


class FakeCalendar:
    """Just enough of googleapiclient's Calendar service for sync_calendar."""

    def __init__(self):
        self.events_by_id: dict = {}

    def events(self):
        return self

    def insert(self, calendarId, body):
        self.events_by_id[body["id"]] = body
        return _Exec({"htmlLink": f"https://calendar.google.com/event?eid={body['id']}"})

    def update(self, calendarId, eventId, body):
        self.events_by_id[eventId] = body
        return _Exec({"htmlLink": "x"})

    def delete(self, calendarId, eventId):
        self.events_by_id.pop(eventId, None)
        return _Exec({})


class _Exec:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


def test_calendar_changes_are_reported(cfg):
    from eclass_digest.gcal import sync_calendar

    cfg = dataclasses.replace(cfg, gcal_calendar_id="cal")
    gcal_state: dict = {}
    fake, moodle = FakeCalendar(), FakeMoodle()

    digest, _ = collect(moodle, empty_seen(), cfg)
    changes = sync_calendar(None, digest, gcal_state, cfg, service=fake)
    assert [c.kind for c in changes] == ["added"]
    assert "eclass9001" in fake.events_by_id

    # Same data again: nothing to report
    digest, _ = collect(moodle, empty_seen(), cfg)
    assert sync_calendar(None, digest, gcal_state, cfg, service=fake) == []

    # Due date pushed back a day, plus a brand-new quiz
    moodle.calendar_events = lambda *a: [
        {"id": 9001, "name": "Assignment 1 is due", "courseid": 42, "modulename": "assign",
         "instance": 9, "eventtype": "due", "timestart": NOW + 3 * 86400, "timeduration": 0},
        {"id": 9002, "name": "Quiz 1 closes", "courseid": 42, "modulename": "quiz",
         "instance": 11, "eventtype": "close", "timestart": NOW + 5 * 86400, "timeduration": 0}]
    digest, _ = collect(moodle, empty_seen(), cfg)
    changes = sync_calendar(None, digest, gcal_state, cfg, service=fake)
    assert sorted(c.kind for c in changes) == ["added", "moved"]
    moved = next(c for c in changes if c.kind == "moved")
    assert moved.previous_start < moved.start

    subject, html, text = emailer.render_alert(digest, changes, [], cfg)
    assert subject.startswith("🔔 eClass:") and "2 calendar changes" in subject
    assert "Quiz 1 closes" in html and "View in Calendar" in html and "Rescheduled" in html

    # Quiz removed from eClass
    moodle.calendar_events = lambda *a: [
        {"id": 9001, "name": "Assignment 1 is due", "courseid": 42, "modulename": "assign",
         "instance": 9, "eventtype": "due", "timestart": NOW + 3 * 86400, "timeduration": 0}]
    digest, _ = collect(moodle, empty_seen(), cfg)
    changes = sync_calendar(None, digest, gcal_state, cfg, service=fake)
    assert [(c.kind, c.title) for c in changes] == [("removed", "EECS2030: Quiz 1 closes")]
    assert "eclass9002" not in fake.events_by_id


def test_include_courses_adds_courses_outside_in_progress(cfg):
    from eclass_digest.collect import tracked_courses

    class TermMoodle(FakeMoodle):
        def courses(self, classification="inprogress"):
            current = [{"id": 42, "shortname": "2026_LE_CSSD_F_3131__3_A_EN_A_LECT_01",
                        "fullname": "LE/CSSD 3131 A - Machine Learning"}]
            past = [{"id": 7, "shortname": "2026_LE_CSSD_F_3101__3_A_EN_A_LECT_01",
                     "fullname": "LE/CSSD 3101 A - Software Requirements &#38; Analysis"},
                    {"id": 8, "shortname": "2024_LE_CSSD_F_1101__3_A_EN_A_LECT_01",
                     "fullname": "LE/CSSD 1101 A - Old course"}]
            return current if classification == "inprogress" else current + past

    m = TermMoodle()
    ids = lambda c: sorted(x["id"] for x in tracked_courses(m, c))  # noqa: E731
    assert ids(cfg) == [42]
    assert ids(dataclasses.replace(cfg, include_courses=("cssd 3101",))) == [7, 42]
    assert ids(dataclasses.replace(cfg, include_courses=("cssd3101",))) == [7, 42]
    assert ids(dataclasses.replace(cfg, include_courses=("8",))) == [8, 42]
    assert ids(dataclasses.replace(cfg, include_courses=("requirements & analysis",))) == [7, 42]
    assert ids(dataclasses.replace(cfg, include_courses=("cssd 3101",),
                                   exclude_courses=("machine learning",))) == [7]


def test_new_grades_are_flagged_after_the_baseline(cfg):
    moodle = FakeMoodle()
    digest, seen = collect(moodle, empty_seen(), cfg)
    assert [g.name for g in digest.courses[0].grades] == ["Lab 1"]  # ungraded + course total skipped
    assert not digest.fresh_grades()  # first look at grades is a baseline

    moodle.grades[1].update(gradeformatted="88.50", gradedategraded=NOW, feedback="<p>Nice work</p>")
    moodle.grades[0]["feedback"] = "Late penalty applied"
    digest2, seen2 = collect(moodle, seen, cfg)
    fresh = {g.name: (g.status, g.display, g.feedback) for _, g in digest2.fresh_grades()}
    assert fresh == {"Assignment 1": ("new", "88.50 / 100", "Nice work"),
                     "Lab 1": ("updated", "97.00 / 100", "Late penalty applied")}
    assert digest2.has_news

    digest3, _ = collect(moodle, seen2, cfg)
    assert not digest3.fresh_grades()


def test_unsubmitted_deadline_within_24h_is_urgent(cfg):
    moodle = FakeMoodle()
    moodle.calendar_events = lambda *a: [
        {"id": 9001, "name": "Assignment 1 is due", "courseid": 42, "modulename": "assign",
         "instance": 9, "eventtype": "due", "timestart": NOW + 5 * 3600, "timeduration": 0},
        {"id": 9003, "name": "Lab 2 is due", "courseid": 42, "modulename": "assign",
         "instance": 10, "eventtype": "due", "timestart": NOW + 6 * 3600, "timeduration": 0},
        {"id": 9004, "name": "Lab 3 is due", "courseid": 42, "modulename": "assign",
         "instance": 11, "eventtype": "due", "timestart": NOW + 3 * 86400, "timeduration": 0}]
    moodle.submissions = {9: "not submitted", 10: "submitted", 11: "draft"}
    digest, _ = collect(moodle, empty_seen(), cfg)

    assert [e.name for e in digest.urgent(24)] == ["Assignment 1 is due"]  # Lab 3 is 3 days out
    subject, html, _ = emailer.render_digest(digest, [], cfg)
    assert subject.split(" · ")[2].startswith("⚠ 1 not submitted")
    assert "Due soon and not submitted" in html and "✓ submitted" in html
    assert "draft, not submitted yet" in html  # not urgent yet, so not red

    subject, html, _ = emailer.render_alert(digest, [], digest.urgent(24), cfg)
    assert subject.startswith("🔔 eClass: ⚠ 1 due soon, not submitted")


def test_morning_lists_whats_due_today(cfg):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    end_of_today = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tz)
    moodle = FakeMoodle()
    due_today = int((now + (end_of_today - now) / 2).timestamp())
    moodle.calendar_events = lambda *a: [
        {"id": 9001, "name": "Assignment 1 is due", "courseid": 42, "modulename": "assign",
         "instance": 9, "eventtype": "due", "timestart": due_today, "timeduration": 0},
        {"id": 9005, "name": "Lab 9 is due", "courseid": 42, "modulename": "assign",
         "instance": 12, "eventtype": "due", "timestart": int(end_of_today.timestamp()) + 3600,
         "timeduration": 0}]
    digest, _ = collect(moodle, empty_seen(), cfg)
    subject, html, text = emailer.render_morning(digest, [], cfg)
    assert subject == "☀ Due today: 1 item (1 not submitted)"
    assert "Assignment 1 is due" in html and "Lab 9" not in html


def test_check_mode_baseline_is_separate_from_digest(tmp_path):
    from eclass_digest.state import State

    import json

    old = empty_seen()  # a state file from before alerts existed
    old["courses"]["42"] = {"name": "X"}
    (tmp_path / "state.json").write_text(json.dumps({"seen": old}))
    s2 = State.load(tmp_path / "state.json")
    assert s2.alert_seen == s2.seen  # alerts start from the digest's baseline
    s2.alert_seen["courses"]["43"] = {"name": "Y"}
    assert "43" not in s2.seen["courses"]


def test_new_slides_get_their_own_summary_in_the_digest(cfg, monkeypatch, tmp_path):
    from eclass_digest import files, summarize

    cfg = dataclasses.replace(cfg, output_dir=tmp_path, ai_enabled=True)
    moodle = FakeMoodle()
    _, seen = collect(moodle, empty_seen(), cfg)
    moodle.contents[1]["modules"].append(
        {"id": 203, "name": "Week 2 slides", "modname": "resource", "instance": 5,
         "url": f"{BASE}/mod/resource/view.php?id=203",
         "contents": [{"type": "file", "filename": "week2.pdf", "filepath": "/",
                       "fileurl": pluginfile("1/mod_resource/content/1/week2.pdf"),
                       "filesize": 1000, "timemodified": NOW}]})
    digest, _ = collect(moodle, seen, cfg)

    calls = []
    monkeypatch.setattr(Moodle, "download", lambda self, url, max_bytes: b"%PDF-1.4 fake")
    monkeypatch.setattr(summarize, "summarize_file",
                        lambda cfg, c, m, f, data: calls.append(f.name) or "- Inheritance and `super()`")
    files.process_files(moodle, digest, cfg, want_text=True)
    assert calls == ["week2.pdf"]
    _, html, _ = emailer.render_digest(digest, [], cfg)
    assert "Inheritance and <code>super()</code>" in html
    assert 'kind="summary"' in build_prompt(digest, cfg)

    # A second run (e.g. a resend) reuses the cached summary
    digest, _ = collect(moodle, seen, cfg)
    files.process_files(moodle, digest, cfg, want_text=True)
    assert calls == ["week2.pdf"]


def test_pages_are_tracked_without_flagging_old_ones(cfg):
    moodle = FakeMoodle()
    page = {"id": 204, "name": "Week 1 notes", "modname": "page", "instance": 6,
            "url": f"{BASE}/mod/page/view.php?id=204",
            "contents": [{"type": "file", "filename": "index.html", "filepath": "/",
                          "fileurl": pluginfile("1/mod_page/content/3/index.html"),
                          "filesize": 500, "timemodified": NOW - 86400}]}
    moodle.contents[1]["modules"].append(page)
    _, seen = collect(moodle, empty_seen(), cfg)
    seen["modules"]["204"]["files"] = {}  # what older versions recorded for pages
    digest, seen = collect(moodle, seen, cfg)
    assert not digest.changed_modules()  # first look at the page's text is a baseline

    page["contents"][0]["timemodified"] = NOW  # the prof edits the notes
    digest, _ = collect(moodle, seen, cfg)
    assert {m.name: m.status for _, m in digest.changed_modules()} == {"Week 1 notes": "updated"}
