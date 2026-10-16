"""Show which eClass courses the digest tracks, and which it doesn't (and why).

    python scripts/list_courses.py          # tracked courses + this year's untracked ones
    python scripts/list_courses.py --all    # also list every past course
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eclass_digest.collect import INCLUDE_COURSES_HELP, _clean, _matches, course_label  # noqa: E402
from eclass_digest.collect import tracked_courses  # noqa: E402
from eclass_digest.config import load_config  # noqa: E402
from eclass_digest.moodle import Moodle, MoodleError  # noqa: E402


def _label(c: dict) -> str:
    return course_label(c.get("shortname", ""), _clean(c.get("fullname")))


def _title(c: dict) -> str:
    # "LE/CSSD 3101 A - Software Requirements & Analysis (Fall 2026-2027)" -> the part after " - "
    full = _clean(c.get("fullname"))
    return full.split(" - ", 1)[1] if " - " in full else full


def _row(c: dict, note: str) -> str:
    return f"  {_label(c):<16} {_title(c)[:52]:<52}  {note}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also list courses from past years")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.eclass_token:
        print("No eClass token yet. Run: python scripts/setup_wizard.py")
        return 1
    moodle = Moodle(cfg.eclass_url, cfg.eclass_token)
    try:
        who = moodle.site_info().get("fullname")
    except MoodleError as e:
        print(f"eClass rejected the token ({e}). Run: python scripts/get_eclass_token.py")
        return 1

    status: dict[int, str] = {}
    for classification in ("past", "future", "inprogress"):  # later ones win
        for c in moodle.courses(classification):
            status[c["id"]] = classification.replace("inprogress", "in progress")
    everything = {c["id"]: c for c in moodle.courses("allincludinghidden")}
    tracked = {c["id"]: c for c in tracked_courses(moodle, cfg)}

    print(f"eClass courses for {who}\n")
    print(f"TRACKED ({len(tracked)})  - in your daily digest and calendar")
    for cid, c in sorted(tracked.items(), key=lambda x: _label(x[1])):
        why = "in progress" if status.get(cid) == "in progress" else \
            f"INCLUDE_COURSES (eClass says {status.get(cid, 'hidden')})"
        try:
            moodle.course_contents(cid)
            note = why
        except MoodleError as e:
            note = f"{why}; eClass won't let you open it, so it's skipped ({e.errorcode})"
        print(_row(c, note))

    # Courses from this academic year that aren't tracked. York shortnames start with the year
    # the academic year began ("2026_..." for Fall 2026 - Winter 2027).
    today = date.today()
    year = str(today.year if today.month >= 8 else today.year - 1)
    untracked = [c for cid, c in everything.items() if cid not in tracked]
    excluded = [c for c in untracked if cfg.exclude_courses and _matches(c, cfg.exclude_courses)]
    others = [c for c in untracked if c not in excluded]
    if not args.all:
        has_year = [c for c in others if re.match(r"\d{4}_", c.get("shortname", ""))]
        if has_year:
            others = [c for c in others if c.get("shortname", "").startswith(year + "_")]

    print(f"\nNOT TRACKED ({len(others)}{'' if args.all else f', {year}-{int(year) + 1} only'})")
    for c in sorted(others, key=_label):
        print(_row(c, f"eClass says {status.get(c['id'], 'hidden')}"))
    if excluded:
        print(f"\nEXCLUDED by EXCLUDE_COURSES ({len(excluded)})")
        for c in sorted(excluded, key=_label):
            print(_row(c, f"eClass says {status.get(c['id'], 'hidden')}"))

    print(f"\n{INCLUDE_COURSES_HELP}")
    print(f"Currently: INCLUDE_COURSES={', '.join(cfg.include_courses) or '(empty)'}"
          f"   EXCLUDE_COURSES={', '.join(cfg.exclude_courses) or '(empty)'}")
    if not args.all:
        print("Add --all to see courses from past years too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
