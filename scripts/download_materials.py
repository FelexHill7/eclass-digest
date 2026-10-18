"""Download every course file (except recordings) into nested folders, one per course.

    python scripts/download_materials.py           # everything you don't have yet
    python scripts/download_materials.py --list    # show what's there / missing, download nothing

Layout:  <DOWNLOAD_DIR>/<CSSD 3131 - Machine Learning>/<unit>/<week>/<file>.pdf
Nothing is downloaded twice: files you already have (and copies a prof posted in two places)
are skipped. After this, the 2-hourly checks and the evening digest keep it up to date.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eclass_digest.collect import collect  # noqa: E402
from eclass_digest.config import load_config  # noqa: E402
from eclass_digest.files import DownloadManifest, download_path, is_recording, process_files  # noqa: E402
from eclass_digest.moodle import Moodle  # noqa: E402
from eclass_digest.state import empty_seen  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show the files, download nothing")
    args = ap.parse_args()

    cfg = dataclasses.replace(load_config(), download_files=True)
    moodle = Moodle(cfg.eclass_url, cfg.eclass_token)
    moodle.site_info()
    digest, _ = collect(moodle, empty_seen(), cfg)  # a fresh look; doesn't touch the digest's state
    manifest = DownloadManifest(cfg)

    have = missing = skipped = 0
    for course in digest.courses:
        print(f"\n{course.shortname} - {course.title}")
        for module in course.modules:
            for f in module.files:
                if module.modname == "page":
                    continue
                if is_recording(f):
                    skipped += 1
                    continue
                path = manifest.local_path(module, f) or download_path(cfg, course, module, f)
                exists = manifest.is_current(module, f) or path.exists()
                have, missing = have + exists, missing + (not exists)
                print(f"  {'have   ' if exists else 'missing'}  {path.relative_to(cfg.download_dir)}")
    print(f"\n{have} on disk, {missing} to download, {skipped} recordings skipped. Folder: {cfg.download_dir}")
    if args.list:
        return 0
    process_files(moodle, digest, cfg, want_text=False)  # also records files already on disk
    print(f"Done. Your materials are in {cfg.download_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
