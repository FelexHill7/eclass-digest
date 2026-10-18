"""A browsable "Course resources.html" page with a dropdown per course, unit and week.

Gmail can't collapse sections inside an email, so the email keeps a one-line-per-course summary
and this page (saved next to the downloaded materials) holds the full, collapsible list. Each file
links to eClass and, when it's been downloaded, to the copy on your computer.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

from .collect import Course, Digest, Module
from .config import Config

log = logging.getLogger(__name__)

PAGE_NAME = "Course resources.html"
ICONS = {"assign": "📝", "quiz": "❓", "forum": "💬", "url": "🔗", "page": "📃", "folder": "📁",
         "resource": "📄", "lti": "🧩", "h5pactivity": "🧩", "book": "📚", "choice": "☑️",
         "choicegroup": "☑️", "feedback": "🗳️", "zoom": "🎥"}

STYLE = """
:root { --bg:#fff; --fg:#1f1f1f; --muted:#6b6b6b; --line:#e5e5e5; --card:#fafafa; --accent:#E31837; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161616; --fg:#ececec; --muted:#9a9a9a; --line:#333; --card:#1f1f1f; --accent:#ff5a6e; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width:820px; margin:0 auto; padding:24px 16px 64px; }
header { background:#E31837; color:#fff; border-radius:8px; padding:16px 20px; margin-bottom:16px; }
header h1 { margin:0; font-size:22px; } header p { margin:2px 0 0; opacity:.9; }
input[type=search] { width:100%; padding:10px 12px; font-size:15px; border:1px solid var(--line);
       border-radius:6px; background:var(--card); color:var(--fg); margin-bottom:12px; }
details { border-left:2px solid var(--line); margin:4px 0 4px 2px; padding-left:12px; }
details.course { border:1px solid var(--line); border-radius:8px; padding:6px 14px; margin:10px 0;
       background:var(--card); }
summary { cursor:pointer; padding:4px 0; font-weight:600; list-style-position:outside; }
details.course > summary { font-size:17px; }
summary .meta { font-weight:400; color:var(--muted); font-size:13px; margin-left:6px; }
ul { list-style:none; margin:2px 0 6px; padding-left:4px; }
li { padding:3px 0; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
.local { font-size:12px; margin-left:6px; } .muted { color:var(--muted); font-size:13px; }
.hidden { display:none; }
"""

SCRIPT = """
const box = document.getElementById('q');
box.addEventListener('input', () => {
  const q = box.value.trim().toLowerCase();
  document.querySelectorAll('li[data-text]').forEach(li => {
    li.classList.toggle('hidden', q && !li.dataset.text.includes(q));
  });
  document.querySelectorAll('details').forEach(d => {
    const any = d.querySelector('li[data-text]:not(.hidden)');
    d.classList.toggle('hidden', q && !any);
    if (q) d.open = !!any;
  });
});
"""


def page_path(cfg: Config) -> Path:
    return (cfg.download_dir if cfg.download_files else cfg.output_dir) / PAGE_NAME


_manifests: dict = {}


def _local_link(cfg: Config, page_dir: Path, course: Course, module: Module, f) -> str:
    if not cfg.download_files:
        return ""
    from .files import DownloadManifest

    manifest = _manifests.setdefault(cfg.download_dir, DownloadManifest(cfg))
    path = manifest.local_path(module, f)  # a re-posted file points at the one stored copy
    if path is None:
        return ""
    rel = os.path.relpath(path, page_dir).replace("\\", "/")
    return f' <a class="local" href="{quote(rel)}">open copy</a>'


def _module_items(cfg: Config, page_dir: Path, course: Course, module: Module) -> list[str]:
    icon = ICONS.get(module.modname, "•")
    items = [f'<li data-text="{escape(module.name.lower())}">{icon} '
             f'<a href="{escape(module.url)}">{escape(module.name)}</a></li>']
    files = module.files if module.modname != "page" else []
    if module.modname == "folder" or len(files) > 1:
        for f in files:
            items.append(f'<li data-text="{escape((module.name + " " + f.name).lower())}" style="padding-left:22px">'
                         f'📎 <a href="{escape(f.url)}">{escape(f.name)}</a>'
                         f'{_local_link(cfg, page_dir, course, module, f)}</li>')
    elif files:
        items[0] = items[0].replace("</li>", _local_link(cfg, page_dir, course, module, files[0]) + "</li>")
    return items


def _section_tree(course: Course) -> dict:
    """{"Functional Programming": {"Week 1": {...}, "_modules": [...]}} in eClass order."""
    root: dict = {}
    for section, mods in course.sections:
        node = root
        for part in [p.strip() for p in section.split("›") if p.strip()]:
            node = node.setdefault(part, {})
        node.setdefault("_modules", []).extend(mods)
    return root


def _render_tree(cfg: Config, page_dir: Path, course: Course, node: dict) -> list[str]:
    h = []
    mods = node.get("_modules", [])
    if mods:
        h.append("<ul>")
        for m in mods:
            h += _module_items(cfg, page_dir, course, m)
        h.append("</ul>")
    for name, child in node.items():
        if name == "_modules":
            continue
        count = _count(child)
        h.append(f'<details><summary>{escape(name)}<span class="meta">{count} item{"s" * (count != 1)}'
                 f'</span></summary>')
        h += _render_tree(cfg, page_dir, course, child)
        h.append("</details>")
    return h


def _count(node: dict) -> int:
    return len(node.get("_modules", [])) + sum(_count(v) for k, v in node.items() if k != "_modules")


def write(digest: Digest, cfg: Config) -> Path:
    _manifests.clear()  # re-read what's been downloaded
    path = page_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = digest.generated
    h = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
         f"<title>eClass course resources</title><style>{STYLE}</style></head><body><main>",
         f"<header><h1>eClass course resources</h1><p>Updated {now:%a %b %d, %I:%M %p}</p></header>".replace(" 0", " "),
         "<input id='q' type='search' placeholder='Search files and activities…' autocomplete='off'>"]
    for course in digest.courses:
        files = sum(len(m.files) for m in course.modules if m.modname != "page")
        h.append(f'<details class="course"><summary>{escape(course.shortname)} · {escape(course.title)}'
                 f'<span class="meta">{len(course.modules)} items · {files} files</span></summary>')
        h.append(f'<p class="muted"><a href="{escape(course.url)}">Open the course in eClass</a></p>')
        if course.grades:
            h.append(f'<details><summary>Grades<span class="meta">{len(course.grades)}</span></summary><ul>')
            h += [f'<li data-text="{escape(g.name.lower())}">🎓 <a href="{escape(g.url)}">{escape(g.name)}</a>'
                  f' <b>{escape(g.display)}</b></li>' for g in course.grades]
            h.append("</ul></details>")
        if course.announcements:
            h.append(f'<details><summary>Announcements<span class="meta">{len(course.announcements)}'
                     f'</span></summary><ul>')
            h += [f'<li data-text="{escape(a.subject.lower())}">📣 <a href="{escape(a.url)}">{escape(a.subject)}</a>'
                  f' <span class="muted">{a.posted:%b %d}</span></li>' for a in course.announcements]
            h.append("</ul></details>")
        h += _render_tree(cfg, path.parent, course, _section_tree(course))
        h.append("</details>")
    h.append(f"<script>{SCRIPT}</script></main></body></html>")
    path.write_text("\n".join(h), encoding="utf-8")
    log.info("Saved %s", path)
    return path
