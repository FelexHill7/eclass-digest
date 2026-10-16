"""One-off: summarize a course's lecture files with Gemini and email the notes.

Unlike the daily digest (which only reads new files, as text), this reads every matching file
and sends PDFs to the AI as documents, so diagrams, charts and equations are included.
Uses Gemini's free tier (GEMINI_API_KEY in .env).

    python scripts/summarize_course.py "CSSD 3131"                  # all course documents
    python scripts/summarize_course.py "CSSD 3131" --match lecture  # only names containing "lecture"
    python scripts/summarize_course.py "CSSD 3131" --list           # just list the files
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import markdown  # noqa: E402

from eclass_digest import ai, emailer  # noqa: E402
from eclass_digest.collect import _clean, _module, course_label, ordered_sections  # noqa: E402
from eclass_digest.config import load_config  # noqa: E402
from eclass_digest.files import extract_text  # noqa: E402
from eclass_digest.moodle import Moodle  # noqa: E402

DOC_TYPES = {".pdf", ".pptx", ".docx"}
MAX_FILE_BYTES = 30 * 1024 * 1024

LECTURE_PROMPT = """These are the slides for one lecture of the course "{course}": {title}.

Summarize them as study notes in Markdown:
- One or two sentences on what the lecture is about.
- "**Key ideas**": the main concepts, each explained in a sentence or two.
- "**Formulas & definitions**": the important ones, written in plain text or simple notation
  (no LaTeX), with what each symbol means. Skip this part if there aren't any.
- "**Know for the exam**": three to five things a student should be able to do or explain.

Use what's on the slides, including diagrams and worked examples, and don't add material that
isn't there. No headings above level 4. Start directly with the summary, no preamble."""

OVERVIEW_PROMPT = """Below are study notes for each lecture of "{course}" so far.
Write a short course overview in Markdown: a paragraph on how the lectures build on each other,
then a "**Study checklist**" of the 8 to 12 most important skills across all lectures, each tagged
with its lecture number. No headings above level 4. Start directly, no preamble.

{notes}"""


def _parts(data: bytes, filename: str, prompt: str) -> list[dict]:
    if filename.lower().endswith(".pdf"):
        return [{"type": "pdf", "data": data, "name": filename}, {"type": "text", "text": prompt}]
    text = extract_text(data, filename)
    return [{"type": "text", "text": f'<file name="{filename}">\n{text}\n</file>\n\n{prompt}'}]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("course", help='part of the course name, e.g. "CSSD 3131" or "Machine Learning"')
    ap.add_argument("--match", help="only modules/files whose name contains this (case-insensitive)")
    ap.add_argument("--list", action="store_true", help="list the matching files, then stop")
    ap.add_argument("--max", type=int, help="only the first N files (e.g. --max 3 to test)")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.ai_enabled:
        print("No AI key set. Add a free GEMINI_API_KEY to .env (https://aistudio.google.com/apikey).")
        return 1
    moodle = Moodle(cfg.eclass_url, cfg.eclass_token)

    q = args.course.lower()
    matches = []
    for c in moodle.courses("allincludinghidden"):  # any course you're in, whatever its status
        names = f"{c['shortname']} {_clean(c['fullname'])} {course_label(c['shortname'], _clean(c['fullname']))}"
        if q in names.lower() and "_LAB_" not in c["shortname"]:
            matches.append(c)
    if len(matches) != 1:
        print(f"Found {len(matches)} courses matching {args.course!r}; be more specific.")
        return 1
    c = matches[0]
    full_title = _clean(c["fullname"])
    course = course_label(c["shortname"], full_title)

    items = []
    for section, sec in ordered_sections(moodle.course_contents(c["id"])):
        for raw in sec.get("modules", []):
            if raw.get("modname") not in ("resource", "folder") or not raw.get("uservisible", True):
                continue
            mod = _module(moodle, raw, section)
            for f in mod.files:
                wanted = not args.match or args.match.lower() in f"{mod.name} {f.name}".lower()
                if Path(f.name).suffix.lower() in DOC_TYPES and wanted:
                    items.append((mod, f))
    if not items:
        print("No matching lecture files.")
        return 1
    if args.max:
        items = items[:args.max]

    print(f"{full_title}: {len(items)} files")
    for mod, f in items:
        print(f"  {f.size / 1e6:>5.1f} MB  {mod.name[:70]}")
    if args.list:
        return 0

    # Finished lectures are cached, so a re-run (or a bigger --max) doesn't redo them.
    cache_file = cfg.output_dir / "notes-cache" / f"{c['id']}.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    notes, used_in, used_out, models_used = [], 0, 0, set()
    try:
        for i, (mod, f) in enumerate(items, 1):
            key = f"{f.url}@{f.timemodified}"
            if key in cache:
                print(f"Cached {i}/{len(items)}: {mod.name[:70]}")
                notes.append((mod, f, cache[key]["text"]))
                models_used.add(cache[key]["model"])
                continue
            print(f"Summarizing {i}/{len(items)}: {mod.name[:70]}", flush=True)
            data = moodle.download(f.download_url, MAX_FILE_BYTES)
            parts = _parts(data, f.name, LECTURE_PROMPT.format(course=full_title, title=mod.name))
            try:
                r = ai.generate(cfg, parts)
            except ai.AIError as e:
                print(f"  skipped: {e}")
                notes.append((mod, f, f"*Couldn't summarize this file: {e}*"))
                continue
            used_in += r.input_tokens
            used_out += r.output_tokens
            models_used.add(r.model)
            notes.append((mod, f, r.text or "*No summary came back for this file.*"))
            cache[key] = {"text": notes[-1][2], "model": r.model}
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache), encoding="utf-8")
    except KeyboardInterrupt:  # Ctrl+C: send what's finished
        print(f"\nStopped; sending the {len(notes)} finished.")
    if not notes:
        return 1

    joined = "\n\n".join(f"## {m.name}\n{t}" for m, _, t in notes)
    r = ai.generate(cfg, [{"type": "text", "text": OVERVIEW_PROMPT.format(course=full_title, notes=joined)}])
    used_in += r.input_tokens
    used_out += r.output_tokens
    models = ", ".join(sorted(models_used | {r.model}))

    def md(s: str) -> str:
        html = markdown.markdown(s, extensions=["sane_lists"])
        return html.replace("<a ", f'<a style="color:{emailer.RED}" ')

    now = datetime.now(ZoneInfo(cfg.timezone))
    h = [f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
         f'max-width:720px;margin:0 auto;color:#1f1f1f;font-size:14px;line-height:1.55">',
         f'<div style="background:{emailer.RED};color:#fff;padding:16px 20px;border-radius:6px">'
         f'<div style="font-size:20px;font-weight:600">{escape(course)} lecture notes</div>'
         f'<div style="opacity:.9">{escape(full_title)}</div></div>',
         emailer._h2("Course overview"), md(r.text)]
    for mod, f, text in notes:
        h.append(emailer._h2(emailer._link(mod.url, mod.name)))
        h.append(f'<div style="color:{emailer.MUTED};font-size:12px">{escape(mod.section)} · '
                 f'{emailer._link(f.url, f.name)}</div>')
        h.append(md(text))
    h.append(f'<p style="color:{emailer.MUTED};font-size:12px;margin-top:32px">AI-generated from '
             f'the slides by {escape(models)}; check anything important against the '
             f'originals. Generated {now:%b %d, %Y}.</p></div>')
    html = "\n".join(h)
    text = f"{course} lecture notes\n\n{r.text}\n\n{joined}"

    out = cfg.output_dir / f"notes-{course.replace(' ', '')}-{now:%Y-%m-%d}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"<!doctype html><meta charset='utf-8'>{html}", encoding="utf-8")
    subject = f"📚 {course} lecture notes: {len(notes)} lectures summarized"
    creds = None
    if cfg.email_method == "gmail_api":
        from eclass_digest.google_auth import load_credentials

        creds = load_credentials(cfg)
    emailer.send(cfg, emailer.build_message(cfg, subject, html, text), creds)
    print(f"\nEmailed {cfg.digest_to}. Tokens: {used_in:,} in / {used_out:,} out ({models})."
          f"\nSaved a copy to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
