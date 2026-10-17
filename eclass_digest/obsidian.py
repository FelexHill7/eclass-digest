"""Turn lecture slides into Obsidian notes with LaTeX math, one note per lecture file.

Layout inside the vault:
    <OBSIDIAN_FOLDER>/<course>/<course>.md         index linking every lecture in course order
    <OBSIDIAN_FOLDER>/<course>/<lecture>.md        study notes ($inline$ and $$display$$ math)
    <OBSIDIAN_FOLDER>/<course>/slides/<file>.pdf   the original slides, embedded in each note
    <OBSIDIAN_FOLDER>/.eclass-digest.json          which files have notes, and their versions

Notes you've edited in Obsidian are never overwritten: if the slides change later, the new
version is written next to yours as "<lecture> (updated <date>).md".
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from . import ai
from .collect import Course, FileItem, Module
from .config import Config
from .moodle import Moodle

log = logging.getLogger(__name__)

DOC_TYPES = {".pdf", ".pptx", ".docx"}
MAX_FILE_BYTES = 30 * 1024 * 1024
GENERIC_NAMES = {"slides", "slide", "lecture", "lecture slides", "notes", "lecture notes", "reading",
                 "handout", "presentation"}

NOTE_PROMPT = """The attached file is "{title}" from the course "{course}" ({section}).

Write study notes from it as Markdown for Obsidian:
- Start with a `> [!summary]` callout: two or three sentences on what this covers (no math in it).
- Then `## ` sections that follow the material's own structure. Explain each idea in clear sentences
  and bullets, the way a strong student's notes read, including what the diagrams and worked
  examples show.
- Math in LaTeX for MathJax: inline as $...$, and display equations alone on their own lines as
  $$...$$. Never use \\( \\) or \\[ \\]. Explain what each symbol means the first time it appears.
- Definitions go in `> [!definition]` callouts, worked examples in `> [!example]` callouts, and code
  in fenced code blocks with the language named.
- End with `## Key takeaways` (bullets), then `## Practice`: three to five questions answerable from
  this material, each as a `> [!question]- <question>` callout with the answer inside it.
- No top-level `# ` title (the file name is the title). Don't add material that isn't in the file.
"""


def _safe_name(text: str) -> str:
    """Obsidian-safe file name: no \\ / : * ? " < > | # ^ [ ]."""
    text = re.sub(r"\s*:\s*", " - ", text)
    text = re.sub(r'[\\/*?"<>|#^\[\]]+', " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-")[:120] or "Untitled"


def note_title(module: Module, f: FileItem, files_in_module: int) -> str:
    """"Lecture 3: Vectorization and Linear algebra, Thursday Sep.17, 2026" -> "Lecture 3 - Vectorization
    and Linear algebra"; a generic "Slides" in section "Logic Programming › Week 5" -> "Week 5 - Slides"."""
    name = module.name
    name = re.sub(r",?\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s+[A-Z][a-z]{2,8}\.?\s*\d{1,2}(,\s*\d{4})?\s*$",
                  "", name)  # trailing lecture date
    if name.strip().lower() in GENERIC_NAMES:
        name = f"{module.section.split('›')[-1].strip()} - {name.strip()}"
    if files_in_module > 1:  # a folder of several decks: one note each
        name = f"{name} - {Path(f.name).stem}"
    return _safe_name(name)


def course_folder(course: Course) -> str:
    return _safe_name(course.shortname)


def fix_math(md: str) -> str:
    """Normalize math delimiters to what Obsidian renders: \\( \\) -> $, \\[ \\] -> $$ on own lines."""
    md = re.sub(r"\\\((.+?)\\\)", lambda m: f"${m.group(1).strip()}$", md, flags=re.S)
    md = re.sub(r"\\\[(.+?)\\\]", lambda m: f"\n$$\n{m.group(1).strip()}\n$$\n", md, flags=re.S)
    # $$ ... $$ written inline on one line -> its own block, so Obsidian shows it as display math
    md = re.sub(r"(?m)^(?!\s*>)(.*\S.*?)\s*\$\$(.+?)\$\$\s*$", r"\1\n\n$$\2$$", md)
    md = re.sub(r"^\s*#\s+.*\n", "", md, count=1)  # drop a stray top-level title
    return md.strip() + "\n"


def _yaml(value) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON strings are valid YAML


def _frontmatter(course: Course, module: Module, f: FileItem, title: str, order: int,
                 now: datetime) -> str:
    tag = re.sub(r"[^a-z0-9]+", "-", course.shortname.lower()).strip("-")
    lines = [
        "---",
        f"course: {_yaml(course.shortname)}",
        f"course_title: {_yaml(course.fullname)}",
        f"lecture: {_yaml(module.name)}",
        f"section: {_yaml(module.section)}",
        f"order: {order}",
        f"source: {_yaml(module.url)}",
        f"slides: {_yaml(f.url)}",
        f"posted: {datetime.fromtimestamp(f.timemodified).date().isoformat() if f.timemodified else ''}",
        f"generated: {now.date().isoformat()}",
        f"tags: [eclass, {tag}, lecture-notes]",
        "---",
    ]
    return "\n".join(lines) + "\n"


class Vault:
    def __init__(self, cfg: Config):
        self.root = cfg.obsidian_vault / cfg.obsidian_folder
        self.manifest_path = self.root / ".eclass-digest.json"
        try:
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.manifest = {}

    @staticmethod
    def key(f: FileItem) -> str:
        return f.url

    def is_current(self, f: FileItem) -> bool:
        entry = self.manifest.get(self.key(f))
        return bool(entry) and entry.get("version") == f.timemodified

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(self.manifest, indent=1, ensure_ascii=False),
                                      encoding="utf-8")

    def write_note(self, course: Course, module: Module, f: FileItem, title: str, order: int,
                   body: str, pdf: bytes | None, now: datetime) -> Path:
        folder = self.root / course_folder(course)
        folder.mkdir(parents=True, exist_ok=True)
        embed = ""
        if pdf is not None:
            name = Path(f.name)
            slides = folder / "slides" / f"{_safe_name(name.stem)}{name.suffix.lower()}"
            slides.parent.mkdir(exist_ok=True)
            slides.write_bytes(pdf)
            embed = f"\n## Slides\n\n![[{slides.name}]]\n"
        content = (_frontmatter(course, module, f, title, order, now) + "\n" + fix_math(body) + embed
                   + f"\n---\n[Open in eClass]({module.url}) · [[{course_folder(course)}|Course index]]\n")

        path = folder / f"{title}.md"
        entry = self.manifest.get(self.key(f))
        if path.exists() and entry and _hash(path.read_text(encoding="utf-8")) != entry.get("hash"):
            # You've edited the earlier note in Obsidian; keep yours and put the new one beside it.
            path = folder / f"{title} (updated {now.date().isoformat()}).md"
            log.info("Kept your edited note; wrote the new version to %s", path.name)
        path.write_text(content, encoding="utf-8")
        self.manifest[self.key(f)] = {
            "course": course.shortname, "course_title": course.fullname, "title": path.stem,
            "path": str(path.relative_to(self.root)).replace("\\", "/"), "order": order,
            "version": f.timemodified, "hash": _hash(content),
        }
        return path

    def write_index(self, course: Course, now: datetime) -> Path:
        notes = sorted((e for e in self.manifest.values() if e["course"] == course.shortname),
                       key=lambda e: e["order"])
        folder = self.root / course_folder(course)
        folder.mkdir(parents=True, exist_ok=True)
        lines = ["---", f"course: {_yaml(course.shortname)}", f"course_title: {_yaml(course.fullname)}",
                 "tags: [eclass, course-index]", "---", "",
                 f"> [!info] {course.fullname}", f"> Notes generated from the eClass slides. "
                 f"[Open the course in eClass]({course.url})", "", "## Lectures", ""]
        lines += [f"- [[{e['title']}]]" for e in notes]
        lines += ["", f"*Updated {now:%b %d, %Y}.*", ""]
        path = folder / f"{course_folder(course)}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def lecture_files(course: Course, pattern: str):
    """(order, module, file) for each lecture document in the course, in course order."""
    regex = re.compile(pattern, re.I) if pattern else None
    order = 0
    for module in course.modules:
        if module.modname not in ("resource", "folder"):
            continue
        docs = [f for f in module.files if Path(f.name).suffix.lower() in DOC_TYPES]
        for f in docs:
            order += 1
            if regex is None or regex.search(f"{module.section} {module.name} {f.name}"):
                yield order, module, f, len(docs)


def sync(moodle: Moodle, courses: list[Course], cfg: Config, limit: int | None = None,
         force: bool = False) -> list[Path]:
    """Write notes for lecture files that don't have a current one yet. Newest first, so freshly
    posted slides are done before an older backlog; `limit` caps how many per run."""
    vault = Vault(cfg)
    now = datetime.now()
    todo = [(c, o, m, f, n) for c in courses for o, m, f, n in lecture_files(c, cfg.obsidian_match)
            if force or not vault.is_current(f)]
    todo.sort(key=lambda t: t[3].timemodified, reverse=True)
    written, touched = [], set()
    for course, order, module, f, n in todo[:limit]:
        title = note_title(module, f, n)
        try:
            data = moodle.download(f.download_url, MAX_FILE_BYTES)
            body = _generate(cfg, course, module, f, title, data)
        except Exception as e:  # one lecture failing shouldn't stop the rest
            log.warning("No Obsidian note for %s: %s", title, e)
            continue
        pdf = data if cfg.obsidian_attach_slides and f.name.lower().endswith(".pdf") else None
        written.append(vault.write_note(course, module, f, title, order, body, pdf, now))
        vault.save()  # after each note, so an interrupted run keeps what's done
        touched.add(course.id)
        log.info("Obsidian note: %s", written[-1].name)
    for course in courses:
        if course.id in touched or not (vault.root / course_folder(course) / f"{course_folder(course)}.md").exists():
            if any(e["course"] == course.shortname for e in vault.manifest.values()):
                vault.write_index(course, now)
    return written


def _generate(cfg: Config, course: Course, module: Module, f: FileItem, title: str, data: bytes) -> str:
    prompt = NOTE_PROMPT.format(title=title, course=course.fullname or course.shortname,
                                section=module.section)
    if f.name.lower().endswith(".pdf"):
        parts = [{"type": "pdf", "data": data, "name": f.name}, {"type": "text", "text": prompt}]
    else:
        from .files import extract_text

        parts = [{"type": "text", "text": f"<file name=\"{f.name}\">\n{extract_text(data, f.name)}\n</file>\n\n{prompt}"}]
    result = ai.generate(cfg, parts)
    if not result.text:
        raise ai.AIError("empty answer")
    return result.text
