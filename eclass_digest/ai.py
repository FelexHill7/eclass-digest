"""AI summaries through Google's Gemini API, which has a free tier.

Parts are plain dicts: {"type": "text", "text": ...} or {"type": "pdf", "data": bytes, "name": ...}.
PDFs go to the model as documents, so it sees diagrams and equations, not just extracted text.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass

import requests

from .config import Config

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DECLINED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


class AIError(RuntimeError):
    pass


class AIDeclined(AIError):
    pass


@dataclass
class Result:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


def generate(cfg: Config, parts: list[dict], system: str = "") -> Result:
    """Try GEMINI_MODEL, then each fallback model when one is overloaded or out of free quota."""
    body = {"contents": [{"role": "user", "parts": [_part(p) for p in parts]}],
            "generationConfig": {"maxOutputTokens": 16000}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    models = [cfg.gemini_model] + [m for m in cfg.gemini_fallback_models if m != cfg.gemini_model]
    last = ""
    for model in models:
        for attempt in range(4):
            r = requests.post(API.format(model=model), json=body, timeout=300,
                              headers={"x-goog-api-key": cfg.gemini_api_key})
            if r.status_code == 200:
                return _result(r.json(), model)
            last = f"{model}: {r.status_code} {_message(r)}"
            if r.status_code == 429 and attempt < 2:  # free-tier rate limit; wait it out
                wait = _retry_delay(r, attempt)
                log.info("Gemini rate limit on %s; waiting %ss", model, wait)
                time.sleep(wait)
                continue
            if r.status_code in (500, 503, 504) and attempt == 0:  # overloaded; one quick retry
                time.sleep(5)
                continue
            if r.status_code in (429, 500, 503, 504):
                break  # move on to the next model
            raise AIError(last)  # bad key, bad request, etc.
        log.info("Gemini %s unavailable (%s); trying the next model", model, last)
    raise AIError(f"No Gemini model available right now ({last})")


def _part(p: dict) -> dict:
    if p["type"] == "pdf":
        return {"inline_data": {"mime_type": "application/pdf",
                                "data": base64.b64encode(p["data"]).decode()}}
    return {"text": p["text"]}


def _result(data: dict, model: str) -> Result:
    blocked = data.get("promptFeedback", {}).get("blockReason")
    candidates = data.get("candidates") or []
    if blocked or not candidates:
        raise AIDeclined(f"Gemini declined this request ({blocked or 'no answer'})")
    cand = candidates[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])
                   if not p.get("thought")).strip()
    reason = cand.get("finishReason", "")
    if not text and reason in DECLINED:
        raise AIDeclined(f"Gemini declined this request ({reason})")
    if reason == "MAX_TOKENS":
        text += "\n\n*(cut off)*"
    usage = data.get("usageMetadata", {})
    return Result(text, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0), model)


def _message(r: requests.Response) -> str:
    try:
        return r.json().get("error", {}).get("message", "")[:200]
    except ValueError:
        return r.text[:200]


def _retry_delay(r: requests.Response, attempt: int) -> int:
    """Google says how long to wait in the error details ("retryDelay": "17s")."""
    m = re.search(r'"retryDelay":\s*"(\d+)', r.text)
    return min(90, int(m.group(1)) + 1) if m else 20 * (attempt + 1)
