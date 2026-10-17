"""Pull courses, resources, announcements and calendar events from eClass and diff them."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .config import Config
from .moodle import Moodle, MoodleError
from .state import empty_seen

log = logging.getLogger(__name__)

# On the first run everything is "already seen"; announcements newer than this are still shown.
RECENT_ANNOUNCEMENT_DAYS = 7
SKIP_MODULES = {"label", "subsection"}  # subsections are expanded into their own sections
# eClass lists some course shells (often lab sections) that students can't open.
INACCESSIBLE = {"errorcoursecontextnotvalid", "requireloginerror", "coursehidden"}


@dataclass
class FileItem:
    name: str
    url: str  # opens in a logged-in browser
    download_url: str  # webservice URL, needs the token
    mimetype: str
    size: int
    timemodified: int
    status: str = ""  # "new" | "updated" | ""
    text: str = ""  # extracted text, filled in only for files we summarize


@dataclass
class Module:
    cmid: int
    name: str
    modname: str
    url: str
    section: str
    files: list[FileItem] = field(default_factory=list)
    status: str = ""


@dataclass
class Announcement:
    id: int
    subject: str
    author: str
    posted: datetime
    url: str
    text: str
    status: str = ""  # "new" | "updated" | "recent" | ""


@dataclass
class Event:
    id: int
    name: str
    course_id: int
    course_name: str
    start: datetime
    duration: int
    url: str
    eventtype: str
    modulename: str
    description: str
    instance: int = 0  # the assignment/quiz id behind the event
    submission: str = ""  # "submitted" | "draft" | "not submitted" | "" (unknown / not applicable)

    @property
    def is_deadline(self) -> bool:
        return self.eventtype in ("due", "close")

    @property
    def needs_submission(self) -> bool:
        return self.is_deadline and self.submission in ("not submitted", "draft")


@dataclass
class Grade:
    id: int
    name: str
    grade: str  # as eClass formats it, e.g. "97.00"
    max: str
    feedback: str
    graded: datetime | None
    url: str
    status: str = ""  # "new" | "updated" | ""

    @property
    def display(self) -> str:
        try:
            return f"{self.grade} / {float(self.max):g}"
        except ValueError:
            return self.grade


@dataclass
class Course:
    id: int
    shortname: str
    fullname: str
    url: str
    sections: list[tuple[str, list[Module]]] = field(default_factory=list)
    announcements: list[Announcement] = field(default_factory=list)
    grades: list[Grade] = field(default_factory=list)
    first_seen: bool = False

    @property
    def modules(self) -> list[Module]:
        return [m for _, mods in self.sections for m in mods]


@dataclass
class Digest:
    generated: datetime
    courses: list[Course]
    events: list[Event]
    events_ok: bool
    first_run: bool
    errors: list[str] = field(default_factory=list)
    summary_md: str = ""

    def changed_modules(self) -> list[tuple[Course, Module]]:
        return [(c, m) for c in self.courses for m in c.modules if m.status]

    def fresh_announcements(self) -> list[tuple[Course, Announcement]]:
        return [(c, a) for c in self.courses for a in c.announcements if a.status]

    def fresh_grades(self) -> list[tuple[Course, Grade]]:
        return [(c, g) for c in self.courses for g in c.grades if g.status]

    def upcoming(self, days: float) -> list[Event]:
        end = self.generated + timedelta(days=days)
        return [e for e in self.events if self.generated <= e.start <= end]

    def due_by_end_of(self, days_from_today: int) -> list[Event]:
        """Deadlines from now until midnight at the end of today (0), tomorrow (1), ..."""
        end = datetime.combine(self.generated.date() + timedelta(days=days_from_today + 1),
                               datetime.min.time(), self.generated.tzinfo)
        return [e for e in self.events if e.is_deadline and self.generated <= e.start < end]

    def urgent(self, hours: int) -> list[Event]:
        """Deadlines in the next `hours` that you haven't submitted yet."""
        return [e for e in self.upcoming(hours / 24) if e.needs_submission]

    @property
    def has_news(self) -> bool:
        return bool(self.changed_modules() or self.fresh_announcements() or self.fresh_grades())


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a"):
        href, label = a.get("href", ""), a.get_text(" ", strip=True)
        if href.startswith("http") and href != label:
            a.replace_with(f"{label} ({href})" if label else href)
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "• ")
    for block in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                                "ul", "ol", "table", "blockquote"]):
        block.insert_after("\n")
    text = soup.get_text()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _clean(text: str | None) -> str:
    """eClass sends names HTML-encoded ("Logic &#38; Proof")."""
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def course_label(shortname: str, fullname: str) -> str:
    """"2026_LE_CSSD_A3_3102__2_A_EN_A_LAB_01" -> "CSSD 3102 Lab"."""
    m = (re.search(r"\b([A-Z]{2,5})\s*(\d{4})\b", fullname)
         or re.search(r"_([A-Z]{2,5})_(?:[A-Z0-9]+_)?(\d{4})_", shortname))
    if not m:
        return shortname or fullname
    label = f"{m.group(1)} {m.group(2)}"
    if re.search(r"_LAB_|\(Lab\b", f"{shortname} {fullname}"):
        label += " Lab"
    return label


def ordered_sections(sections: list[dict]):
    """Yield (display name, section) in reading order.

    Since Moodle 4.5, a unit can hold subsections ("Functional Programming" > "Week 1").
    The API returns those as separate sections at the end, so put them back under their parent.
    """
    delegated = {s.get("itemid"): s for s in sections if s.get("component") == "mod_subsection"}
    used: set = set()

    def walk(sec, prefix):
        name = _clean(sec.get("name")) or "General"
        full = f"{prefix} › {name}" if prefix else name
        used.add(sec["id"])
        yield full, sec
        for m in sec.get("modules", []):
            child = delegated.get(m.get("instance")) if m.get("modname") == "subsection" else None
            if child is not None and child["id"] not in used:
                yield from walk(child, full)

    for sec in sections:
        if sec.get("component") != "mod_subsection":
            yield from walk(sec, "")
    for sec in sections:  # subsections whose parent we never saw
        if sec["id"] not in used:
            yield from walk(sec, "")


def _matches(course: dict, patterns: tuple[str, ...]) -> bool:
    """True if any pattern is the course's id or appears in its name ("CSSD 3101", "cssd3101")."""
    fullname = _clean(course.get("fullname"))
    names = f"{course.get('shortname', '')} {fullname} {course_label(course.get('shortname', ''), fullname)}"
    names = names.lower()
    squashed = names.replace(" ", "").replace("_", "")
    return any(p == str(course.get("id")) or p in names or p.replace(" ", "") in squashed
               for p in patterns)


INCLUDE_COURSES_HELP = ("To track a course, add it to INCLUDE_COURSES in .env by code, name or id, "
                        "e.g. INCLUDE_COURSES=CSSD 3104, 165414")


def tracked_courses(moodle: Moodle, cfg: Config) -> list[dict]:
    """Courses eClass lists under COURSE_CLASSIFICATION, plus any in INCLUDE_COURSES whatever their
    status (profs sometimes set dates that make a current course show up as "past" or "future")."""
    courses = {c["id"]: c for c in moodle.courses(cfg.course_classification)}
    if cfg.include_courses:
        for c in moodle.courses("allincludinghidden"):
            if c["id"] not in courses and _matches(c, cfg.include_courses):
                courses[c["id"]] = c
                log.info("Including %s (listed in INCLUDE_COURSES)", c.get("shortname"))
    return [c for c in courses.values() if not _matches(c, cfg.exclude_courses)]


def _module(moodle: Moodle, m: dict, section: str) -> Module:
    modname = m.get("modname", "")
    url = m.get("url") or f"{moodle.base}/mod/{modname}/view.php?id={m['id']}"
    files: list[FileItem] = []
    for c in m.get("contents") or []:
        if modname == "url" and c.get("type") == "url" and c.get("fileurl"):
            url = c["fileurl"]  # link straight to the external site
        elif modname in ("resource", "folder") and c.get("type") == "file":
            path = (c.get("filepath") or "/").strip("/")
            name = f"{path}/{c['filename']}" if path else c["filename"]
            files.append(
                FileItem(
                    name=name,
                    url=Moodle.browser_url(c["fileurl"]),
                    download_url=c["fileurl"],
                    mimetype=c.get("mimetype", ""),
                    size=int(c.get("filesize") or 0),
                    timemodified=int(c.get("timemodified") or 0),
                )
            )
    return Module(cmid=m["id"], name=_clean(m.get("name")) or "(untitled)", modname=modname, url=url,
                  section=section, files=files)


def collect(moodle: Moodle, seen: dict, cfg: Config) -> tuple[Digest, dict]:
    """Returns the digest plus the updated "seen" state (not saved until the email goes out)."""
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    seen = seen or empty_seen()
    first_run = not seen["courses"]
    new_seen = {"courses": {}, "modules": {}, "discussions": dict(seen["discussions"])}
    errors: list[str] = []

    raw_courses = tracked_courses(moodle, cfg)
    log.info("Found %d courses", len(raw_courses))

    courses: dict[int, Course] = {}
    names: dict[int, str] = {}  # every course's label, including ones we can't open
    cm_lookup: dict[tuple[str, int], int] = {}  # (modname, instance) -> cmid, for event links

    for c in raw_courses:
        cid = c["id"]
        known = str(cid) in seen["courses"]
        fullname = _clean(c.get("fullname"))
        course = Course(
            id=cid,
            shortname=course_label(c.get("shortname", ""), fullname) or f"Course {cid}",
            fullname=fullname,
            url=c.get("viewurl") or f"{moodle.base}/course/view.php?id={cid}",
            first_seen=not known,
        )
        courses[cid] = course
        names[cid] = course.shortname
        new_seen["courses"][str(cid)] = {"name": course.shortname}

        try:
            sections = moodle.course_contents(cid)
        except (MoodleError, OSError) as e:
            if isinstance(e, MoodleError) and e.errorcode in INACCESSIBLE:
                log.info("Skipping %s: eClass doesn't let students open it", course.shortname)
                del courses[cid]
                continue
            errors.append(f"Couldn't read contents of {course.shortname}: {e}")
            # Keep last run's view of this course so nothing looks "new" tomorrow by mistake.
            for cmid, info in seen["modules"].items():
                if info.get("course") == cid:
                    new_seen["modules"][cmid] = info
            continue

        for section_name, s in ordered_sections(sections):
            mods = []
            for m in s.get("modules", []):
                if m.get("modname") in SKIP_MODULES or not m.get("uservisible", True):
                    continue
                if m.get("instance") is not None:
                    cm_lookup[(m.get("modname"), m["instance"])] = m["id"]
                module = _module(moodle, m, section_name)
                prev = seen["modules"].get(str(module.cmid))
                if known:
                    if prev is None:
                        module.status = "new"
                        for f in module.files:
                            f.status = "new"
                    else:
                        for f in module.files:
                            before = prev["files"].get(f.name)
                            if before is None:
                                f.status = "new"
                            elif f.timemodified > before:
                                f.status = "updated"
                        if any(f.status for f in module.files):
                            module.status = "updated"
                new_seen["modules"][str(module.cmid)] = {
                    "course": cid,
                    "files": {f.name: f.timemodified for f in module.files},
                }
                mods.append(module)
            if mods:
                course.sections.append((section_name, mods))

    # --- Announcements (each course's "news" forum) -------------------------
    try:
        forums = moodle.forums(list(courses)) if courses else []
    except (MoodleError, OSError) as e:
        errors.append(f"Couldn't read announcements: {e}")
        forums = []
    for forum in forums:
        course = courses.get(forum.get("course"))
        if course is None or forum.get("type") != "news":
            continue
        try:
            discussions = moodle.discussions(forum["id"])
        except (MoodleError, OSError) as e:
            errors.append(f"Couldn't read announcements for {course.shortname}: {e}")
            continue
        for d in discussions:
            did = d.get("discussion") or d["id"]
            posted = datetime.fromtimestamp(d.get("created") or d.get("timemodified", 0), tz)
            modified = int(d.get("modified") or d.get("created") or 0)
            ann = Announcement(
                id=did,
                subject=_clean(d.get("subject") or d.get("name")) or "(no subject)",
                author=_clean(d.get("userfullname")),
                posted=posted,
                url=f"{moodle.base}/mod/forum/discuss.php?d={did}",
                text=html_to_text(d.get("message", "")),
            )
            prev = seen["discussions"].get(str(did))
            if not course.first_seen:
                if prev is None:
                    ann.status = "new"
                elif modified > prev:
                    ann.status = "updated"
            elif posted >= now - timedelta(days=RECENT_ANNOUNCEMENT_DAYS):
                ann.status = "recent"
            new_seen["discussions"][str(did)] = modified
            course.announcements.append(ann)
        course.announcements.sort(key=lambda a: a.posted, reverse=True)

    # --- Grades and feedback ---------------------------------------------------
    seen_grades = seen.get("grades", {})
    new_seen["grades"] = dict(seen_grades)
    user_id = moodle.user_id if moodle.user_id is not None else moodle.site_info()["userid"]
    for course in courses.values():
        try:
            items = moodle.grade_items(course.id, user_id)
        except (MoodleError, OSError) as e:
            log.info("No grades for %s: %s", course.shortname, e)
            continue
        for it in items:
            if it.get("itemtype") in ("course", "category") or it.get("gradeishidden"):
                continue
            value = (it.get("gradeformatted") or "").strip()
            feedback = html_to_text(it.get("feedback") or "")
            signature = f"{value}|{feedback}"
            key = str(it["id"])
            prev = seen_grades.get(key)
            new_seen["grades"][key] = signature
            if value in ("", "-") and not feedback:
                continue  # not graded yet
            module = it.get("itemmodule")
            url = (f"{moodle.base}/mod/{module}/view.php?id={it['cmid']}" if module and it.get("cmid")
                   else f"{moodle.base}/grade/report/user/index.php?id={course.id}")
            graded = it.get("gradedategraded")
            grade = Grade(id=it["id"], name=_clean(it.get("itemname")) or "(grade item)", grade=value,
                          max=str(it.get("grademax") or ""), feedback=feedback, url=url,
                          graded=datetime.fromtimestamp(graded, tz) if graded else None)
            # A course seen for the first time, or a first look at grades at all, is a baseline.
            if not course.first_seen and seen_grades and signature != prev:
                was_graded = prev is not None and not prev.startswith(("|", "-|"))
                grade.status = "updated" if was_graded else "new"
            course.grades.append(grade)
        course.grades.sort(key=lambda g: g.graded or now, reverse=True)

    # --- Calendar: deadlines, quizzes, exams, course events -----------------
    events: list[Event] = []
    events_ok = True
    try:
        raw_events = moodle.calendar_events(
            list(courses),
            int((now - timedelta(days=1)).timestamp()),
            int((now + timedelta(days=cfg.lookahead_days)).timestamp()),
        )
    except (MoodleError, OSError) as e:
        errors.append(f"Couldn't read the eClass calendar: {e}")
        raw_events, events_ok = [], False
    for ev in raw_events:
        if not ev.get("visible", 1):
            continue
        course = courses.get(ev.get("courseid"))
        modname, instance = ev.get("modulename") or "", ev.get("instance")
        cmid = cm_lookup.get((modname, instance))
        if cmid:
            url = f"{moodle.base}/mod/{modname}/view.php?id={cmid}"
        else:
            url = f"{moodle.base}/calendar/view.php?view=day&time={ev['timestart']}"
        events.append(
            Event(
                id=ev["id"],
                name=_clean(ev.get("name")) or "(event)",
                course_id=ev.get("courseid") or 0,
                course_name=names.get(ev.get("courseid"), ""),
                start=datetime.fromtimestamp(ev["timestart"], tz),
                duration=int(ev.get("timeduration") or 0),
                url=url,
                eventtype=ev.get("eventtype", ""),
                modulename=modname,
                description=html_to_text(ev.get("description", "")),
                instance=int(instance or 0),
            )
        )
    events.sort(key=lambda e: e.start)

    # --- Have you submitted the deadlines coming up? -------------------------
    soon = now + timedelta(days=cfg.deadline_window_days)
    for ev in events:
        if not (ev.is_deadline and now <= ev.start <= soon and ev.course_id in courses):
            continue
        try:
            if ev.modulename == "assign" and ev.instance:
                ev.submission = moodle.assignment_submission(ev.instance)
            elif ev.modulename == "quiz" and ev.instance:
                ev.submission = moodle.quiz_submission(ev.instance)
        except (MoodleError, OSError) as e:
            log.info("Couldn't check submission for %s: %s", ev.name, e)

    digest = Digest(
        generated=now,
        courses=list(courses.values()),
        events=events,
        events_ok=events_ok,
        first_run=first_run,
        errors=errors,
    )
    return digest, new_seen
