"""Minimal client for the Moodle web-service REST API that eClass exposes to the mobile app."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

# Errors that mean the token is dead and the user must run get_eclass_token.py again.
AUTH_ERRORS = {"invalidtoken", "accessexception", "invalidlogin", "usernotfullysetup"}


class MoodleError(Exception):
    def __init__(self, errorcode: str, message: str):
        super().__init__(f"{errorcode}: {message}")
        self.errorcode = errorcode
        self.message = message

    @property
    def is_auth_error(self) -> bool:
        return self.errorcode in AUTH_ERRORS


def _flatten(obj, prefix: str = "") -> dict:
    """Turn nested args into Moodle's form encoding: {"a": [{"b": 1}]} -> {"a[0][b]": 1}."""
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}[{k}]" if prefix else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    elif isinstance(obj, bool):
        out[prefix] = int(obj)
    elif obj is not None:
        out[prefix] = obj
    return out


class Moodle:
    def __init__(self, base_url: str, token: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "eclass-digest/1.0"
        self.user_id: int | None = None  # filled in by site_info()

    def call(self, function: str, **params):
        data = {"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json"}
        data.update(_flatten(params))
        r = self.session.post(
            f"{self.base}/webservice/rest/server.php", data=data, timeout=self.timeout
        )
        r.raise_for_status()
        result = r.json()
        if isinstance(result, dict) and "exception" in result:
            raise MoodleError(result.get("errorcode", "unknown"), result.get("message", ""))
        return result

    # --- API wrappers -------------------------------------------------------

    def site_info(self) -> dict:
        info = self.call("core_webservice_get_site_info")
        self.user_id = info.get("userid")
        return info

    def courses(self, classification: str = "inprogress") -> list[dict]:
        result = self.call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification=classification,
            limit=0,
            offset=0,
        )
        return result.get("courses", [])

    def course_contents(self, course_id: int) -> list[dict]:
        return self.call("core_course_get_contents", courseid=course_id)

    def forums(self, course_ids: list[int]) -> list[dict]:
        return self.call("mod_forum_get_forums_by_courses", courseids=course_ids)

    def discussions(self, forum_id: int, perpage: int = 10) -> list[dict]:
        result = self.call(
            "mod_forum_get_forum_discussions", forumid=forum_id, page=0, perpage=perpage
        )
        return result.get("discussions", [])

    def calendar_events(self, course_ids: list[int], start: int, end: int) -> list[dict]:
        result = self.call(
            "core_calendar_get_calendar_events",
            events={"courseids": course_ids},
            options={
                "userevents": True,
                "siteevents": True,
                "timestart": start,
                "timeend": end,
                "ignorehidden": True,
            },
        )
        return result.get("events", [])

    def grade_items(self, course_id: int, user_id: int) -> list[dict]:
        result = self.call("gradereport_user_get_grade_items", courseid=course_id, userid=user_id)
        users = result.get("usergrades") or [{}]
        return users[0].get("gradeitems", [])

    def assignment_submission(self, assign_id: int) -> str:
        """ "submitted" | "draft" | "not submitted", or "" if it has no online submission."""
        last = self.call("mod_assign_get_submission_status", assignid=assign_id).get("lastattempt") or {}
        if last.get("submissionsenabled") is False:
            return ""
        status = (last.get("submission") or last.get("teamsubmission") or {}).get("status")
        return {"submitted": "submitted", "draft": "draft"}.get(status, "not submitted")

    def quiz_submission(self, quiz_id: int) -> str:
        result = self.call("mod_quiz_get_user_attempts", quizid=quiz_id, status="finished")
        return "submitted" if result.get("attempts") else "not submitted"

    # --- Files --------------------------------------------------------------

    def download(self, fileurl: str, max_bytes: int) -> bytes:
        """Download a webservice/pluginfile.php URL. Raises ValueError if it is too large."""
        with self.session.get(
            fileurl, params={"token": self.token}, stream=True, timeout=self.timeout
        ) as r:
            r.raise_for_status()
            chunks, total = [], 0
            for chunk in r.iter_content(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"file larger than {max_bytes // (1024 * 1024)} MB")
                chunks.append(chunk)
            return b"".join(chunks)

    @staticmethod
    def browser_url(fileurl: str) -> str:
        """A link that opens in a normal browser session (no token in it, safe to email)."""
        parts = urlsplit(fileurl.replace("/webservice/pluginfile.php", "/pluginfile.php"))
        query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("token", "forcedownload")]
        return urlunsplit(parts._replace(query=urlencode(query)))
