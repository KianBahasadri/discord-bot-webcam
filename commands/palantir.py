#!/usr/bin/env python3
"""
Command module for the /palantir slash command.

Exposes register(tree, client) which registers the command on the provided
app_commands CommandTree and wires the client instance the command uses.

This module contains only command-specific helpers (kept private) and does
not modify bot.py. Behavior is intended to match the original implementation
in bot.py.
"""

import os
import sys
import tempfile
import logging
import re
import time
import subprocess
import shlex
import shutil
from typing import Any, Callable, Optional, cast
import json
from dataclasses import dataclass
from typing import List, Dict, Tuple
import requests
from datetime import timezone, timedelta

import discord
import asyncio
from media_redaction import redact_sensitive_media_inplace
from . import heart_rate

try:
    import cv2
except Exception:
    # Match bot.py behavior: surface a clear error if OpenCV is not available.
    print("Error: failed to import OpenCV (opencv-python-headless).", file=sys.stderr)
    raise

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


# Linear GraphQL helper constants and defaults
LINEAR_GRAPHQL_ENDPOINT = os.environ.get("LINEAR_GRAPHQL_ENDPOINT", "https://api.linear.app/graphql")
LINEAR_SUMMARY_ENABLED_DEFAULT = True
LINEAR_DEFAULT_STATUSES = ["In Progress", "Todo"]
LINEAR_DEFAULT_LOOKAHEAD_DAYS = 3


class LinearError(Exception):
    """Base exception for Linear-related failures."""


class LinearFetchError(LinearError):
    """Raised when fetching data from Linear fails in a non-ambiguous way."""


def _get_linear_env_defaults() -> Tuple[List[str], int]:
    """Read env-configurable defaults for linear filtering.

    - LINEAR_STATUSES: comma-separated status names (default: "In Progress,Todo")
    - LINEAR_LOOKAHEAD_DAYS: integer number of days to look ahead (default: 3)
    """
    default_statuses_csv = ",".join(LINEAR_DEFAULT_STATUSES)
    raw_statuses = os.environ.get("LINEAR_STATUSES", default_statuses_csv)
    # split on comma and strip whitespace, ignore empty entries
    statuses = [s.strip() for s in raw_statuses.split(",") if s.strip()]
    try:
        lookahead = int(os.environ.get("LINEAR_LOOKAHEAD_DAYS", str(LINEAR_DEFAULT_LOOKAHEAD_DAYS)))
    except Exception:
        lookahead = LINEAR_DEFAULT_LOOKAHEAD_DAYS
    return statuses, max(0, lookahead)


@dataclass
class LinearIssue:
    identifier: Optional[str]
    title: Optional[str]
    priority: Optional[int]
    due_date: Optional[datetime]
    state_name: Optional[str]
    state_type: Optional[str]
    assignee_name: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    updated_at: Optional[datetime]
    project_name: Optional[str]
    url: Optional[str]


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Normalize trailing Z to +00:00 for fromisoformat
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        # Try date-only format
        try:
            parsed = datetime.fromisoformat(value + "T00:00:00+00:00")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            logger.debug("Failed to parse ISO datetime: %s", value)
            return None


def _issue_node_key(node: Dict) -> str:
    identifier = str(node.get("identifier") or "").strip()
    if identifier:
        return f"id:{identifier}"
    url = str(node.get("url") or "").strip()
    if url:
        return f"url:{url}"
    title = str(node.get("title") or "").strip()
    updated_at = str(node.get("updatedAt") or "").strip()
    return f"fallback:{title}:{updated_at}"


def _is_closed_issue_node(node: Dict) -> bool:
    state = node.get("state") or {}
    state_type = str(state.get("type") or "").strip().lower()
    state_name = str(state.get("name") or "").strip().lower()
    closed_types = {"completed", "canceled", "cancelled"}
    closed_names = {"done", "completed", "canceled", "cancelled"}
    return (state_type in closed_types) or (state_name in closed_names)


def _filter_closed_issues(nodes: List[Dict], now: Optional[datetime] = None, recent_closed_days: int = 7) -> List[Dict]:
    """Keep open issues and only recently closed issues.

    Closed issues are kept only if their completion/cancellation timestamp is
    within the last `recent_closed_days` days.
    """
    if not nodes:
        return []

    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(0, int(recent_closed_days)))

    filtered: List[Dict] = []
    for node in nodes:
        if not _is_closed_issue_node(node):
            filtered.append(node)
            continue

        closed_at = _parse_iso_datetime(node.get("completedAt")) or _parse_iso_datetime(node.get("updatedAt"))
        if closed_at and closed_at >= cutoff:
            filtered.append(node)

    return filtered


def _node_to_issue(node: Dict) -> LinearIssue:
    return LinearIssue(
        identifier=node.get("identifier"),
        title=node.get("title"),
        priority=node.get("priority"),
        due_date=_parse_iso_datetime(node.get("dueDate")),
        state_name=(node.get("state") or {}).get("name"),
        state_type=(node.get("state") or {}).get("type"),
        assignee_name=(node.get("assignee") or {}).get("name"),
        started_at=_parse_iso_datetime(node.get("startedAt")),
        completed_at=_parse_iso_datetime(node.get("completedAt")),
        updated_at=_parse_iso_datetime(node.get("updatedAt")),
        project_name=(node.get("project") or {}).get("name"),
        url=node.get("url"),
    )


def _issue_to_dict(issue: LinearIssue) -> Dict:
    """Convert LinearIssue to a JSON-serializable dict used by summary helpers."""
    def iso_or_none(dt: Optional[datetime]) -> Optional[str]:
        if dt is None:
            return None
        try:
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return dt.isoformat()

    return {
        "id": issue.identifier,
        "title": issue.title,
        "priority": issue.priority,
        "dueDate": iso_or_none(issue.due_date),
        "state": issue.state_name,
        "stateType": issue.state_type,
        "assignee": issue.assignee_name,
        "startedAt": iso_or_none(issue.started_at),
        "completedAt": iso_or_none(issue.completed_at),
        "updatedAt": iso_or_none(issue.updated_at),
        "project": issue.project_name,
        "url": issue.url,
    }


def fetch_linear_issues(statuses: Optional[List[str]] = None, lookahead_days: Optional[int] = None, page_size: int = 100, max_total: int = 2000) -> List[Dict]:
    """Fetch issues from Linear GraphQL matching: (state in statuses) OR (dueDate < now + lookahead_days).

    Returns a list of issue dicts (raw GraphQL node dicts). This function handles pagination
    and includes several fail-soft fallbacks with clear errors.
    """
    resolved_statuses: List[str]
    resolved_lookahead_days: int
    if statuses is None or not statuses:
        resolved_statuses, _la = _get_linear_env_defaults()
    else:
        resolved_statuses = [s for s in statuses if isinstance(s, str) and s.strip()]
        if not resolved_statuses:
            resolved_statuses, _la = _get_linear_env_defaults()

    if lookahead_days is None:
        _st, resolved_lookahead_days = _get_linear_env_defaults()
    else:
        resolved_lookahead_days = max(0, int(lookahead_days))

    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        raise LinearFetchError("Missing LINEAR_API_KEY environment variable; cannot fetch Linear data.")

    # Linear docs expect the API key to be sent as-is in the Authorization header
    # (Authorization: <API_KEY>). If a user mistakenly provided a Bearer-prefixed
    # value, strip it so we follow the docs.
    normalized_token = token.strip()
    if normalized_token.lower().startswith("bearer "):
        normalized_token = normalized_token[7:].strip()

    headers = {
        "Authorization": normalized_token,
        "Content-Type": "application/json",
    }

    # Server-side dueDate filter: use a relative ISO duration (P{days}D) so the
    # GraphQL filter is evaluated server-side rather than embedding a concrete timestamp.
    now = datetime.now(timezone.utc)
    due_period = f"P{resolved_lookahead_days}D"

    # Build a GraphQL-friendly array for statuses
    statuses_list = ", ".join(json.dumps(s) for s in resolved_statuses)

    # Query template (we inline statuses and due threshold for simplicity)
    base_query = f"""
query Issues($first: Int, $after: String) {{
  issues(first: $first, after: $after, filter: {{ or: [ {{ state: {{ name: {{ in: [{statuses_list}] }} }} }}, {{ dueDate: {{ lt: \"{due_period}\" }} }} ] }}) {{
    nodes {{
      identifier
      title
      priority
      dueDate
      startedAt
      completedAt
      updatedAt
      url
      state {{ name type }}
      assignee {{ name }}
      project {{ name }}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""

    issues: List[Dict] = []
    after = None
    total_retrieved = 0

    session = requests.Session()

    try:
        # Primary attempt: server-side OR filter
        while True:
            payload = {"query": base_query, "variables": {"first": page_size, "after": after}}
            resp = session.post(LINEAR_GRAPHQL_ENDPOINT, headers=headers, json=payload, timeout=15)
            if resp.status_code != 200:
                # Surface helpful message
                raise LinearFetchError(f"Linear API returned HTTP {resp.status_code}: {resp.text}")

            data = resp.json()
            if data.get("errors"):
                # Bail out to fallback behavior (server-side filter might be unsupported)
                raise LinearFetchError(f"Linear GraphQL errors: {data.get('errors')}")

            issues_nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
            for n in issues_nodes:
                issues.append(n)
                total_retrieved += 1
                if total_retrieved >= max_total:
                    logger.warning("Reached max_total (%s) while fetching Linear issues; truncating results.", max_total)
                    return _filter_closed_issues(issues, now=now)

            page_info = data.get("data", {}).get("issues", {}).get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")

        return _filter_closed_issues(issues, now=now)

    except LinearFetchError as exc:
        logger.warning("Primary Linear fetch failed (%s). Attempting fallback fetch methods.", exc)
        # Fallback 1: use two server-side filters and merge so OR semantics are preserved.
        try:
            statuses_list = ", ".join(json.dumps(s) for s in resolved_statuses)
            state_query = f"""
query IssuesByState($first: Int, $after: String) {{
  issues(first: $first, after: $after, filter: {{ state: {{ name: {{ in: [{statuses_list}] }} }} }}) {{
    nodes {{ identifier title priority dueDate startedAt completedAt updatedAt url state {{ name type }} assignee {{ name }} project {{ name }} }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""

            due_query = f"""
query IssuesByDue($first: Int, $after: String) {{
  issues(first: $first, after: $after, filter: {{ dueDate: {{ lt: \"{due_period}\" }} }}) {{
    nodes {{ identifier title priority dueDate startedAt completedAt updatedAt url state {{ name type }} assignee {{ name }} project {{ name }} }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""

            merged_issues: Dict[str, Dict] = {}
            after = None
            while True:
                payload = {"query": state_query, "variables": {"first": page_size, "after": after}}
                resp = session.post(LINEAR_GRAPHQL_ENDPOINT, headers=headers, json=payload, timeout=15)
                if resp.status_code != 200:
                    raise LinearFetchError(f"Linear API returned HTTP {resp.status_code}: {resp.text}")
                data = resp.json()
                if data.get("errors"):
                    raise LinearFetchError(f"Linear GraphQL errors on fallback: {data.get('errors')}")
                nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
                for n in nodes:
                    key = _issue_node_key(n)
                    if key not in merged_issues:
                        merged_issues[key] = n
                        total_retrieved += 1
                        if total_retrieved >= max_total:
                            logger.warning("Reached max_total (%s) during fallback fetch; truncating results.", max_total)
                            return _filter_closed_issues(list(merged_issues.values()), now=now)
                page_info = data.get("data", {}).get("issues", {}).get("pageInfo", {})
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")

            after = None
            while True:
                payload = {"query": due_query, "variables": {"first": page_size, "after": after}}
                resp = session.post(LINEAR_GRAPHQL_ENDPOINT, headers=headers, json=payload, timeout=15)
                if resp.status_code != 200:
                    raise LinearFetchError(f"Linear API returned HTTP {resp.status_code}: {resp.text}")
                data = resp.json()
                if data.get("errors"):
                    raise LinearFetchError(f"Linear GraphQL errors on due-date fallback: {data.get('errors')}")
                nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
                for n in nodes:
                    key = _issue_node_key(n)
                    if key not in merged_issues:
                        merged_issues[key] = n
                        total_retrieved += 1
                        if total_retrieved >= max_total:
                            logger.warning("Reached max_total (%s) during due-date fallback; truncating results.", max_total)
                            return _filter_closed_issues(list(merged_issues.values()), now=now)
                page_info = data.get("data", {}).get("issues", {}).get("pageInfo", {})
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")

            return _filter_closed_issues(list(merged_issues.values()), now=now)
        except LinearFetchError as exc2:
            logger.warning("State-only fallback failed: %s", exc2)
            # Final fallback: fetch without server-side filter and filter client-side (bounded)
            try:
                raw_query = """
query AllIssues($first: Int, $after: String) {
  issues(first: $first, after: $after) {
    nodes { identifier title priority dueDate startedAt completedAt updatedAt url state { name type } assignee { name } project { name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""
                issues = []
                after = None
                total_retrieved = 0
                capped = False
                while True:
                    payload = {"query": raw_query, "variables": {"first": page_size, "after": after}}
                    resp = session.post(LINEAR_GRAPHQL_ENDPOINT, headers=headers, json=payload, timeout=15)
                    if resp.status_code != 200:
                        raise LinearFetchError(f"Linear API returned HTTP {resp.status_code}: {resp.text}")
                    data = resp.json()
                    if data.get("errors"):
                        raise LinearFetchError(f"Linear GraphQL errors on final fallback: {data.get('errors')}")
                    nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
                    for n in nodes:
                        issues.append(n)
                        total_retrieved += 1
                        if total_retrieved >= max_total:
                            logger.warning("Reached max_total (%s) during final fallback; truncating results.", max_total)
                            capped = True
                            break
                    if capped:
                        break
                    page_info = data.get("data", {}).get("issues", {}).get("pageInfo", {})
                    if not page_info.get("hasNextPage"):
                        break
                    after = page_info.get("endCursor")

                # client-side filter to match requested expression
                due_limit = now + timedelta(days=resolved_lookahead_days)
                filtered = []
                for n in issues:
                    state_name = (n.get("state") or {}).get("name")
                    due = _parse_iso_datetime(n.get("dueDate"))
                    if (state_name in resolved_statuses) or (due is not None and due <= due_limit):
                        filtered.append(n)
                return _filter_closed_issues(filtered, now=now)
            except LinearFetchError as final_exc:
                raise LinearFetchError(f"All Linear fetch attempts failed: {final_exc}")
    finally:
        try:
            session.close()
        except Exception:
            pass


def compute_linear_summary(raw_nodes: List[Dict], lookahead_days: Optional[int] = None, representative_limit: int = 5) -> Dict:
    """Compute summary metrics from raw GraphQL issue nodes.

    Returns a dict with counts and representative lists for:
      - behind (past due and not completed)
      - upcoming (due within lookahead and not completed)
      - completed_this_week (completed within last 7 days)

    NOTE: For "completed_this_week" we use a 7-day lookback window (assumption).
    """
    resolved_lookahead_days: int
    if lookahead_days is None:
        _, resolved_lookahead_days = _get_linear_env_defaults()
    else:
        resolved_lookahead_days = max(0, int(lookahead_days))

    issues = [_node_to_issue(n) for n in raw_nodes]
    now = datetime.now(timezone.utc)
    lookahead_limit = now + timedelta(days=resolved_lookahead_days)
    week_ago = now - timedelta(days=7)

    behind = []
    upcoming = []
    completed = []

    for iss in issues:
        completed_at = iss.completed_at
        due = iss.due_date

        if completed_at and completed_at >= week_ago:
            completed.append(iss)

        # only consider open issues for behind/upcoming (no completedAt)
        if not completed_at:
            if due and due < now:
                behind.append(iss)
            elif due and now <= due <= lookahead_limit:
                upcoming.append(iss)

    # Sort representative lists: behind by oldest due, upcoming by nearest due, completed by most recent
    behind_sorted = sorted(behind, key=lambda i: (i.due_date or datetime.max))
    upcoming_sorted = sorted(upcoming, key=lambda i: (i.due_date or datetime.max))
    completed_sorted = sorted(completed, key=lambda i: (i.completed_at or datetime.min), reverse=True)

    summary = {
        "counts": {
            "behind": len(behind),
            "upcoming": len(upcoming),
            "completed_this_week": len(completed),
            "total_fetched": len(issues),
        },
        "items": {
            "behind": [_issue_to_dict(i) for i in behind_sorted[:representative_limit]],
            "upcoming": [_issue_to_dict(i) for i in upcoming_sorted[:representative_limit]],
            "completed_this_week": [_issue_to_dict(i) for i in completed_sorted[:representative_limit]],
        },
        "meta": {
            "lookahead_days": resolved_lookahead_days,
            "generated_at": now.isoformat(),
        },
    }
    return summary


def _extract_openrouter_choice_text(data: Dict) -> str:
    """Extract plain text from an OpenRouter chat/completions response.

    Handles a few shapes the API may return (string, dict, or list parts).
    """
    choices = data.get("choices") or []
    if not choices:
        return ""

    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")

    # content can be a list of parts, a string, or a dict with 'text'
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        text = "".join(parts)
    elif isinstance(content, dict):
        # Some adapters embed text under 'text' or nested keys
        text = str(content.get("text") or content.get("message") or json.dumps(content))
    else:
        text = str(content or "")

    return text.strip()


def _linear_summary_fallback_text(summary: Dict, flavor: Optional[str] = None) -> str:
    """Deterministic fallback if the model call fails.

    Produces a short plaintext summary grounded in the numeric counts from summary.
    """
    counts = (summary or {}).get("counts", {}) or {}
    behind = int(counts.get("behind", 0) or 0)
    upcoming = int(counts.get("upcoming", 0) or 0)
    completed = int(counts.get("completed_this_week", 0) or 0)
    total = int(counts.get("total_fetched", 0) or 0)
    lookahead = (summary or {}).get("meta", {}).get("lookahead_days")

    # Simple deterministic phrasing — keep it short
    if behind > max(0, completed):
        # Negative performance
        if lookahead is None:
            return f"Kian status: {behind} overdue, {upcoming} due soon, {completed} completed this week. It's a mess — priorities need attention."
        return f"Kian status: {behind} overdue, {upcoming} due in the next {lookahead}d, {completed} completed this week. It's a mess — priorities need attention."
    else:
        # Non-negative / positive
        if lookahead is None:
            return f"Kian status: {completed} completed this week, {upcoming} upcoming, {behind} overdue (of {total} fetched). Nice momentum!"
        return f"Kian status: {completed} completed this week, {upcoming} upcoming in the next {lookahead}d, {behind} overdue (of {total}). Nice momentum!"


def _openrouter_linear_summary(summary: Dict, flavor: Optional[str] = None, max_chars: int = 1800) -> str:
    """Generate a short, stylized Linear status summary via OpenRouter (Grok).

    This helper mirrors the request style used in commands/ragebait_mo.py: it POSTs to
    https://openrouter.ai/api/v1/chat/completions with the Authorization Bearer token and
    HTTP-Referer / X-Title headers. If anything goes wrong, a deterministic fallback is
    returned instead of raising.

    Note: this function is intentionally reusable and is not wired into the /palantir
    command path yet.
    """
    try:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            logger.warning("OPENROUTER_API_KEY not set; returning fallback summary.")
            return _linear_summary_fallback_text(summary, flavor=flavor)

        model = os.environ.get("OPENROUTER_LINEAR_SUMMARY_MODEL", "x-ai/grok-4.1-fast")

        system_prompt = (
            "You are an assistant whose job is to tell Kian's current project status to his "
            "friends based ONLY on the provided Linear data. Be entertaining and concise. "
            "Output plain text only (no markdown, no code fences), about 2-4 sentences. "
            "Make it lively and human: if the numeric facts show problems (more overdue than "
            "completed this week or obvious regression), adopt a frustrated tone — light profanity is allowed. "
            "If performance looks good, be celebratory but still punchy. Always ground claims in the provided facts and do NOT invent new facts or URLs."
        )

        # Provide the model the raw JSON summary and a short human-friendly bullet list
        try:
            summary_json = json.dumps(summary, ensure_ascii=False, indent=2)
        except Exception:
            summary_json = str(summary)

        counts = (summary or {}).get("counts", {}) or {}
        behind = int(counts.get("behind", 0) or 0)
        upcoming = int(counts.get("upcoming", 0) or 0)
        completed = int(counts.get("completed_this_week", 0) or 0)
        total = int(counts.get("total_fetched", 0) or 0)
        lookahead = (summary or {}).get("meta", {}).get("lookahead_days")

        brief = (
            f"Counts: behind={behind}, upcoming={upcoming}, completed_this_week={completed}, total={total}."
        )

        if lookahead is not None:
            brief += f" Lookahead_days={lookahead}."

        user_text = (
            "Use only the facts below to write the short status update for Kian's friends. "
            "Aim for a couple of sentences so it's easy to read in Discord. "
            f"Requested flavor: {flavor or 'casual'}\n\n"
            "BRIEF_FACTS:\n"
            f"{brief}\n\n"
            "REPRESENTATIVE_ITEMS (showing title, id, project when available):\n"
        )

        # Append representative items in a compact form
        items = (summary or {}).get("items", {}) or {}
        for section in ("behind", "upcoming", "completed_this_week"):
            rep = items.get(section) or []
            if rep:
                user_text += f"{section}:\n"
                for it in rep[:5]:
                    tid = it.get("id") or ""
                    title = (it.get("title") or "").strip().replace("\n", " ")
                    project = it.get("project") or ""
                    user_text += f"- {title}"
                    if tid:
                        user_text += f" (#{tid})"
                    if project:
                        user_text += f" [{project}]"
                    user_text += "\n"

        user_text += "\nFULL_SUMMARY_JSON:\n" + summary_json

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.9,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "http://localhost"),
            "X-Title": os.environ.get("OPENROUTER_APP_NAME", "discord-palantir"),
        }

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        logger.debug("OpenRouter linear summary status=%s", resp.status_code)
        if resp.status_code >= 400:
            logger.warning("OpenRouter linear summary request failed: %s", resp.text[:400])
            return _linear_summary_fallback_text(summary, flavor=flavor)

        data = resp.json()
        text = _extract_openrouter_choice_text(data)
        if not text:
            logger.warning("OpenRouter returned empty text for linear summary; using fallback.")
            return _linear_summary_fallback_text(summary, flavor=flavor)

        # Coerce to single-line output and keep below Discord's 2000-char message limit.
        single = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if len(single) > max_chars:
            single = single[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."

        return single
    except Exception as exc:
        logger.exception("Exception while generating linear summary via OpenRouter: %s", exc)
        return _linear_summary_fallback_text(summary, flavor=flavor)

# Audio capture defaults (same keys/behavior as bot.py)
AUDIO_CAPTURE_BACKEND = "arecord"
AUDIO_CAPTURE_DEVICE_ENV = "WEBCAM_AUDIO_DEVICE"
AUDIO_CAPTURE_DEVICE_DEFAULT = "plughw:CARD=Device,DEV=0"
AUDIO_CAPTURE_RATE = 48000
AUDIO_CAPTURE_CHANNELS = 2
AUDIO_CAPTURE_FORMAT = "S16_LE"
AUDIO_CAPTURE_GAIN_PERCENT = 100
AUDIO_POST_GAIN_DB = 4.0


# Module-level client reference; set by register(). Helper functions use this
# client to resolve channels and send files. Kept private.
_client: Optional[discord.Client] = None


def _apply_post_gain_inplace(path: str, gain_db: float) -> None:
    """Apply software gain to a WAV file in place when ffmpeg is available."""
    if gain_db <= 0 or not shutil.which("ffmpeg"):
        return

    boosted_tmp = f"{path}.boosted.wav"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        path,
        "-af",
        f"volume={gain_db}dB",
        boosted_tmp,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Post-gain processing failed: {stderr or 'unknown error'}")

    os.replace(boosted_tmp, path)


def _capture_local_audio_to_tempfile(duration_s: int = 10) -> str:
    """Record audio from the host microphone to a temporary file and return its path.

    This is a best-effort implementation that tries common recorders (arecord, ffmpeg).
    It raises RuntimeError on failure. This function is blocking and intended to be run
    in a thread via asyncio.to_thread().
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    tmp.close()

    backend = AUDIO_CAPTURE_BACKEND
    audio_device = os.environ.get(AUDIO_CAPTURE_DEVICE_ENV, AUDIO_CAPTURE_DEVICE_DEFAULT)

    try:
        if backend == "arecord" and shutil.which("arecord"):
            cmd = [
                "arecord",
                "-D",
                audio_device,
                "-d",
                str(int(duration_s)),
                "-f",
                AUDIO_CAPTURE_FORMAT,
                "-r",
                str(AUDIO_CAPTURE_RATE),
                "-c",
                str(AUDIO_CAPTURE_CHANNELS),
                "-t",
                "wav",
                tmp_path,
            ]
        elif shutil.which("ffmpeg"):
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "alsa",
                "-i",
                audio_device,
                "-t",
                str(int(duration_s)),
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(AUDIO_CAPTURE_RATE),
                "-ac",
                str(AUDIO_CAPTURE_CHANNELS),
                tmp_path,
            ]
        else:
            raise RuntimeError("Audio capture unavailable: neither 'arecord' nor 'ffmpeg' is installed.")

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=int(duration_s) + 15)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Audio capture failed: {stderr or 'unknown error'}")

        # basic sanity check
        if os.path.getsize(tmp_path) < 100:
            raise RuntimeError("Captured audio file is too small; recording may have failed.")

        try:
            _apply_post_gain_inplace(tmp_path, AUDIO_POST_GAIN_DB)
        except Exception as exc:
            logger.warning("Unable to apply post gain to captured audio: %s", exc)

        return tmp_path
    except subprocess.TimeoutExpired as exc:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise RuntimeError(f"Audio capture timed out after {duration_s}s.") from exc
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


async def _palantir_audio_capture_and_send(channel_id: int, duration_s: int = 10) -> None:
    """Fire-and-forget helper: record microphone for duration_s and send file to channel.

    This helper logs exceptions and attempts to post concise error messages in the
    same channel when failures occur. It intentionally returns None and is meant to be
    scheduled with asyncio.create_task(...) from the /palantir handler.
    """
    channel: Any = None
    tmp_path = None
    audio_device = os.environ.get(AUDIO_CAPTURE_DEVICE_ENV, AUDIO_CAPTURE_DEVICE_DEFAULT)
    safe_device = re.sub(r"[^A-Za-z0-9._-]+", "-", str(audio_device)).strip("-") or "unknown-device"
    timestamp = datetime.now().strftime("%Y-%m-%d_%I-%M-%S-%p")
    upload_name = f"{timestamp}-{safe_device}.wav"
    try:
        # Resolve channel (try cache first, then REST fetch)
        if _client is None:
            logger.warning("Palantir audio helper: no client available to resolve channel %s", channel_id)
            return
        channel: Any = _client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await _client.fetch_channel(channel_id)
            except Exception:
                channel = None

        if channel is None:
            logger.warning("Palantir audio helper: could not resolve channel id %s", channel_id)
            return

        # Run blocking capture in a thread to avoid blocking the event loop
        tmp_path = await asyncio.to_thread(_capture_local_audio_to_tempfile, duration_s)

        # Attempt to send the resulting audio file
        try:
            await channel.send(file=discord.File(tmp_path, filename=upload_name))
        except discord.Forbidden:
            # Bot cannot post in channel
            try:
                await channel.send("Failed to send palantir audio: missing permission to post messages here.")
            except Exception:
                logger.exception("Failed to report audio send permission error to channel %s", channel_id)
        except Exception as exc:
            logger.exception("Failed to send palantir audio to channel %s: %s", channel_id, exc)
            try:
                await channel.send(f"Failed to send palantir audio: {exc}")
            except Exception:
                logger.exception("Failed to deliver audio error message to channel %s", channel_id)

    except Exception as exc:
        logger.exception("Unhandled error in palantir audio helper: %s", exc)
        # Try to post a brief error message in the same channel
        try:
            if channel is None and _client is not None:
                channel = _client.get_channel(channel_id)
            if channel is not None:
                await channel.send(f"Palantir audio capture failed: {exc}")
        except Exception:
            logger.exception("Failed to report palantir audio helper error to channel %s", channel_id)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                logger.exception("Error removing temporary audio file: %s", tmp_path)


async def _palantir_linear_summary_and_send(channel_id: int) -> None:
    """Fire-and-forget helper: fetch Linear issues, compute metrics, and post a short audience-facing summary.

    This helper is intentionally non-blocking for the /palantir command and logs but
    swallows errors so it never interrupts the primary capture flow.
    """
    try:
        # Respect env gating; default true
        enabled_raw = os.environ.get(
            "LINEAR_SUMMARY_ENABLED",
            "true" if LINEAR_SUMMARY_ENABLED_DEFAULT else "false",
        )
        if str(enabled_raw).strip().lower() not in ("1", "true", "yes", "y", "on"):
            logger.debug("Linear summary disabled by LINEAR_SUMMARY_ENABLED=%s", enabled_raw)
            return

        # Resolve client/channel early
        if _client is None:
            logger.warning("Palantir linear summary: no client available to resolve channel %s", channel_id)
            return

        channel: Any = _client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await _client.fetch_channel(channel_id)
            except discord.Forbidden:
                logger.warning("Palantir linear summary: missing permission to fetch channel %s", channel_id)
                return
            except Exception as exc:
                logger.exception("Palantir linear summary: failed to resolve channel %s: %s", channel_id, exc)
                return

        # Require LINEAR_API_KEY to actually fetch/post summaries. If missing, do nothing.
        if not os.environ.get("LINEAR_API_KEY"):
            logger.debug("LINEAR_API_KEY not set; skipping linear summary post.")
            return

        # Use env-configured defaults for statuses and lookahead
        statuses, lookahead_days = _get_linear_env_defaults()

        # Fetch data in a thread to avoid blocking the event loop
        try:
            raw_nodes = await asyncio.to_thread(fetch_linear_issues, statuses, lookahead_days)
        except Exception as exc:
            logger.warning("Palantir linear summary: failed to fetch Linear issues: %s", exc)
            raw_nodes = []

        # Compute summary metrics (fast/sync)
        try:
            summary = compute_linear_summary(raw_nodes, lookahead_days)
        except Exception as exc:
            logger.exception("Palantir linear summary: failed to compute summary metrics: %s", exc)
            summary = {"counts": {}, "items": {}, "meta": {"lookahead_days": lookahead_days}}

        # Ask OpenRouter/Grok to produce a short stylized summary. Run in thread.
        try:
            text = await asyncio.to_thread(_openrouter_linear_summary, summary)
        except Exception as exc:
            logger.exception("Palantir linear summary: OpenRouter summary generation failed: %s", exc)
            text = _linear_summary_fallback_text(summary)

        if not text:
            text = _linear_summary_fallback_text(summary)

        # Final send to channel with robust error handling; do not raise.
        try:
            await cast(Any, channel).send(text)
        except discord.Forbidden:
            logger.warning("Palantir linear summary: missing permission to post in channel %s", channel_id)
        except discord.HTTPException as exc:
            logger.exception("Palantir linear summary: HTTP error sending to channel %s: %s", channel_id, exc)
        except Exception as exc:
            logger.exception("Palantir linear summary: unexpected error sending to channel %s: %s", channel_id, exc)
    except Exception as exc:
        logger.exception("Unhandled error in palantir linear summary helper: %s", exc)


def _read_stable_frame(cap: "cv2.VideoCapture", warmup_reads: int = 10, delay_s: float = 0.05):
    """Read a usable frame, skipping initial green placeholder frames some devices emit."""
    last_frame = None
    for i in range(max(1, warmup_reads)):
        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(delay_s)
            continue

        last_frame = frame
        try:
            # Some HDMI capture cards output an initial solid green frame while warming up.
            mean_bgr = frame.mean(axis=(0, 1))
            std_bgr = frame.std(axis=(0, 1))
            looks_uniform = float(std_bgr.max()) < 2.0
            looks_green = mean_bgr[1] > 80 and mean_bgr[0] < 20 and mean_bgr[2] < 20
            if looks_uniform and looks_green and i < warmup_reads - 1:
                time.sleep(delay_s)
                continue
        except Exception:
            # If frame analysis fails, keep the frame and continue normally.
            pass

        if i < warmup_reads - 1:
            time.sleep(delay_s)

    return last_frame


def _apply_timestamp_overlay(frame):
    """Burn an Eastern-time timestamp into the frame."""
    try:
        try:
            now = datetime.now(ZoneInfo("America/New_York"))
        except ZoneInfoNotFoundError:
            logger.exception("IANA timezone data unavailable; falling back to UTC.")
            now = datetime.utcnow()
        timestamp = now.strftime("%Y-%m-%d %-I:%M:%S %p")

        h, w = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = max(0.6, min(w, h) / 1000.0)
        thickness = max(1, int(round(font_scale * 2)))
        (text_w, text_h), baseline = cv2.getTextSize(timestamp, font, font_scale, thickness)

        pad = int(round(min(w, h) * 0.02))
        rect_pad = int(round(min(w, h) * 0.01))

        x = w - text_w - pad
        y = h - pad

        rect_tl = (max(0, x - rect_pad), max(0, y - text_h - rect_pad - baseline))
        rect_br = (min(w, x + text_w + rect_pad), min(h, y + rect_pad))

        overlay = frame.copy()
        cv2.rectangle(overlay, rect_tl, rect_br, (0, 0, 0), thickness=-1)
        alpha = 0.5
        frame = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0)

        cv2.putText(frame, timestamp, (x, y), font, font_scale, (0, 0, 0), thickness=thickness + 2, lineType=cv2.LINE_AA)
        cv2.putText(frame, timestamp, (x, y), font, font_scale, (255, 255, 255), thickness=thickness, lineType=cv2.LINE_AA)
    except Exception:
        logger.exception("Failed to burn timestamp into image. Proceeding without timestamp.")
    return frame


def _configure_hdmi_capture(cap: "cv2.VideoCapture"):
    """Prefer a widescreen HDMI mode so screenshots are not squashed."""
    try:
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    # Keep FOURCC tuning best-effort disabled for static-type compatibility.

    preferred_modes = [
        (1920, 1080),
        (1280, 720),
        (1600, 900),
    ]
    for width, height in preferred_modes:
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
            time.sleep(0.05)
            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            if actual_w == width and actual_h == height:
                logger.info("Configured HDMI capture resolution to %sx%s", actual_w, actual_h)
                return
        except Exception:
            continue

    try:
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        logger.info("Using HDMI capture default resolution %sx%s", actual_w, actual_h)
    except Exception:
        pass


def _capture_remote_webcam_to_tempfile() -> str:
    """Capture one frame from a remote laptop webcam over SSH and return local temp file path."""
    ssh_target = os.environ.get("REMOTE_WEBCAM_SSH_TARGET", "root@laptop3")
    remote_device = os.environ.get("REMOTE_WEBCAM_DEVICE", "/dev/video0")
    remote_output = os.environ.get("REMOTE_WEBCAM_OUTPUT", "/tmp/discord-remote-webcam.jpg")
    timeout_s = int(os.environ.get("REMOTE_WEBCAM_TIMEOUT_SECONDS", "12"))
    warmup_frames = int(os.environ.get("REMOTE_WEBCAM_WARMUP_FRAMES", "30"))
    warmup_fps = int(os.environ.get("REMOTE_WEBCAM_WARMUP_FPS", "15"))
    settle_seconds = float(os.environ.get("REMOTE_WEBCAM_SETTLE_SECONDS", "0.4"))

    if warmup_frames < 0:
        warmup_frames = 0
    if warmup_fps < 1:
        warmup_fps = 1
    if settle_seconds < 0:
        settle_seconds = 0.0

    remote_device_q = shlex.quote(remote_device)
    remote_output_q = shlex.quote(remote_output)

    warmup_cmd = ""
    if warmup_frames > 0:
        warmup_cmd = (
            f"ffmpeg -f video4linux2 -framerate {warmup_fps} -i {remote_device_q} "
            f"-frames:v {warmup_frames} -f null - -loglevel error >/dev/null 2>&1"
        )

    capture_cmd = (
        f"ffmpeg -f video4linux2 -i {remote_device_q} -frames:v 1 {remote_output_q} "
        f"-y -loglevel error >/dev/null 2>&1 && cat {remote_output_q}"
    )

    if warmup_cmd:
        remote_cmd = f"{warmup_cmd} && sleep {settle_seconds:.3f} && {capture_cmd}"
    else:
        remote_cmd = capture_cmd

    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                ssh_target,
                remote_cmd,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Remote webcam capture timed out after {timeout_s}s via SSH ({ssh_target}).") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Remote webcam capture failed via SSH ({ssh_target}): {stderr or 'unknown error'}")

    img_bytes = proc.stdout or b""
    if len(img_bytes) < 100:
        raise RuntimeError(f"Remote webcam capture returned too little data from {ssh_target}.")

    tmp = tempfile.NamedTemporaryFile(suffix="-remote.jpg", delete=False)
    try:
        tmp.write(img_bytes)
        tmp.flush()
    finally:
        tmp.close()

    remote_frame = cv2.imread(tmp.name)
    if remote_frame is not None:
        remote_frame = _apply_timestamp_overlay(remote_frame)
        if not cv2.imwrite(tmp.name, remote_frame):
            logger.warning("Failed to write timestamped remote webcam image; sending original remote frame.")
    else:
        logger.warning("Failed to decode remote webcam image for timestamping; sending original remote frame.")

    return tmp.name


def _capture_local_webcam_to_tempfile() -> str:
    """Capture one frame from the host webcam and return local temp file path."""
    cap = None
    try:
        if hasattr(cv2, "CAP_V4L2"):
            cap = cv2.VideoCapture("/dev/video0", cv2.CAP_V4L2)
        else:
            cap = cv2.VideoCapture("/dev/video0")

        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            cap = cv2.VideoCapture(0)

        if not cap.isOpened():
            raise RuntimeError(
                "Could not open video device /dev/video0 (or index 0). "
                "Ensure /dev/video0 is passed into the container and allowed."
            )

        frame = _read_stable_frame(cap)
        if frame is None:
            raise RuntimeError("Failed to read frame from camera.")

        frame = _apply_timestamp_overlay(frame)
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp_path = tmp.name
        tmp.close()
        if not cv2.imwrite(tmp_path, frame):
            raise RuntimeError("Failed to write image to temporary file.")

        return tmp_path
    finally:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            logger.exception("Error releasing camera.")


def _capture_hdmi_screenshot_to_tempfile() -> str:
    """Capture one frame from HDMI capture card and return local temp file path."""
    hdmi_cap = None
    try:
        if hasattr(cv2, "CAP_V4L2"):
            hdmi_cap = cv2.VideoCapture("/dev/video2", cv2.CAP_V4L2)
        else:
            hdmi_cap = cv2.VideoCapture("/dev/video2")

        if not hdmi_cap.isOpened():
            try:
                hdmi_cap.release()
            except Exception:
                pass
            hdmi_cap = cv2.VideoCapture(2)

        if not hdmi_cap.isOpened():
            raise RuntimeError("Could not open video device /dev/video2 (or index 2).")

        _configure_hdmi_capture(hdmi_cap)
        hdmi_frame = _read_stable_frame(hdmi_cap)
        if hdmi_frame is None:
            raise RuntimeError("Failed to read frame from HDMI capture card.")

        hdmi_frame = _apply_timestamp_overlay(hdmi_frame)
        hdmi_tmp = tempfile.NamedTemporaryFile(suffix="-screenshot.jpg", delete=False)
        hdmi_tmp_path = hdmi_tmp.name
        hdmi_tmp.close()
        if not cv2.imwrite(hdmi_tmp_path, hdmi_frame):
            raise RuntimeError("Failed to write HDMI screenshot to temporary file.")

        return hdmi_tmp_path
    finally:
        try:
            if hdmi_cap is not None:
                hdmi_cap.release()
        except Exception:
            logger.exception("Error releasing HDMI capture device.")


async def _run_capture_job(name: str, capture_fn: Callable[[], str], timeout_s: int) -> dict:
    """Run one capture in background and normalize success/error shape."""
    try:
        path = await asyncio.wait_for(asyncio.to_thread(capture_fn), timeout=timeout_s)
        return {"name": name, "path": path, "error": None}
    except asyncio.TimeoutError:
        return {"name": name, "path": None, "error": f"{name} capture timed out after {timeout_s}s."}
    except Exception as exc:
        logger.warning("%s capture failed for /palantir: %s", name, exc)
        msg = str(exc).strip() or f"{name} capture failed."
        return {"name": name, "path": None, "error": msg}


async def _run_redaction_job(name: str, path: str, timeout_s: int) -> dict:
    """Run one image redaction in background and normalize success/error shape."""
    try:
        metadata = await asyncio.wait_for(
            asyncio.to_thread(redact_sensitive_media_inplace, path),
            timeout=timeout_s,
        )
        return {"name": name, "path": path, "error": None, "metadata": metadata}
    except asyncio.TimeoutError:
        return {
            "name": name,
            "path": None,
            "error": f"{name} redaction timed out after {timeout_s}s.",
            "metadata": None,
        }
    except Exception as exc:
        logger.warning("%s redaction failed for /palantir: %s", name, exc)
        msg = str(exc).strip() or f"{name} redaction failed."
        return {"name": name, "path": None, "error": msg, "metadata": None}


def register(
    tree: "discord.app_commands.CommandTree",
    client: discord.Client,
    allowed_guilds: tuple[discord.Object, ...] | None = None,
) -> None:
    """Register the /palantir command on the provided CommandTree and bind the client.

    The registered handler mirrors the behavior from bot.py. Helper functions in
    this module are intentionally private (leading underscore).
    """
    global _client
    _client = client

    @tree.command(name="palantir", description="idek wtf im doing anymore")
    async def palantir(interaction: discord.Interaction):
        """Slash command handler: capture local webcam, remote webcam, and HDMI screenshot."""
        await interaction.response.defer()
        # Trigger fire-and-forget helpers exactly once per /palantir invocation.
        try:
            channel_id = None
            interaction_channel = getattr(interaction, "channel", None)
            if interaction_channel is not None:
                channel_id = interaction_channel.id
            else:
                channel_id = getattr(interaction, "channel_id", None)

            if channel_id is not None:
                # Allow override of duration via env var; default 10s
                try:
                    duration = int(os.environ.get("WEBCAM_AUDIO_DURATION_SECONDS", "10"))
                except Exception:
                    duration = 10
                # Schedule non-blocking fire-and-forget task
                try:
                    asyncio.create_task(_palantir_audio_capture_and_send(channel_id, duration_s=duration))
                except Exception:
                    logger.exception("Failed to create background task for palantir audio helper")
                try:
                    if heart_rate.is_enabled():
                        asyncio.create_task(heart_rate.send_palantir_heart_rate(channel_id))
                except Exception:
                    logger.exception("Failed to create background task for palantir heart-rate helper")
                try:
                    asyncio.create_task(_palantir_linear_summary_and_send(channel_id))
                except Exception:
                    logger.exception("Failed to create background task for palantir linear summary helper")
        except Exception:
            logger.exception("Unexpected error scheduling palantir background helpers")

        tmp_paths = []
        try:
            timeout_s = int(os.environ.get("WEBCAM_CAPTURE_TIMEOUT_SECONDS", "20"))
            capture_jobs = [
                _run_capture_job("Local webcam", _capture_local_webcam_to_tempfile, timeout_s),
                _run_capture_job("Remote webcam", _capture_remote_webcam_to_tempfile, timeout_s),
                _run_capture_job("HDMI screenshot", _capture_hdmi_screenshot_to_tempfile, timeout_s),
            ]
            results = await asyncio.gather(*capture_jobs)

            redaction_timeout_s = int(os.environ.get("WEBCAM_REDACTION_TIMEOUT_SECONDS", "30"))
            redaction_inputs = []

            files = []
            errors = []
            notes = []
            for result in results:
                path = result.get("path")
                if path:
                    tmp_paths.append(path)
                    redaction_inputs.append({"name": result.get("name", "capture"), "path": path})
                if result.get("error"):
                    errors.append(result["error"])

            if redaction_inputs:
                redaction_jobs = [
                    _run_redaction_job(item["name"], item["path"], redaction_timeout_s)
                    for item in redaction_inputs
                ]
                redaction_results = await asyncio.gather(*redaction_jobs)

                for redaction_result in redaction_results:
                    if redaction_result.get("error"):
                        errors.append(redaction_result["error"])
                        continue

                    redacted_path = redaction_result.get("path")
                    if redacted_path:
                        files.append(discord.File(redacted_path))

                    metadata = redaction_result.get("metadata") or {}
                    if metadata.get("full_blur"):
                        notes.append(f"{redaction_result.get('name', 'Capture')}: full-frame blur fallback applied.")

            if not files and errors:
                await interaction.followup.send("Palantir captures failed:\n" + "\n".join(f"- {err}" for err in errors))
                return

            content = None
            if errors:
                content = "Some captures failed:\n" + "\n".join(f"- {err}" for err in errors)
            if notes:
                note_block = "\n".join(f"- {note}" for note in notes)
                if content is None:
                    content = "Redaction notes:\n" + note_block
                else:
                    content = content + "\n\nRedaction notes:\n" + note_block

            if files:
                if content is None:
                    await interaction.followup.send(files=files)
                else:
                    await interaction.followup.send(content=content, files=files)
            else:
                await interaction.followup.send(content or "No captures produced output.")

        except Exception as exc:
            logger.exception("Error during /palantir")
            # Attempt to send an ephemeral error message to the user
            try:
                await interaction.followup.send(f"Error capturing image: {exc}", ephemeral=True)
            except Exception:
                # If followup fails, try response (may already be deferred)
                try:
                    await interaction.response.send_message(f"Error capturing image: {exc}", ephemeral=True)
                except Exception:
                    logger.exception("Failed to deliver error message to user.")
        finally:
            for tmp_path in tmp_paths:
                try:
                    os.remove(tmp_path)
                except Exception:
                    logger.exception("Error removing temporary file: %s", tmp_path)
