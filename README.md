# eClass Daily Digest

A free tool for York University students. It watches your **eClass** courses through the day and:

- **at 7:30 AM, emails what's due today**, with whether you've submitted it. It's skipped if nothing's due.
- **every 2 hours (9 AM–9 PM), checks for changes** and only emails when something happened:
  - a new **grade or feedback**
  - a new **announcement** (full text)
  - new **slides or files**
  - a **deadline added, moved or removed**
  - a deadline **under 24 hours away that you haven't submitted**, flagged in red
- **at 8 PM, after profs have posted for the day, sends the full digest:**
  - what's due tonight and tomorrow
  - everything new that day
  - grades
  - an AI-written summary and to-do list (optional)
  - a linked index of every course
- **keeps every deadline, quiz and exam in Google Calendar**, updating changed due dates and removing deleted ones.

<p align="center">
  <img src="docs/sample-digest.png" width="560" alt="Example digest email">
  <br><img src="docs/sample-calendar-email.png" width="560" alt="Example calendar update email">
  <br><sub>Example emails (made-up course data)</sub>
</p>

It uses eClass's official mobile-app API (the same one the Moodle app uses), so it doesn't break when the
website changes. You log in with Passport York + Duo **once**, during setup.

**Cost: free.** Gmail, Google Calendar, GitHub and the optional AI summary (Google Gemini's free tier) are all
free for this.

## Setup (about 15 minutes)

You need [Python 3.10+](https://www.python.org/downloads/) (on Windows, tick *"Add python.exe to PATH"* while installing),
a personal Gmail account, and your eClass login.

**1. Download the project.** Click **Code → Download ZIP** and unzip it, or run:
```
git clone https://github.com/FelexHill7/eclass-digest.git
```

**2. Install.** Open a terminal in the project folder.

Windows:
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-setup.txt
.venv\Scripts\python scripts\setup_wizard.py
```
macOS / Linux:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-setup.txt
.venv/bin/python scripts/setup_wizard.py
```

**3. Follow the wizard.** It walks you through each step, tests it as you go, and you can re-run it anytime:

| Step | What you do |
|---|---|
| eClass | A browser opens; log in as usual. Your eClass token is captured and saved. |
| Recipient | The address the digest goes to (usually your `@my.yorku.ca`). |
| Gmail | Make a Gmail [app password](https://myaccount.google.com/apppasswords) so the digest can be sent. |
| Calendar | Make a free Google Cloud "service account" (a robot account) and share one calendar with it. |
| AI summary | Optional. Paste a free [Gemini key](https://aistudio.google.com/apikey), or press Enter to skip. |
| Schedule | Sends your first digest and sets it to run every morning. |

That's it. On Windows it runs from Task Scheduler: the morning email, the 2-hourly checks and the evening digest.
If your PC is asleep at a scheduled time, that run happens when the PC wakes. Change the times with
`scripts\install_task.ps1 -Morning 07:00 -Evening 21:00 -CheckEvery 3`, or stop everything with `-Remove`.

<details>
<summary><b>Run it in the cloud with GitHub Actions</b> (works even when your computer is off)</summary>

1. On this repo's GitHub page, click **Use this template → Create a new repository** and make it **Private**.
   It has to be private, because workflow logs on a public repo are public.
2. Clone your private copy, then run the setup wizard in it (steps 2–3 above) so `.env` and `service_account.json` work locally.
3. Install the [GitHub CLI](https://cli.github.com), run `gh auth login`, then:
   ```
   python scripts/push_github_secrets.py
   ```
   This copies your settings into the repo's encrypted secrets and switches the workflow on. It then runs
   on the same schedule (Toronto time) and uses roughly 300 of the 2,000 free Actions minutes a month.
   You can also start a run from the **Actions** tab.

The workflow does nothing until `DIGEST_ENABLED` is set, so copies of this repo never run by accident.
</details>

## Settings

Everything is in `.env`, which the wizard writes for you. See [.env.example](.env.example) for the full list.

| Setting | Default | What it does |
|---|---|---|
| `DIGEST_TO` | | Where the digest goes |
| `INCLUDE_COURSES` | | Also track these courses even if eClass doesn't mark them "in progress" (e.g. `CSSD 3101, 165459`) |
| `EXCLUDE_COURSES` | | Skip courses whose code or name contains any of these (comma-separated) |
| `DEADLINE_WINDOW_DAYS` | `14` | How far ahead "Coming up" looks |
| `DUE_SOON_HOURS` | `24` | Flag unsubmitted deadlines in red, and alert once, when they're this close |
| `NOTIFY_CALENDAR_CHANGES` | `true` | Include deadlines added, moved or removed in the emails |
| `SEND_IF_NOTHING_NEW` | `true` | Still send the evening digest on quiet days |
| `MORNING_SEND_IF_EMPTY` | `false` | Send the morning email even when nothing's due |
| `INCLUDE_FULL_INDEX` | `true` | One line per course in each digest, plus the `Course resources.html` dropdown page |
| `DOWNLOAD_FILES` | `false` | Keep a copy of every course file (not recordings) in nested course/unit/week folders |
| `DOWNLOAD_DIR` | `downloads` | Where those copies go |
| `ECLASS_URL` | `https://eclass.yorku.ca` | Works with other Moodle sites too |

Command line: `python -m eclass_digest [--mode digest|check|morning] [--dry-run] [--no-email] [--no-calendar] [--no-ai] [--reset]`.
`--dry-run` saves a preview to `output/` without sending anything.

**Course materials on your computer:** set `DOWNLOAD_FILES=true` (and a `DOWNLOAD_DIR`), then run
`python scripts/download_materials.py` once. It files everything except recordings as
`<DOWNLOAD_DIR>/CSSD 3131 - Machine Learning/<unit>/<week>/…`, and the 2-hourly checks add new files after that.
The same folder gets `Course resources.html`, with a dropdown for each course, unit and week, a search box, and
links to both eClass and your copies. Emails can't do dropdowns in Gmail, so the digest only lists one line per course.

**Which courses are tracked?** `python scripts/list_courses.py` lists the courses in your digest, any eClass
won't let you open, and this year's courses that aren't tracked (with how to add them).

**Lecture notes for a whole course** (one-off): reads every lecture PDF, including diagrams and equations, and emails study notes:
```
python scripts/summarize_course.py "CSSD 3131" --match lecture
```

## Troubleshooting

- **No email?** Check spam. Also check that `DIGEST_TO` is your exact address; York addresses often include a number (e.g. `jsmith7@`).
- **"App passwords: the setting is not available"** → turn on 2-Step Verification first. If you're signed in to your school
  account, switch to a personal Gmail; York blocks app passwords on school accounts.
- **The robot can't write to the calendar (403)** → in the calendar's sharing settings, change the robot's permission to
  **Make changes to events**. If that option is greyed out, the calendar belongs to a school account; create it in a personal Gmail.
- **Calendar shows nothing**:
  - Most deadlines are at **11:59 PM**, so scroll down, or use Month or Schedule view.
  - If the calendar lives in another account, accept the share invitation and make sure it's ticked under *Other calendars*.
  - If you have two calendars with the same name, check you're looking at the shared one.
  - "Create" in Google Calendar makes an *appointment schedule*, not a calendar. Use *Other calendars → + → Create new calendar*.
- **Some courses are skipped** → eClass lists some sections (often labs) that students can't open. They're skipped automatically.
- **"Please refresh your eClass token" email** → run `scripts/get_eclass_token.py` (or the wizard) again, and re-run
  `push_github_secrets.py` if you use GitHub Actions.
- Logs are in `logs/eclass_digest.log`.

## Privacy & security

- Everything runs on **your** computer or **your** private GitHub repo. Nothing is sent anywhere else, except the
  optional AI summary, which sends course material to the AI you chose. On Gemini's **free tier, Google may use
  that content to improve its products**. Leave the key out, or use a paid Gemini key, if that matters to you.
- Email links are normal eClass links with no token in them, so forwarding a digest doesn't give anyone access.
- `.env`, `service_account.json` and `token.json` hold your secrets. They're git-ignored; never commit or share them.
  Anyone with your eClass token can read your eClass.

## For developers

```
pip install -r requirements-setup.txt
python -m pytest tests            # offline; uses a fake eClass
```

| Module | Job |
|---|---|
| [moodle.py](eclass_digest/moodle.py) | Moodle web-service client |
| [collect.py](eclass_digest/collect.py) | Courses, sections, files, announcements, events; diffing against last run |
| [files.py](eclass_digest/files.py) | Downloads and text extraction (PDF, PPTX, DOCX) |
| [ai.py](eclass_digest/ai.py) | Gemini API calls, with fallback models and rate-limit waits |
| [summarize.py](eclass_digest/summarize.py) | Daily summary prompt |
| [gcal.py](eclass_digest/gcal.py) | Google Calendar sync |
| [emailer.py](eclass_digest/emailer.py) | Email rendering and sending |

Pull requests welcome.

*Not affiliated with or endorsed by York University. You're responsible for using it in line with York's
policies; it only reads what your own account can already see.*

## License

[MIT](LICENSE)
