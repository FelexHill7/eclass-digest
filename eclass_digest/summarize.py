"""Turn the day's eClass changes into a short readable briefing with Gemini."""

from __future__ import annotations

import logging
from html import escape

from . import ai
from .collect import Digest
from .config import Config

log = logging.getLogger(__name__)

SYSTEM = """You write a short daily briefing for a York University student, based on what changed in their eClass (Moodle) courses since yesterday.

Write it in Markdown:
1. A two-to-four sentence overview of what matters most right now, starting with anything due within a day that isn't submitted.
2. "### Action items": a checklist (`- [ ]`) of concrete things to do, soonest first, with due dates written out (e.g. "Thu Oct 22, 11:59 PM"). Leave out deadlines already marked submitted.
3. One "### <course>" section per course that had activity: new grades and the gist of any feedback, what was announced or posted, and for new slides, notes or readings, two or three bullets on what they cover.

Skip courses with no activity. Link items with the URLs you're given. Only state what's in the material: file contents are excerpts and may be cut off, so summarize what's there and don't guess at the rest. It should take about two minutes to read. Use only headings, bullets, bold and links; no tables."""

def _when(dt) -> str:
    return dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def build_prompt(digest: Digest, cfg: Config) -> str:
    out = [f"Today is {digest.generated:%A, %B %d, %Y} ({cfg.timezone})."]

    due = digest.upcoming(cfg.deadline_window_days)
    out.append(f'\n<deadlines next_days="{cfg.deadline_window_days}">')
    for e in due:
        status = f" | {e.submission}" if e.submission else ""
        out.append(f"- {_when(e.start)} | {e.course_name or 'Personal'} | {e.name}{status} | {e.url}")
    out.append("</deadlines>")

    for course in digest.courses:
        anns = [a for a in course.announcements if a.status]
        mods = [m for m in course.modules if m.status]
        grades = [g for g in course.grades if g.status]
        if not (anns or mods or grades):
            continue
        out.append(f'\n<course name="{escape(course.shortname)}" title="{escape(course.fullname)}">')
        for g in grades:
            out.append(f'<grade item="{escape(g.name)}" grade="{escape(g.display)}" url="{g.url}">'
                       f'{g.feedback}</grade>')
        for a in anns:
            out.append(
                f'<announcement subject="{escape(a.subject)}" by="{escape(a.author)}" '
                f'posted="{_when(a.posted)}" url="{a.url}">\n{a.text}\n</announcement>'
            )
        for m in mods:
            out.append(f'<{m.status}_item type="{m.modname}" name="{escape(m.name)}" '
                       f'section="{escape(m.section)}" url="{m.url}">')
            for f in m.files:
                if f.status and f.summary:
                    out.append(f'<file name="{escape(f.name)}" url="{f.url}" kind="summary">\n'
                               f'{f.summary}\n</file>')
                elif f.status and f.text:
                    out.append(f'<file name="{escape(f.name)}" url="{f.url}" kind="excerpt">\n'
                               f'{f.text}\n</file>')
                elif f.status:
                    out.append(f'<file name="{escape(f.name)}" url="{f.url}"/>')
            out.append(f"</{m.status}_item>")
        out.append("</course>")
    return "\n".join(out)


FILE_PROMPT = """This was just posted in the course "{course}" ({section} > {module}): {name}.

In 3 to 5 short Markdown bullets for a student's email, say what it covers: the topics, the key
ideas, and any formulas or definitions worth knowing (write math in plain text, no LaTeX). Base it
only on what's in the file, including diagrams and worked examples. Bullets only, no heading or preamble."""


def summarize_file(cfg: Config, course, module, f, data: bytes) -> str:
    """Bullet points on what one new file covers. PDFs go to the model whole (diagrams included)."""
    from .files import MAX_AI_PDF_BYTES, extract_text

    prompt = FILE_PROMPT.format(course=course.fullname or course.shortname, section=module.section,
                                module=module.name, name=f.name)
    if f.name.lower().endswith(".pdf") and len(data) <= MAX_AI_PDF_BYTES:
        parts = [{"type": "pdf", "data": data, "name": f.name}, {"type": "text", "text": prompt}]
    else:
        text = extract_text(data, f.name).strip()
        if not text:
            return ""
        parts = [{"type": "text", "text": f'<file name="{escape(f.name)}">\n{text[:100_000]}\n</file>\n\n{prompt}'}]
    result = ai.generate(cfg, parts)
    log.info("Summarized %s (%s)", f.name, result.model)
    return result.text


def summarize(digest: Digest, cfg: Config) -> str:
    """Returns Markdown, or "" if there was nothing worth summarizing."""
    if not digest.has_news and not digest.upcoming(cfg.deadline_window_days):
        return ""
    result = ai.generate(cfg, [{"type": "text", "text": build_prompt(digest, cfg)}], system=SYSTEM)
    log.info("Summary (%s): %d input / %d output tokens",
             result.model, result.input_tokens, result.output_tokens)
    return result.text
