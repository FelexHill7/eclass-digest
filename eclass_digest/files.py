"""Download new course files: summarize them for the digest and optionally keep a local copy."""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

from .collect import Course, Digest, FileItem, Module, html_to_text
from .config import Config
from .moodle import Moodle

log = logging.getLogger(__name__)

SUMMARIZABLE = {".pdf", ".pptx", ".docx", ".txt", ".md", ".html"}
MAX_SUMMARY_FILES = 8  # per run, to stay well inside the AI free tier's limits
EXCERPT_CHARS = 20_000  # per file, for the text fallback; the prompt says these are excerpts
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_AI_PDF_BYTES = 30 * 1024 * 1024


def extract_text(data: bytes, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if ext == ".pptx":
        from pptx import Presentation

        slides = []
        for i, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
            parts = [s.text_frame.text for s in slide.shapes if s.has_text_frame and s.text_frame.text]
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text:
                parts.append("Notes: " + slide.notes_slide.notes_text_frame.text)
            slides.append(f"[Slide {i}] " + "\n".join(parts))
        return "\n\n".join(slides)
    if ext == ".docx":
        import docx

        return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs if p.text)
    if ext in (".txt", ".md"):
        return data.decode("utf-8", errors="replace")
    if ext == ".html":
        return html_to_text(data.decode("utf-8", errors="replace"))
    return ""


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .") or "_"


class SummaryCache:
    """File summaries already made, keyed by file and version, so a re-run never redoes them."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def key(f: FileItem) -> str:
        return f"{f.url}@{f.timemodified}"

    def get(self, f: FileItem) -> str:
        return self.data.get(self.key(f), "")

    def put(self, f: FileItem, summary: str) -> None:
        self.data[self.key(f)] = summary
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


def process_files(moodle: Moodle, digest: Digest, cfg: Config, want_text: bool) -> None:
    """For new or updated files: an AI summary of each (slides read as documents, so diagrams
    and equations count), plus a local copy when DOWNLOAD_FILES is on."""
    cache = SummaryCache(cfg.output_dir / "summary-cache.json")
    summarized = 0
    for course in digest.courses:
        for module in course.modules:
            for f in module.files:
                ext = Path(f.name).suffix.lower()
                wanted = bool(want_text and f.status and ext in SUMMARIZABLE)
                if wanted and (cached := cache.get(f)):
                    f.summary, wanted = cached, False
                wanted = wanted and summarized < MAX_SUMMARY_FILES
                target = None
                if cfg.download_files and module.modname != "page":
                    target = (cfg.download_dir / _safe(course.shortname) / _safe(module.section)
                              / Path(*[_safe(p) for p in f.name.split("/")]))
                    if target.exists() and not f.status:
                        target = None
                if not (wanted or target):
                    continue
                try:
                    data = moodle.download(f.download_url, MAX_DOWNLOAD_BYTES)
                except Exception as e:  # one bad file shouldn't sink the digest
                    log.warning("Download failed for %s: %s", f.name, e)
                    continue
                if target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                if wanted:
                    summarized += 1
                    _summarize(cfg, course, module, f, data, cache)


def _summarize(cfg: Config, course: Course, module: Module, f: FileItem, data: bytes,
               cache: SummaryCache) -> None:
    from .summarize import summarize_file

    try:
        f.summary = summarize_file(cfg, course, module, f, data)
        if f.summary:
            cache.put(f, f.summary)
            return
    except Exception as e:
        log.warning("AI couldn't summarize %s: %s", f.name, e)
    _attach_text(f, data)  # fall back to raw text for the overall summary


def _attach_text(f: FileItem, data: bytes) -> None:
    try:
        text = extract_text(data, f.name)
    except Exception as e:
        log.warning("Couldn't read text from %s: %s", f.name, e)
        return
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    f.text = text[:EXCERPT_CHARS]
