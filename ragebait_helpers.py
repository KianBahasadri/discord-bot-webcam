"""Helpers for generating topics (Azure OpenAI), starting ElevenLabs calls,
polling conversations and formatting transcripts.

Only Python stdlib is used for HTTP and JSON handling.

Functions implemented:
- generate_topic_with_azure(prompt_text: str, fallback: str) -> str
- start_elevenlabs_call(dynamic_topic: str) -> dict
- poll_conversation_until_terminal(conversation_id: str) -> dict
- format_transcript(conversation_payload: dict) -> str

Environment variables used:
- AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT,
  AZURE_OPENAI_API_VERSION
- ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID,
  ELEVENLABS_AGENT_PHONE_NUMBER_ID, MO_CELL_NUMBER
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional
import logging


POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 10 * 60  # 10 minutes
DISCORD_SAFE_LIMIT = 1800

# common terminal statuses for ElevenLabs conversations
TERMINAL_STATUSES = {
    "done",
    "failed",
    "completed",
    "succeeded",
    "finished",
    "complete",
    "failure",
    "error",
    "errored",
    "terminated",
    "cancelled",
    "canceled",
}


def _http_request(url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None, data: Optional[bytes] = None, timeout: int = 120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read()
            text = body.decode("utf-8", errors="replace")
            return status, text
    except urllib.error.HTTPError as e:
        # include a short snippet of response for debugging
        try:
            err_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_text = str(e)
        raise RuntimeError(f"HTTP {e.code} error for {url}: {err_text[:300]}")
    except Exception as e:
        raise RuntimeError(f"Request error for {url}: {e}")


logger = logging.getLogger(__name__)


def parse_http_error_info(exc: Exception) -> tuple[Optional[int], Optional[str]]:
    """Extract HTTP status code and short body/text from common urllib/RuntimeError forms.

    Returns (code, body_text) where either may be None if not available.
    This helper is intentionally small and tolerant: it understands
    urllib.error.HTTPError and RuntimeError messages produced by the
    helpers in this module.
    """
    # urllib HTTPError: can read body directly
    try:
        if isinstance(exc, urllib.error.HTTPError):
            code = getattr(exc, "code", None)
            body = None
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                try:
                    body = str(exc)
                except Exception:
                    body = None
            return (int(code) if code is not None else None, body)
    except Exception:
        # fall through to other parsing
        pass

    # RuntimeError messages created by helpers often embed status and a
    # short response snippet. Try to parse common patterns.
    msg = None
    try:
        msg = str(exc)
    except Exception:
        msg = None

    if not msg:
        return (None, None)

    # Common patterns:
    #  - "HTTP {code} error for {url}: {err_text}"
    #  - "... failed {code}: {text}"
    import re

    m = re.search(r"HTTP\s+(\d{3})", msg)
    if not m:
        m = re.search(r"failed\s+(\d{3})", msg, flags=re.I)
    code = int(m.group(1)) if m else None

    # Try to extract a short textual snippet after the status marker.
    body = None
    m_body = re.search(r"failed\s+\d{3}:\s*(.*)$", msg, flags=re.I)
    if m_body:
        body = m_body.group(1).strip()
    elif " error for " in msg:
        try:
            tail = msg.split(" error for ", 1)[1]
            if ": " in tail:
                body = tail.split(": ", 1)[1].strip()
            else:
                body = tail.strip()
        except Exception:
            body = msg
    else:
        body = msg

    # Clamp body length
    if isinstance(body, str) and len(body) > 1000:
        body = body[:1000]

    return (code, body)


def _find_json_substring(s: str) -> Optional[str]:
    # attempt to locate a balanced JSON object in text
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _sanitize_topic(candidate: str, fallback: str) -> Optional[str]:
    if not isinstance(candidate, str):
        return None
    # collapse whitespace and trim quotes/newlines
    t = re.sub(r"\s+", " ", candidate).strip().strip('"\'')
    # basic guard: must contain at least one alphanumeric char
    if not re.search(r"[A-Za-z0-9]", t):
        return None
    # enforce word and length limits
    words = t.split()
    if len(words) > 6:
        return None
    if len(t) > 60:
        return None
    return t


def _extract_text_from_content(content) -> Optional[str]:
    """Robustly extract human-readable text from common content shapes.

    Accepts str, dict, list and nested combinations. Returns a single
    collapsed string or None if nothing useful is found.
    """
    if content is None:
        return None
    # Already a string
    if isinstance(content, str):
        return content.strip()

    # If it's a dict, look for common text-like keys first
    if isinstance(content, dict):
        # Common candidate keys in order of likelihood
        keys_to_try = ("content", "text", "message", "response", "output", "result", "raw", "value", "parts")
        for k in keys_to_try:
            if k in content:
                v = content.get(k)
                txt = _extract_text_from_content(v)
                if txt:
                    return txt

        # Sometimes content is nested under choices/message.delta etc.
        for v in content.values():
            txt = _extract_text_from_content(v)
            if txt:
                return txt

        # Last resort for dicts: compact JSON representation
        try:
            return json.dumps(content)
        except Exception:
            return str(content)

    # If it's a list, join all extracted pieces
    if isinstance(content, list):
        parts = []
        for item in content:
            txt = _extract_text_from_content(item)
            if txt:
                parts.append(txt)
        if parts:
            return " ".join(p for p in parts if p)
        return None

    # Fallback: coerce to string
    try:
        return str(content)
    except Exception:
        return None


def generate_topic_with_azure(prompt_text: str, fallback: str) -> str:
    """Call Azure OpenAI chat completions to generate a short topic.

    The model is asked to return a JSON object like: {"topic": "..."} to
    make extraction straightforward. If the response is malformed or the
    candidate violates length/word constraints the provided fallback is
    returned.
    """
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION")

    if not all([endpoint, api_key, deployment, api_version]):
        raise RuntimeError("Missing one of AZURE_OPENAI_{ENDPOINT,API_KEY,DEPLOYMENT,API_VERSION} env vars")

    # Narrow types for static checkers
    assert endpoint is not None
    assert deployment is not None
    assert api_version is not None
    endpoint = endpoint.rstrip("/")

    url = (
        f"{endpoint}/openai/deployments/{urllib.parse.quote(deployment, safe='')}/chat/completions"
        f"?api-version={urllib.parse.quote(api_version, safe='')}"
    )

    system_instructions = (
        "You are an assistant that MUST return a single JSON object with the key 'topic'. "
        "The value must be a short phrase (no more than 6 words and 60 characters) that summarizes the user's input. "
        "Return only valid JSON, nothing else."
    )

    messages = [
        {"role": "system", "content": system_instructions},
        {"role": "user", "content": prompt_text},
    ]

    payload = {"messages": messages, "max_completion_tokens": 60}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "api-key": api_key}

    status, resp_text = _http_request(url, method="POST", headers=headers, data=data)
    if not (200 <= status < 300):
        raise RuntimeError(f"Azure request failed {status}: {resp_text[:300]}")

    # Try to parse JSON response structure
    topic_candidate = None
    try:
        resp = json.loads(resp_text)
    except Exception:
        # try to extract an embedded JSON substring
        jsub = _find_json_substring(resp_text)
        if jsub:
            try:
                resp = json.loads(jsub)
            except Exception:
                resp = None
        else:
            resp = None

    if isinstance(resp, dict):
        # Some Azure wrappers place the actual response under a top-level
        # `body` key (often a JSON string or dict). If present prefer that
        # inner value for subsequent parsing to make extraction more robust.
        if "body" in resp:
            body_val = resp.get("body")
            if isinstance(body_val, dict):
                resp = body_val
            elif isinstance(body_val, str):
                try:
                    parsed_body = json.loads(body_val)
                    if isinstance(parsed_body, dict):
                        resp = parsed_body
                except Exception:
                    # leave resp as-is but allow downstream fallbacks
                    pass
        # get the model's text content from common response shapes
        content = None
        choices = resp.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            # typical chat response: {"message": {"content": ...}}
            if isinstance(first, dict):
                msg = first.get("message") or first.get("delta") or first
                if isinstance(msg, dict):
                    # content can be str, dict or list
                    content = msg.get("content") or msg.get("text") or msg
                elif isinstance(msg, str):
                    content = msg

        # last resort: top-level common keys
        if content is None:
            for key in ("content", "text", "response", "output", "result"):
                if key in resp:
                    content = resp.get(key)
                    break

        # Use the robust extractor to turn the content into text. If the
        # normal content extraction fails, fall back to extracting from
        # the whole response payload to catch atypical shapes.
        content_text = _extract_text_from_content(content)
        if not content_text:
            # Fall back to extracting any useful text from the full response
            content_text = _extract_text_from_content(resp)
        if content_text:
            # content_text may itself be JSON or plain text
            try:
                parsed = json.loads(content_text)
                if isinstance(parsed, dict) and "topic" in parsed:
                    topic_candidate = parsed.get("topic")
                elif isinstance(parsed, list):
                    # look for a dict with 'topic'
                    for it in parsed:
                        if isinstance(it, dict) and "topic" in it:
                            topic_candidate = it.get("topic")
                            break
            except Exception:
                # search for a "topic" field in raw text
                m = re.search(r'"topic"\s*:\s*"([^\"]+)"', content_text)
                if m:
                    topic_candidate = m.group(1)
                else:
                    # fallback to using the content itself
                    topic_candidate = content_text.strip()

    # sanitize and enforce constraints
    topic = _sanitize_topic(topic_candidate or "", fallback)
    if not topic:
        return fallback
    return topic


def start_elevenlabs_call(dynamic_topic: str, override_to_number: Optional[str] = None) -> dict:
    """Start an outbound ElevenLabs convai call (Twilio). Returns parsed JSON.

    The request will include DYNAMIC_TOPIC in the payload. Environment
    variables required:
      - ELEVENLABS_API_KEY
      - ELEVENLABS_AGENT_ID
      - ELEVENLABS_AGENT_PHONE_NUMBER_ID
      - MO_CELL_NUMBER (unless override_to_number provided)
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    agent_id = os.environ.get("ELEVENLABS_AGENT_ID")
    phone_number_id = os.environ.get("ELEVENLABS_AGENT_PHONE_NUMBER_ID")
    to_number = override_to_number if override_to_number else os.environ.get("MO_CELL_NUMBER")

    missing = []
    if not api_key:
        missing.append("ELEVENLABS_API_KEY")
    if not agent_id:
        missing.append("ELEVENLABS_AGENT_ID")
    if not phone_number_id:
        missing.append("ELEVENLABS_AGENT_PHONE_NUMBER_ID")
    if not to_number:
        missing.append("MO_CELL_NUMBER (or provide an override_to_number)")
    if missing:
        raise RuntimeError("Missing required ElevenLabs configuration: " + ", ".join(missing))

    url = "https://api.elevenlabs.io/v1/convai/twilio/outbound-call"
    # Use the documented ElevenLabs Twilio outbound payload shape. Keep the
    # dynamic topic inside conversation_initiation_client_data.dynamic_variables
    # under the key DYNAMIC_TOPIC. Do not include top-level `to`/`from` or
    # top-level dynamic_topic fields.
    body = {
        "agent_id": agent_id,
        "agent_phone_number_id": phone_number_id,
        "to_number": to_number,
        "conversation_initiation_client_data": {
            "dynamic_variables": {"DYNAMIC_TOPIC": dynamic_topic}
        },
    }

    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "xi-api-key": api_key}

    status, resp_text = _http_request(url, method="POST", headers=headers, data=data)
    if not (200 <= status < 300):
        raise RuntimeError(f"ElevenLabs start call failed {status}: {resp_text[:300]}")

    try:
        return json.loads(resp_text)
    except Exception:
        return {"raw_response": resp_text}


def fetch_conversation_audio(conversation_id: str) -> tuple[bytes, str]:
    """Download conversation audio bytes from ElevenLabs documented endpoint.

    GET https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}/audio

    Returns (audio_bytes, file_ext) where file_ext includes the leading dot
    (e.g. ".mp3", ".wav"). Raises RuntimeError on non-2xx or request errors.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ELEVENLABS_API_KEY env var")

    quoted = urllib.parse.quote(conversation_id, safe="")
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{quoted}/audio"
    req = urllib.request.Request(url, headers={"xi-api-key": api_key, "Accept": "*/*"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status = resp.getcode()
            body = resp.read()
            if not (200 <= status < 300):
                # Try to include a short textual snippet for debugging
                try:
                    txt = body.decode("utf-8", errors="replace")
                except Exception:
                    txt = str(body)[:300]
                raise RuntimeError(f"ElevenLabs audio download failed {status}: {txt[:300]}")

            # Infer file extension from Content-Type when possible
            ctype = ""
            try:
                ctype = resp.headers.get("Content-Type") or resp.headers.get("content-type") or ""
            except Exception:
                ctype = ""
            ctype = (ctype or "").lower()
            ext = ".mp3"
            if "mpeg" in ctype or "mp3" in ctype:
                ext = ".mp3"
            elif "wav" in ctype:
                ext = ".wav"
            elif "ogg" in ctype:
                ext = ".ogg"
            elif "m4a" in ctype:
                ext = ".m4a"
            elif "flac" in ctype:
                ext = ".flac"

            return body, ext
    except urllib.error.HTTPError as e:
        try:
            err_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_text = str(e)
        raise RuntimeError(f"HTTP {e.code} error for {url}: {err_text[:300]}")
    except Exception as e:
        raise RuntimeError(f"Request error for {url}: {e}")


def fetch_conversation_audio_with_retry(
    conversation_id: str,
    retry_interval_seconds: float = 3.0,
    max_total_wait_seconds: float = 30.0,
) -> tuple[bytes, str]:
    """Attempt to fetch conversation audio with a bounded retry policy.

    Default policy: immediate attempt, then retry every retry_interval_seconds
    up to max_total_wait_seconds of total waiting time. Retries are only
    attempted for the following cases:
      - HTTP 404 where the response body indicates the audio is not yet
        available (e.g. contains 'missing_conversation_audio' or similar)
      - HTTP 429 (rate limit)
      - 5xx server errors
      - transient network/request errors (no HTTP code)

    Non-retryable status codes: 400, 401, 403, 422

    On success returns (audio_bytes, extension). On exhausted retries
    raises RuntimeError with a concise message that includes the final
    HTTP code when available.
    """
    start = time.time()
    deadline = start + float(max_total_wait_seconds)
    while True:
        try:
            return fetch_conversation_audio(conversation_id)
        except Exception as e:
            code, body = parse_http_error_info(e)

            # classify retryable vs non-retryable
            retryable = False
            try:
                if code is None:
                    # network or other non-HTTP error; treat as transient
                    retryable = True
                else:
                    if code == 404:
                        # only retry for 404s that indicate audio not yet ready
                        b = (body or "").lower()
                        if (
                            "missing_conversation_audio" in b
                            or "missing conversation audio" in b
                            or "audio not ready" in b
                            or "audio not found" in b
                            or "missing audio" in b
                        ):
                            retryable = True
                    if code == 429:
                        retryable = True
                    if 500 <= int(code) < 600:
                        retryable = True

                    # explicit non-retryable cases
                    if int(code) in (400, 401, 403, 422):
                        retryable = False
            except Exception:
                # conservative default: do not retry
                retryable = False

            now = time.time()
            if not retryable:
                # non-retryable: re-raise the original exception
                raise

            # Check if we have time left to retry
            if now >= deadline:
                final_code = code or "none"
                short_body = (body or "")[:300]
                # include HTTP <code> pattern so callers can parse the code
                msg = f"Exhausted retries fetching conversation audio {conversation_id}; final HTTP {final_code}"
                if short_body:
                    msg += f": {short_body}"
                raise RuntimeError(msg)

            # sleep a bounded interval then retry
            sleep_for = min(float(retry_interval_seconds), max(0.0, deadline - now))
            time.sleep(sleep_for)
            continue


def poll_conversation_until_terminal(conversation_id: str) -> dict:
    """Poll an ElevenLabs conversation until status is terminal (done/failed).

    Returns the final payload. If the poll times out, returns a payload with
    status 'timeout'.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ELEVENLABS_API_KEY env var")

    quoted = urllib.parse.quote(conversation_id, safe="")
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{quoted}"
    headers = {"xi-api-key": api_key, "Accept": "application/json"}

    start = time.time()
    last_payload = None
    while True:
        status, resp_text = _http_request(url, method="GET", headers=headers)
        if not (200 <= status < 300):
            raise RuntimeError(f"ElevenLabs poll failed {status}: {resp_text[:300]}")
        try:
            payload = json.loads(resp_text)
        except Exception:
            payload = {"raw_response": resp_text}

        last_payload = payload
        st = ""
        if isinstance(payload, dict):
            st = str(payload.get("status", "")).lower()

        if st in TERMINAL_STATUSES:
            return payload

        if time.time() - start > POLL_TIMEOUT_SECONDS:
            return {"status": "timeout", "conversation_id": conversation_id, "last_payload": last_payload}

        time.sleep(POLL_INTERVAL_SECONDS)


def format_transcript(conversation_payload: dict) -> str:
    """Format a conversation payload into lines like "[role] message".

    The result is truncated to about DISCORD_SAFE_LIMIT characters.
    """
    messages = None
    if isinstance(conversation_payload, dict):
        # common keys to inspect
        for key in ("messages", "transcript", "events", "turns", "conversation"):
            if key in conversation_payload and isinstance(conversation_payload[key], list):
                messages = conversation_payload[key]
                break

    if messages is None and isinstance(conversation_payload, dict):
        # fallback: pick the first list-of-dicts we find
        for v in conversation_payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                messages = v
                break

    lines = []
    if messages:
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or item.get("speaker") or item.get("from") or item.get("actor") or "unknown"
            if isinstance(role, dict):
                role = role.get("name") or role.get("id") or str(role)
            if isinstance(role, str) and role.strip().lower() == "user":
                role = "MO"
            # support different content field names
            text = item.get("content") or item.get("text") or item.get("message") or ""
            if isinstance(text, dict):
                text = text.get("text") or json.dumps(text)
            text = (text or "").strip()
            if text:
                lines.append(f"[{role}] {text}")
    else:
        # last resort: dump a compact JSON
        try:
            c = json.dumps(conversation_payload)
        except Exception:
            c = str(conversation_payload)
        lines = [c]

    out = ""
    for ln in lines:
        # preserve whole lines while staying under the Discord-safe limit
        add = ("\n" if out else "") + ln
        if len(out) + len(add) > DISCORD_SAFE_LIMIT:
            remaining = DISCORD_SAFE_LIMIT - len(out)
            if remaining > 3:
                out += add[:remaining - 3] + "..."
            break
        out += add

    return out
