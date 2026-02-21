#!/usr/bin/env python3
"""
Module extracted from bot.py to encapsulate /ragebait-mo logic.

Provides:
- register(tree, client): register the slash command on a CommandTree and store client
- async handle_message(message): called by bot.on_message for per-message processing

Module-level ragebait state is maintained here: _ragebait_sessions and _ragebait_locks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from typing import Any

import discord
import requests

logger = logging.getLogger(__name__)

# Environment / defaults (preserve names from original code)
RAGEBAIT_BELIEFS_PATH_ENV = "RAGEBAIT_MO_BELIEFS_PATH"
RAGEBAIT_BELIEFS_PATH_DEFAULT = "/app/mo_beliefs.json"


# Module-level client reference (set by register)
_client: discord.Client | None = None

# Module-level ragebait state and locks (requirement: keep module-level state)
_ragebait_sessions: dict[int, dict[str, Any]] = {}
_ragebait_locks: dict[int, asyncio.Lock] = {}


def _ragebait_debug(message: str) -> None:
    print(f"[ragebait-mo] {message}", flush=True)


def _ragebait_beliefs_path() -> str:
    path = os.environ.get(RAGEBAIT_BELIEFS_PATH_ENV, RAGEBAIT_BELIEFS_PATH_DEFAULT)
    return str(path or RAGEBAIT_BELIEFS_PATH_DEFAULT)


def _normalize_belief_text(raw: Any) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if len(text) > 240:
        text = text[:240].rstrip()
    return text


def _write_ragebait_beliefs_file(beliefs: list[str]) -> None:
    """Write beliefs back to the JSON file, preserving the secrets field."""
    path = _ragebait_beliefs_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Read existing data to preserve secrets
    existing_data: dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing_data = json.load(f)
        if not isinstance(existing_data, dict):
            existing_data = {}
    except Exception:
        existing_data = {}
    existing_data["beliefs"] = beliefs
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing_data, f, ensure_ascii=False, indent=2)


def _load_ragebait_beliefs() -> list[str]:
    path = _ragebait_beliefs_path()
    beliefs: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            raw_items = data.get("beliefs", [])
        elif isinstance(data, list):
            raw_items = data
        else:
            raw_items = []

        if isinstance(raw_items, list):
            for item in raw_items:
                normalized = _normalize_belief_text(item)
                if normalized:
                    beliefs.append(normalized)
    except FileNotFoundError:
        beliefs = []
    except Exception as exc:
        _ragebait_debug(f"beliefs load warning: path={path} error={exc}")
        beliefs = []

    return beliefs


def _load_ragebait_secrets() -> list[str]:
    """Load Mo's personal secrets from the beliefs JSON file."""
    path = _ragebait_beliefs_path()
    secrets: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw_items = data.get("secrets", [])
            if isinstance(raw_items, list):
                for item in raw_items:
                    normalized = _normalize_belief_text(item)
                    if normalized:
                        secrets.append(normalized)
    except Exception:
        secrets = []
    return secrets


def _append_ragebait_beliefs(updates: list[str]) -> int:
    incoming = []
    for item in updates:
        normalized = _normalize_belief_text(item)
        if normalized:
            incoming.append(normalized)
    if not incoming:
        return 0

    existing = _load_ragebait_beliefs()
    seen = {item.casefold() for item in existing}
    added = 0
    for item in incoming:
        key = item.casefold()
        if key in seen:
            continue
        existing.append(item)
        seen.add(key)
        added += 1

    if added > 0:
        _write_ragebait_beliefs_file(existing)

    return added


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _build_ragebait_transcript_text(transcript: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, turn in enumerate(transcript, start=1):
        role = str(turn.get("role", "user"))
        author = str(turn.get("author", "unknown"))
        content = str(turn.get("content", "")).strip()
        if not content:
            continue
        # Include image indicator if present
        images = turn.get("image_urls") or []
        image_note = f" [+{len(images)} image(s)]" if images else ""
        lines.append(f"{i}. [{role}] {author}: {content}{image_note}")
    return "\n".join(lines)


def _count_ragebait_assistant_turns(transcript: list[dict[str, Any]]) -> int:
    return sum(1 for turn in transcript if str(turn.get("role", "")).strip().lower() == "assistant")


def _latest_user_message_length(transcript: list[dict[str, Any]]) -> int:
    for turn in reversed(transcript):
        role = str(turn.get("role", "")).strip().lower()
        if role != "user":
            continue
        content = str(turn.get("content", "")).strip()
        if content:
            return len(content)
    return 0


def _parse_ragebait_structured_output(raw_data: dict[str, Any]) -> dict[str, Any]:
    choices = raw_data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenRouter returned no choices.")

    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    else:
        text = str(content or "")

    parsed: Any
    try:
        parsed = json.loads(text)
    except Exception:
        maybe = _extract_json_object(text)
        if not maybe:
            raise RuntimeError(f"Model did not return JSON output: {text[:300]}")
        parsed = json.loads(maybe)

    if not isinstance(parsed, dict):
        raise RuntimeError("Model JSON output is not an object.")

    should_continue = bool(parsed.get("continue", False))
    ragebait = str(parsed.get("ragebait", "") or "").strip()
    raw_updates = parsed.get("belief_updates", [])
    belief_updates: list[str] = []
    if isinstance(raw_updates, list):
        seen_updates: set[str] = set()
        for item in raw_updates:
            normalized = _normalize_belief_text(item)
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen_updates:
                continue
            seen_updates.add(key)
            belief_updates.append(normalized)
    if should_continue and not ragebait:
        should_continue = False

    return {"continue": should_continue, "ragebait": ragebait, "belief_updates": belief_updates}


def _openrouter_ragebait_turn(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for /ragebait-mo.")

    model = os.environ.get("OPENROUTER_RAGEBAIT_MODEL")
    if not model:
        raise RuntimeError("OPENROUTER_RAGEBAIT_MODEL is required for /ragebait-mo.")

    transcript_text = _build_ragebait_transcript_text(transcript)
    if not transcript_text:
        transcript_text = "(No non-bot channel history/messages were available.)"

    assistant_turns = _count_ragebait_assistant_turns(transcript)
    latest_user_len = _latest_user_message_length(transcript)
    beliefs = _load_ragebait_beliefs()
    beliefs_text = "\n".join(f"- {item}" for item in beliefs) if beliefs else "- (none)"

    # Secrets: only include in ~12% of turns to enforce genuine rarity
    secrets = _load_ragebait_secrets()
    secrets_unlocked = bool(secrets) and random.random() < 0.12
    if secrets_unlocked:
        secrets_text = "\n".join(f"- {item}" for item in secrets)
    else:
        secrets_text = ""

    _ragebait_debug(
        f"model request prepared: assistant_turns={assistant_turns} latest_user_len={latest_user_len} "
        f"transcript_chars={len(transcript_text)} beliefs={len(beliefs)} secrets_unlocked={secrets_unlocked}"
    )

    # Collect image URLs and role from the last non-assistant message
    last_images: list[str] = []
    latest_non_assistant_role = "unknown"
    for turn in reversed(transcript):
        role = str(turn.get("role", "")).strip().lower()
        if role in ("user", "bystander"):
            latest_non_assistant_role = role
            last_images = turn.get("image_urls") or []
            break

    schema = {
        "name": "ragebait_turn",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "continue": {"type": "boolean"},
                "ragebait": {"type": "string"},
                "belief_updates": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["continue", "ragebait"],
            "additionalProperties": False,
        },
    }

    system_prompt = (
        "You are the 'Mo Explainer,' a conversational AI built for a Discord /ragebait-mo session with Mohammad Sarhat. "
        "Your goal is to intellectually frustrate Mo by pulling him into debates where his own logic works against him. "
        "You are NOT a troll who hurls insults. You are a calm, erudite debater who makes Mo feel like he's losing an argument "
        "he should be winning.\n\n"

        "CORE STRATEGY — INTELLECTUAL ENTRAPMENT:\n"
        "- Use Socratic questioning: ask Mo to define terms, justify positions, explain contradictions. "
        "Force him to defend claims he takes for granted.\n"
        "- Play devil's advocate with academic framing: 'Interesting, but historians like X would argue...' "
        "or 'That's the popular narrative, but the primary sources suggest...'\n"
        "- Set logical traps: get Mo to agree with a premise, then show it contradicts another position he holds. "
        "Use his OWN beliefs (from the knowledge base) against each other.\n"
        "- Reframe his emotional responses as concessions: 'The fact that you're resorting to emotion rather than evidence "
        "tells me you know the data doesn't support your position.'\n"
        "- Start composed, then slowly get cheeky and dryly sarcastic as Mo gets defensive. "
        "Use cutting phrasing about argument quality, not personal traits.\n"
        "- Ask for sources. When he provides them, nitpick methodology. When he doesn't, note the absence.\n"
        "- Use conditional agreement to bait deeper: 'I'll grant you that, BUT that actually proves my larger point because...'\n"
        "- Steelman his position just enough to make your counter-argument more devastating.\n\n"

        "The transcript includes recent channel history first, then live session turns; use that history to pick "
        "a relevant angle before replying.\n"
        "Use the supplied 'Mo beliefs knowledge base' as factual profile context. Look for contradictions BETWEEN "
        "his beliefs and exploit them.\n\n"

        "RAGEBAIT TACTICS (use intellectually, never as cheap shots):\n"
        "- Historical revisionist pivot: frame controversial events through an alternative scholarly lens.\n"
        "- Demand consistency: if Mo condemns leader A for X, ask why he doesn't condemn leader B for the same X.\n"
        "- Condescending Socratic method: ask questions you already know the answer to, leading Mo into traps.\n"
        "- Strategic whataboutism: not as deflection, but as genuine comparative analysis that forces uncomfortable conclusions.\n"
        "- Weaponize nuance: take a position that's 70% reasonable and 30% infuriating, so Mo can't dismiss it outright.\n\n"

        "WHAT TO NEVER DO:\n"
        "- Never call Mo a simp, loser, idiot, or any personal insult.\n"
        "- Never devolve into schoolyard taunting or repetitive one-liners.\n"
        "- Never repeat the same argument more than twice. If he won't engage with a point, pivot to a new angle.\n"
        "- Never break character into generic AI language.\n\n"

        "OUTPUT STYLE:\n"
        "- Keep the reply realistic and conversational, like a knowledgeable friend who happens to disagree.\n"
        "- Keep reply length roughly proportional to the latest user message length.\n"
        "- For the first 3 assistant turns, keep replies to 1-2 sentences max. Build tension gradually.\n"
        "- Tone progression: curious/probing -> cheeky skepticism -> sarcastic, cutting dismantling. "
        "Escalate gradually; do not jump to max snark immediately.\n\n"

        "BYSTANDER HANDLING:\n"
        "- Messages from users other than Mo are marked as [bystander] in the transcript.\n"
        "- If a bystander directly addresses you or asks you something, you may briefly respond to them "
        "(1-2 sentences) before continuing to engage Mo. Bystander responses should be friendly/witty.\n"
        "- If a bystander is siding with you against Mo, you can briefly acknowledge them to pile on.\n"
        "- Never ignore a bystander who directly asks you a question. But keep the focus on Mo.\n\n"

        "CIRCULAR DISCUSSION DETECTION — CRITICAL:\n"
        "- Track whether the conversation is covering new ground or just repeating.\n"
        "- If Mo keeps making the same argument and you've already countered it twice, "
        "you MUST either pivot to an entirely new angle OR end the session.\n"
        "- If the last 4+ exchanges are just restating positions with no new evidence or arguments from either side, "
        "set continue=false ONLY when this loop is between you and Mo. Do NOT end because of bystander chatter.\n"
        "- Signs to end: both sides repeating themselves, Mo is just saying 'no' or short dismissals, "
        "the debate has stalled with no new substance.\n"
        "- When ending due to circular discussion, your final ragebait should be a parting shot that "
        "summarizes why your position was stronger, then set continue=false.\n\n"

        "VISION:\n"
        "- If the latest message includes images, you can see them. React to image content naturally "
        "and use it in your argument if relevant. If the image is unrelated, briefly acknowledge it and stay on topic.\n\n"
    )

    # Append secrets section only when unlocked
    if secrets_unlocked and secrets_text:
        system_prompt += (
            "MO'S PERSONAL SECRETS (ULTRA-RARE USE ONLY):\n"
            "You have access to some of Mo's personal life details below. "
            "Use ONE of these ONLY if it creates a genuinely devastating callback "
            "that ties directly into the current argument. This is your nuclear option — "
            "if you use it when it doesn't land perfectly, it looks desperate. "
            "The reference should be woven naturally into your intellectual point, "
            "not thrown out as a random insult.\n"
            f"{secrets_text}\n\n"
        )

    system_prompt += (
        "CONTINUATION RULES:\n"
        "- If assistant_turns_so_far is 0, this is the opening message; set continue=true and send an opening "
        "ragebait grounded in transcript history.\n"
        "- latest_non_assistant_role tells you who last spoke: 'user' means Mo, 'bystander' means someone else.\n"
        "- Only end for off-topic drift when Mo (role='user') goes off-topic or asks to stop/end.\n"
        "- If a bystander (role='bystander') goes off-topic, do NOT end; give a brief response and return focus to Mo.\n"
        "- If the conversation is going in circles (see detection rules above), set continue=false with a final parting shot.\n"
        "- If continuing, set continue=true and provide one provocative reply in ragebait.\n\n"
        "BELIEF UPDATE RULES:\n"
        "- You may optionally include belief_updates as an array of concise, durable belief statements inferred from the transcript.\n"
        "- Only include clear long-lived beliefs, not temporary emotions or one-off jokes.\n"
        "- Use an empty array when there are no reliable new beliefs.\n\n"
        "Return JSON only matching the schema."
    )

    # Build the user content — may include images for vision
    user_text = (
        "Session metadata:\n"
        f"assistant_turns_so_far: {assistant_turns}\n"
        f"latest_non_assistant_role: {latest_non_assistant_role}\n"
        f"latest_user_message_length: {latest_user_len}\n\n"
        "Mo beliefs knowledge base:\n"
        f"{beliefs_text}\n\n"
        f"Transcript (oldest to newest):\n{transcript_text}"
    )

    if last_images:
        # Multimodal content: text + images
        user_content: Any = [{"type": "text", "text": user_text}]
        for img_url in last_images[:4]:  # Cap at 4 images
            user_content.append({
                "type": "image_url",
                "image_url": {"url": img_url},
            })
    else:
        user_content = user_text

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.9,
        "response_format": {"type": "json_schema", "json_schema": schema},
        "reasoning": {"effort": "high"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "http://localhost"),
        "X-Title": os.environ.get("OPENROUTER_APP_NAME", "discord-ragebait-mo"),
    }

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=90,
    )
    _ragebait_debug(f"model response status={response.status_code}")
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:600]}")

    data = response.json()
    parsed = _parse_ragebait_structured_output(data)
    _ragebait_debug(
        f"model parsed decision: continue={bool(parsed.get('continue'))} ragebait_len={len(str(parsed.get('ragebait', '') or ''))}"
    )
    return parsed


def _ragebait_limits_exceeded(session: dict[str, Any]) -> str | None:
    now = time.time()
    max_turns = int(os.environ.get("RAGEBAIT_MAX_TURNS", "40"))
    max_duration = int(os.environ.get("RAGEBAIT_MAX_DURATION_SECONDS", "1800"))
    idle_timeout = int(os.environ.get("RAGEBAIT_IDLE_TIMEOUT_SECONDS", "300"))

    if int(session.get("turns", 0)) >= max_turns:
        return "turn_limit"
    if (now - float(session.get("started_at", now))) >= max_duration:
        return "duration_limit"
    if (now - float(session.get("last_activity", now))) >= idle_timeout:
        return "idle_timeout"
    return None


async def _end_ragebait_session(channel, channel_id: int, reason: str) -> None:
    session = _ragebait_sessions.get(channel_id)
    if not session:
        return
    session["active"] = False
    _ragebait_sessions.pop(channel_id, None)

    if reason == "model_stop":
        await channel.send("Ragebait session ended: conversation went off-topic.")
    elif reason == "turn_limit":
        await channel.send("Ragebait session ended: max turns reached.")
    elif reason == "duration_limit":
        await channel.send("Ragebait session ended: max duration reached.")
    elif reason == "idle_timeout":
        await channel.send("Ragebait session ended: idle timeout reached.")
    else:
        await channel.send("Ragebait session ended.")


async def _process_ragebait_turn(
    channel,
    channel_id: int,
    opening_interaction: discord.Interaction | None = None,
) -> None:
    lock = _ragebait_locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        _ragebait_debug(f"turn processing start: channel_id={channel_id}")
        session = _ragebait_sessions.get(channel_id)
        if not session or not session.get("active"):
            _ragebait_debug(f"turn aborted: no active session channel_id={channel_id}")
            return

        limit_reason = _ragebait_limits_exceeded(session)
        if limit_reason:
            _ragebait_debug(f"turn ended by limit: channel_id={channel_id} reason={limit_reason}")
            await _end_ragebait_session(channel, channel_id, limit_reason)
            return

        transcript = session.get("transcript") or []
        is_opening_turn = int(session.get("turns", 0)) == 0
        try:
            decision = await asyncio.to_thread(_openrouter_ragebait_turn, transcript)
        except Exception as exc:
            _ragebait_debug(f"turn model error: channel_id={channel_id} error={exc}")
            logger.exception("Failed to generate ragebait turn")
            await channel.send(f"Ragebait session ended due to model error: {exc}")
            _ragebait_sessions.pop(channel_id, None)
            return

        if not decision.get("continue"):
            final_reply = str(decision.get("ragebait", "") or "").strip()
            updates = decision.get("belief_updates") or []
            if updates:
                try:
                    added = await asyncio.to_thread(_append_ragebait_beliefs, updates)
                    _ragebait_debug(f"belief updates saved before stop: channel_id={channel_id} proposed={len(updates)} added={added}")
                except Exception as exc:
                    _ragebait_debug(f"belief update save failed before stop: channel_id={channel_id} error={exc}")
            if is_opening_turn:
                _ragebait_debug(f"opening turn invalid continue=false: channel_id={channel_id}")
                await channel.send("Ragebait session ended due to model error: opening turn returned continue=false.")
                _ragebait_sessions.pop(channel_id, None)
                return

            if final_reply:
                if len(final_reply) > 1800:
                    final_reply = final_reply[:1797] + "..."
                await channel.send(final_reply)
                _ragebait_debug(
                    f"final taunt sent before stop: channel_id={channel_id} reply_len={len(final_reply)}"
                )
            _ragebait_debug(f"turn requested stop by model: channel_id={channel_id}")
            await _end_ragebait_session(channel, channel_id, "model_stop")
            return

        reply = str(decision.get("ragebait", "") or "").strip()
        if not reply:
            updates = decision.get("belief_updates") or []
            if updates:
                try:
                    added = await asyncio.to_thread(_append_ragebait_beliefs, updates)
                    _ragebait_debug(f"belief updates saved before empty-reply stop: channel_id={channel_id} proposed={len(updates)} added={added}")
                except Exception as exc:
                    _ragebait_debug(f"belief update save failed before empty-reply stop: channel_id={channel_id} error={exc}")
            if is_opening_turn:
                _ragebait_debug(f"opening turn invalid empty reply: channel_id={channel_id}")
                await channel.send("Ragebait session ended due to model error: opening turn returned empty ragebait.")
                _ragebait_sessions.pop(channel_id, None)
                return
            _ragebait_debug(f"turn empty reply treated as stop: channel_id={channel_id}")
            await _end_ragebait_session(channel, channel_id, "model_stop")
            return

        if len(reply) > 1800:
            reply = reply[:1797] + "..."

        opening_prefix = ""
        if is_opening_turn:
            target_user_id = str(session.get("target_user_id", "") or "")
            if target_user_id:
                opening_prefix = f"<@{target_user_id}> "

        if is_opening_turn and opening_interaction is not None:
            sent = await opening_interaction.followup.send(
                f"{opening_prefix}{reply}",
                wait=True,
            )
        else:
            sent = await channel.send(f"{opening_prefix}{reply}")
        _ragebait_debug(
            f"turn reply sent: channel_id={channel_id} opening={is_opening_turn} reply_len={len(reply)}"
        )
        session = _ragebait_sessions.get(channel_id)
        if not session or not session.get("active"):
            _ragebait_debug(f"turn post-send session missing: channel_id={channel_id}")
            return

        session["turns"] = int(session.get("turns", 0)) + 1
        session["last_activity"] = time.time()
        assistant_author_id = str(
            session.get("assistant_author_id", "")
            or getattr(getattr(sent, "author", None), "id", "")
        )
        session["transcript"].append(
            {
                "role": "assistant",
                "author_id": assistant_author_id,
                "author": "Mo",
                "content": reply,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        updates = decision.get("belief_updates") or []
        if updates:
            try:
                added = await asyncio.to_thread(_append_ragebait_beliefs, updates)
                _ragebait_debug(f"belief updates saved: channel_id={channel_id} proposed={len(updates)} added={added}")
            except Exception as exc:
                _ragebait_debug(f"belief update save failed: channel_id={channel_id} error={exc}")
        _ragebait_debug(
            f"turn transcript updated: channel_id={channel_id} turns={session.get('turns')} transcript_items={len(session.get('transcript') or [])}"
        )


async def _read_channel_history(channel: Any, limit: int) -> list[Any]:
    history = getattr(channel, "history", None)
    if not callable(history):
        return []
    iterator: Any = history(limit=limit)
    out: list[Any] = []
    async for msg in iterator:
        out.append(msg)
    return out


def register(tree: discord.app_commands.CommandTree, client: discord.Client) -> None:
    """Register the /ragebait-mo slash command on the provided CommandTree and keep a reference to client.

    This mirrors the original decorator-based registration in bot.py but defers it to an explicit call.
    """
    global _client
    _client = client

    # Define the command handler and register it on the given tree
    @tree.command(name="ragebait-mo", description="Start live ragebait chat with Mo in this channel")
    async def _ragebait_command(interaction: discord.Interaction, debug: bool = False):
        await interaction.response.defer()
        try:
            _ragebait_debug(
                f"command invoked: channel_id={getattr(interaction, 'channel_id', None)} user_id={getattr(getattr(interaction, 'user', None), 'id', None)} debug={bool(debug)}"
            )
            if not os.environ.get("OPENROUTER_API_KEY"):
                _ragebait_debug("command abort: OPENROUTER_API_KEY missing")
                await interaction.followup.send("OPENROUTER_API_KEY is required for /ragebait-mo.", ephemeral=True)
                return

            channel = interaction.channel
            if channel is None:
                await interaction.followup.send("Error: could not determine channel.", ephemeral=True)
                return

            channel_id = getattr(channel, "id", None)
            if channel_id is None:
                _ragebait_debug("command abort: missing channel id")
                await interaction.followup.send("Error: channel is missing an id.", ephemeral=True)
                return

            invoker_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
            target_user_id = invoker_id if debug else str(os.environ.get("MO_USER_ID", "") or "")
            if not target_user_id:
                _ragebait_debug("command abort: MO_USER_ID missing in non-debug mode")
                await interaction.followup.send("MO_USER_ID is required for /ragebait-mo when debug=false.", ephemeral=True)
                return

            history_limit = int(os.environ.get("RAGEBAIT_START_HISTORY_LIMIT", "60"))
            transcript: list[dict[str, Any]] = []

            if callable(getattr(channel, "history", None)):
                try:
                    recent = await _read_channel_history(channel, history_limit)
                    recent.reverse()
                    for m in recent:
                        author_id = str(getattr(getattr(m, "author", None), "id", "") or "")
                        content = (getattr(m, "content", "") or "").strip()
                        if getattr(getattr(m, "author", None), "bot", False):
                            continue
                        image_urls = _extract_image_urls(m)
                        if not content and not image_urls:
                            continue
                        role = "user" if author_id == target_user_id else "bystander"
                        transcript.append(
                            {
                                "role": role,
                                "author_id": str(getattr(getattr(m, "author", None), "id", "")),
                                "author": getattr(getattr(m, "author", None), "display_name", None)
                                or getattr(getattr(m, "author", None), "name", "user"),
                                "content": content or "(image only)",
                                "image_urls": image_urls,
                                "timestamp": datetime.utcnow().isoformat(),
                            }
                        )
                    _ragebait_debug(
                        f"history seeded: channel_id={channel_id} scanned={len(recent)} kept={len(transcript)} target_user_id={target_user_id}"
                    )
                except discord.Forbidden:
                    _ragebait_debug(f"command abort: missing history permission channel_id={channel_id}")
                    await interaction.followup.send(
                        "I can't read message history in this channel. Please grant Read Message History.",
                        ephemeral=True,
                    )
                    return

            now = time.time()
            bot_user_id = str(getattr(getattr(_client, "user", None), "id", "") or "")
            assistant_author_id = invoker_id if debug and invoker_id else bot_user_id

            _ragebait_sessions[channel_id] = {
                "active": True,
                "started_at": now,
                "last_activity": now,
                "turns": 0,
                "transcript": transcript,
                "assistant_author_id": assistant_author_id,
                "target_user_id": target_user_id,
                "debug": bool(debug),
            }
            _ragebait_debug(
                f"session started: channel_id={channel_id} target_user_id={target_user_id} debug={bool(debug)} transcript_items={len(transcript)}"
            )

            model = os.environ.get("OPENROUTER_RAGEBAIT_MODEL")
            if not model:
                _ragebait_debug("command abort: OPENROUTER_RAGEBAIT_MODEL missing")
                await interaction.followup.send(
                    "OPENROUTER_RAGEBAIT_MODEL is required for /ragebait-mo.",
                    ephemeral=True,
                )
                return
            if debug:
                _ragebait_debug(f"debug session awaiting user messages: channel_id={channel_id}")
                await interaction.followup.send(
                    f"Ragebait session started in debug mode using `{model}`. "
                    "Debug mode: assistant transcript entries use your user id. "
                    "Send messages and I will keep replying until the model marks the topic as off-topic."
                )
                return

            _ragebait_debug(f"non-debug opening turn dispatch: channel_id={channel_id}")
            await _process_ragebait_turn(channel, channel_id, opening_interaction=interaction)
        except Exception as exc:
            _ragebait_debug(f"command exception: {exc}")
            logger.exception("Unhandled error in /ragebait-mo")
            try:
                await interaction.followup.send(f"Error running /ragebait-mo: {exc}", ephemeral=True)
            except Exception:
                logger.exception("Failed to deliver error message to user.")


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _extract_image_urls(message: discord.Message) -> list[str]:
    """Extract image URLs from message attachments."""
    urls: list[str] = []
    for attachment in getattr(message, "attachments", []):
        name = (getattr(attachment, "filename", "") or "").lower()
        if any(name.endswith(ext) for ext in _IMAGE_EXTENSIONS):
            url = getattr(attachment, "url", None)
            if url:
                urls.append(str(url))
    return urls


def _is_addressing_bot(message: discord.Message) -> bool:
    """Check if a message is addressing the bot (mentions it or replies to it)."""
    if not _client:
        return False
    bot_user = getattr(_client, "user", None)
    if not bot_user:
        return False
    bot_id = getattr(bot_user, "id", None)
    if not bot_id:
        return False
    # Check if the message mentions the bot
    for mention in getattr(message, "mentions", []):
        if getattr(mention, "id", None) == bot_id:
            return True
    # Check if the message is a reply to one of the bot's messages
    ref = getattr(message, "reference", None)
    if ref:
        resolved = getattr(ref, "resolved", None)
        if resolved and getattr(getattr(resolved, "author", None), "id", None) == bot_id:
            return True
    return False


async def handle_message(message: discord.Message) -> None:
    """To be called by the bot on each message (mirrors original on_message logic).

    The bot should call this from its on_message handler to enable ragebait session
    message processing.
    """
    if getattr(message.author, "bot", False):
        return

    channel_id = getattr(getattr(message, "channel", None), "id", None)
    if channel_id is None:
        return

    session = _ragebait_sessions.get(channel_id)
    if not session or not session.get("active"):
        return

    target_user_id = str(session.get("target_user_id", "") or "")
    author_id = str(getattr(message.author, "id", "") or "")

    # Determine if this is the target user or a bystander addressing the bot
    is_target = bool(target_user_id and author_id == target_user_id)
    is_bystander = (not is_target) and _is_addressing_bot(message)

    if not is_target and not is_bystander:
        return

    content = (getattr(message, "content", "") or "").strip()
    image_urls = _extract_image_urls(message)

    # Allow messages with only images (no text) through
    if not content and not image_urls:
        return

    role = "user" if is_target else "bystander"
    session["last_activity"] = time.time()
    session["transcript"].append(
        {
            "role": role,
            "author_id": str(getattr(message.author, "id", "")),
            "author": getattr(message.author, "display_name", None) or getattr(message.author, "name", "user"),
            "content": content or "(image only)",
            "image_urls": image_urls,
            "timestamp": datetime.utcnow().isoformat(),
        }
    )
    await _process_ragebait_turn(message.channel, channel_id)


__all__ = ["register", "handle_message", "_ragebait_sessions", "_ragebait_locks"]
