"""Download new course files: pull their text for the AI summary and optionally keep a local copy."""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from .collect import Digest, FileItem
from .config import Config
from .moodle import Moodle

log = logging.getLogger(__name__)

SUMMARIZABLE = {".pdf", ".pptx", ".docx", ".txt", ".md"}
MAX_SUMMARY_FILES = 15  # per run, so a huge course dump doesn't blow up the bill
EXCERPT_CHARS = 20_000  # per file; the prompt tells Claude these are excerpts
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


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
    return ""


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .") or "_"


def process_files(moodle: Moodle, digest: Digest, cfg: Config, want_text: bool) -> None:
    summarized = 0
    for course in digest.courses:
        for module in course.modules:
            for f in module.files:
                ext = Path(f.name).suffix.lower()
                text_wanted = (want_text and f.status and ext in SUMMARIZABLE
                               and summarized < MAX_SUMMARY_FILES)
                target = None
                if cfg.download_files:
                    target = (cfg.download_dir / _safe(course.shortname) / _safe(module.section)
                              / Path(*[_safe(p) for p in f.name.split("/")]))
                    if target.exists() and not f.status:
                        target = None
                if not (text_wanted or target):
                    continue
                try:
                    data = moodle.download(f.download_url, MAX_DOWNLOAD_BYTES)
                except Exception as e:  # one bad file shouldn't sink the digest
                    log.warning("Download failed for %s: %s", f.name, e)
                    continue
                if target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                if text_wanted:
                    summarized += 1
                    _attach_text(f, data)


def _attach_text(f: FileItem, data: bytes) -> None:
    try:
        text = extract_text(data, f.name)
    except Exception as e:
        log.warning("Couldn't read text from %s: %s", f.name, e)
        return
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    f.text = text[:EXCERPT_CHARS]
