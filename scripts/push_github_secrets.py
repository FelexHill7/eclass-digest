"""Copy your working local setup into GitHub Actions secrets, so the digest runs in the cloud.

Needs the GitHub CLI (https://cli.github.com, then `gh auth login`). Run it from your own
PRIVATE copy of the repo:

    python scripts/push_github_secrets.py              # uses this folder's GitHub repo
    python scripts/push_github_secrets.py you/my-digest
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eclass_digest.envfile import read_env  # noqa: E402

SECRETS = ["ECLASS_TOKEN", "DIGEST_TO", "SMTP_USER", "SMTP_PASSWORD", "GCAL_CALENDAR_ID",
           "GEMINI_API_KEY"]


def gh(*args: str, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=input, text=True, capture_output=True, cwd=ROOT)


def main() -> int:
    try:
        repo = sys.argv[1] if len(sys.argv) > 1 else \
            gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").stdout.strip()
    except FileNotFoundError:
        print("Install the GitHub CLI first: https://cli.github.com  (then run: gh auth login)")
        return 1
    if not repo:
        print("Couldn't tell which GitHub repo to use. Pass it: push_github_secrets.py you/repo")
        return 1

    info = json.loads(gh("repo", "view", repo, "--json", "visibility").stdout or "{}")
    if info.get("visibility") != "PRIVATE":
        print(f"{repo} is {info.get('visibility', 'not found').lower()}. Workflow logs on a public "
              "repo are public, so only run your digest from a PRIVATE copy.")
        return 1

    env = read_env()
    values = {k: env.get(k, "") for k in SECRETS}
    sa = ROOT / "service_account.json"
    if sa.exists():
        values["GOOGLE_SERVICE_ACCOUNT_JSON"] = sa.read_text(encoding="utf-8")
    missing = [k for k in ("ECLASS_TOKEN", "DIGEST_TO", "SMTP_USER", "SMTP_PASSWORD")
               if not values[k]]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}. Run scripts/setup_wizard.py first.")
        return 1

    for name, value in values.items():
        if not value:
            continue
        r = gh("secret", "set", name, "-R", repo, input=value)  # value goes via stdin, not argv
        print(f"  {'set' if r.returncode == 0 else 'FAILED'}: {name}"
              + ("" if r.returncode == 0 else f" ({r.stderr.strip()})"))
    for name in ("INCLUDE_COURSES", "EXCLUDE_COURSES"):  # not secret; plain repo variables
        if env.get(name):
            gh("variable", "set", name, "-R", repo, "--body", env[name])
            print(f"  set variable: {name}")
    gh("variable", "set", "DIGEST_ENABLED", "-R", repo, "--body", "true")
    print(f"\nDone. {repo} now runs the morning email, 2-hourly checks and the evening digest.\n"
          f"Start one by hand with:  gh workflow run daily-digest.yml -R {repo} -f mode=check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
