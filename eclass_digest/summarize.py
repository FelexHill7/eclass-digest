"""Turn the day's eClass changes into a short readable briefing with Claude."""

from __future__ import annotations

import logging
from html import escape

import anthropic

from .collect import Digest
from .config import Config

log = logging.getLogger(__name__)

SYSTEM = """You write a short daily briefing for a York University student, based on what changed in their eClass (Moodle) courses since yesterday.

Write it in Markdown:
1. A two-to-four sentence overview of what matters most today.
2. "### Action items": a checklist (`- [ ]`) of concrete things to do, soonest first, with due dates written out (e.g. "Thu Oct 22, 11:59 PM").
3. One "### <course>" section per course that had activity: what was announced or posted, and for new slides, notes or readings, two or three bullets on what they cover.

Skip courses with no activity. Link items with the URLs you're given. Only state what's in the material: file contents are excerpts and may be cut off, so summarize what's there and don't guess at the rest. It should take about two minutes to read. Use only headings, bullets, bold and links; no tables."""

# Models that accept output_config.effort and server-side fallbacks.
_EFFORT_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-5")
_FALLBACK_MODELS = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")


def _when(dt) -> str:
    return dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def build_prompt(digest: Digest, cfg: Config) -> str:
    out = [f"Today is {digest.generated:%A, %B %d, %Y} ({cfg.timezone})."]

    due = digest.upcoming(cfg.deadline_window_days)
    out.append(f'\n<deadlines next_days="{cfg.deadline_window_days}">')
    for e in due:
        out.append(f"- {_when(e.start)} | {e.course_name or 'Personal'} | {e.name} | {e.url}")
    out.append("</deadlines>")

    for course in digest.courses:
        anns = [a for a in course.announcements if a.status]
        mods = [m for m in course.modules if m.status]
        if not (anns or mods):
            continue
        out.append(f'\n<course name="{escape(course.shortname)}" title="{escape(course.fullname)}">')
        for a in anns:
            out.append(
                f'<announcement subject="{escape(a.subject)}" by="{escape(a.author)}" '
                f'posted="{_when(a.posted)}" url="{a.url}">\n{a.text}\n</announcement>'
            )
        for m in mods:
            out.append(f'<{m.status}_item type="{m.modname}" name="{escape(m.name)}" '
                       f'section="{escape(m.section)}" url="{m.url}">')
            for f in m.files:
                if f.status and f.text:
                    out.append(f'<file name="{escape(f.name)}" url="{f.url}">\n{f.text}\n</file>')
                elif f.status:
                    out.append(f'<file name="{escape(f.name)}" url="{f.url}"/>')
            out.append(f"</{m.status}_item>")
        out.append("</course>")
    return "\n".join(out)


def summarize(digest: Digest, cfg: Config) -> str:
    """Returns Markdown, or "" if there was nothing worth summarizing."""
    if not digest.has_news and not digest.upcoming(cfg.deadline_window_days):
        return ""

    client = anthropic.Anthropic()
    request = dict(
        model=cfg.claude_model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(digest, cfg)}],
    )
    if cfg.claude_model.startswith(_EFFORT_MODELS):
        request["output_config"] = {"effort": "medium"}

    if cfg.claude_model.startswith(_FALLBACK_MODELS):
        # If a safety classifier declines, the API retries on Anthropic's recommended model.
        response = client.beta.messages.create(
            **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
    else:
        response = client.messages.create(**request)

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarize today's content")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "max_tokens":
        text += "\n\n*(summary was cut off)*"
    log.info("Summary: %d input / %d output tokens",
             response.usage.input_tokens, response.usage.output_tokens)
    return text
