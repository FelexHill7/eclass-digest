"""Write Obsidian notes (with LaTeX math) for every lecture file in your tracked courses.

The evening digest already does this for newly posted lectures (and works through a few older
ones each night). Use this to do the whole backlog at once.

    python scripts/obsidian_export.py                    # all tracked courses
    python scripts/obsidian_export.py "CSSD 3131"        # one course
    python scripts/obsidian_export.py --list             # what would be written
    python scripts/obsidian_export.py --max 2            # just a couple, to try it
    python scripts/obsidian_export.py --force            # redo notes that already exist

Needs OBSIDIAN_VAULT (the folder of your Obsidian vault) and GEMINI_API_KEY in .env.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eclass_digest import obsidian  # noqa: E402
from eclass_digest.collect import collect  # noqa: E402
from eclass_digest.config import load_config  # noqa: E402
from eclass_digest.moodle import Moodle  # noqa: E402
from eclass_digest.state import empty_seen  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("course", nargs="?", help='only this course, e.g. "CSSD 3131"')
    ap.add_argument("--list", action="store_true", help="list the lecture files, write nothing")
    ap.add_argument("--max", type=int, help="write at most N notes")
    ap.add_argument("--force", action="store_true", help="rewrite notes that are already current")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("eclass_digest.collect", "eclass_digest.ai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config()
    if cfg.obsidian_vault is None:
        print("Set OBSIDIAN_VAULT in .env to your Obsidian vault folder first, e.g.\n"
              r"  OBSIDIAN_VAULT=C:\Users\you\Documents\Obsidian\School")
        return 1
    if not cfg.ai_enabled and not args.list:
        print("Add a free GEMINI_API_KEY to .env first (https://aistudio.google.com/apikey).")
        return 1

    moodle = Moodle(cfg.eclass_url, cfg.eclass_token)
    moodle.site_info()
    digest, _ = collect(moodle, empty_seen(), cfg)
    courses = [c for c in digest.courses
               if not args.course or args.course.lower().replace(" ", "")
               in f"{c.shortname} {c.fullname}".lower().replace(" ", "")]
    if not courses:
        print(f"No tracked course matches {args.course!r}. See scripts/list_courses.py")
        return 1

    vault = obsidian.Vault(cfg)
    print(f"Vault: {vault.root}")
    pending = 0
    for c in courses:
        files = list(obsidian.lecture_files(c, cfg.obsidian_match))
        print(f"\n{c.shortname}: {len(files)} lecture files")
        for _, module, f, n in files:
            done = "done   " if vault.is_current(f) and not args.force else "to do  "
            pending += done.startswith("to do")
            print(f"  {done}{obsidian.note_title(module, f, n)}")
    if args.list or not pending:
        print("\nNothing to write." if not pending else "")
        return 0

    print(f"\nWriting {min(pending, args.max or pending)} note(s); about a minute each on Gemini's free tier.\n")
    written = obsidian.sync(moodle, courses, cfg, limit=args.max, force=args.force)
    print(f"\nWrote {len(written)} note(s) to {vault.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
