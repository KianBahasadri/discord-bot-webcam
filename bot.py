#!/usr/bin/env python3
"""
Discord bot that captures one frame from /dev/video0 and sends it as a file.
Uses discord.py app_commands and OpenCV (opencv-python-headless).
Reads token from DISCORD_TOKEN env var.
"""
import os
import sys
import tempfile
import logging
import re
import json
import time
import subprocess
import shlex
import shutil
from typing import Any, Callable, Optional

import discord
import asyncio
import requests
from discord import app_commands
from media_redaction import redact_sensitive_media_inplace

try:
    import cv2
except Exception:
    print("Error: failed to import OpenCV (opencv-python-headless).", file=sys.stderr)
    raise

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

AUDIO_CAPTURE_BACKEND = "arecord"
AUDIO_CAPTURE_DEVICE_ENV = "WEBCAM_AUDIO_DEVICE"
AUDIO_CAPTURE_DEVICE_DEFAULT = "plughw:CARD=Device,DEV=0"
AUDIO_CAPTURE_RATE = 48000
AUDIO_CAPTURE_CHANNELS = 2
AUDIO_CAPTURE_FORMAT = "S16_LE"
AUDIO_CAPTURE_GAIN_PERCENT = 100
AUDIO_POST_GAIN_DB = 4.0


def _extract_alsa_card_selector(audio_device: str) -> str:
    """Best-effort card selector for amixer -c from an ALSA device string."""
    card_match = re.search(r"CARD=([^,]+)", audio_device)
    if card_match:
        return card_match.group(1)

    hw_match = re.match(r"(?:plug)?hw:(\d+)(?:,\d+)?", audio_device)
    if hw_match:
        return hw_match.group(1)

    return "Device"


def _set_audio_gain_on_startup() -> None:
    """Raise capture gain at startup so recordings are not too quiet."""
    audio_device = os.environ.get(AUDIO_CAPTURE_DEVICE_ENV, AUDIO_CAPTURE_DEVICE_DEFAULT)
    card_selector = _extract_alsa_card_selector(audio_device)

    try:
        subprocess.run(
            ["amixer", "-c", card_selector, "sset", "Mic", f"{AUDIO_CAPTURE_GAIN_PERCENT}%", "cap"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=8,
        )
        subprocess.run(
            ["amixer", "-c", card_selector, "sset", "Auto Gain Control", "on"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=8,
        )
        logger.info("Applied startup mic gain on ALSA card selector '%s'.", card_selector)
    except Exception:
        logger.exception("Failed to apply startup mic gain for audio device '%s'.", audio_device)


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

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN not set. Exiting.")
    print("Error: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)


class CamBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # Required so the bot can read recent message content
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await asyncio.to_thread(_set_audio_gain_on_startup)
        # Sync global command tree on startup
        await self.tree.sync()
        logger.info("Command tree synced.")

    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return

        channel_id = getattr(getattr(message, "channel", None), "id", None)
        if channel_id is None:
            return

        session = _ragebait_sessions.get(channel_id)
        if not session or not session.get("active"):
            return

        content = (getattr(message, "content", "") or "").strip()
        if not content:
            return

        session["last_activity"] = time.time()
        session["transcript"].append(
            {
                "role": "user",
                "author_id": str(getattr(message.author, "id", "")),
                "author": getattr(message.author, "display_name", None)
                or getattr(message.author, "name", "user"),
                "content": content,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        await _process_ragebait_turn(message.channel, channel_id)


client = CamBot()


_ragebait_sessions: dict[int, dict[str, Any]] = {}
_ragebait_locks: dict[int, asyncio.Lock] = {}


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
        lines.append(f"{i}. [{role}] {author}: {content}")
    return "\n".join(lines)


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
    if not should_continue:
        ragebait = ""
    if should_continue and not ragebait:
        should_continue = False

    return {"continue": should_continue, "ragebait": ragebait}


def _openrouter_ragebait_turn(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for /ragebait-mo.")

    model = os.environ.get("OPENROUTER_RAGEBAIT_MODEL")
    if not model:
        raise RuntimeError("OPENROUTER_RAGEBAIT_MODEL is required for /ragebait-mo.")

    transcript_text = _build_ragebait_transcript_text(transcript)
    if not transcript_text:
        return {"continue": False, "ragebait": ""}

    schema = {
        "name": "ragebait_turn",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "continue": {"type": "boolean"},
                "ragebait": {"type": "string"},
            },
            "required": ["continue", "ragebait"],
            "additionalProperties": False,
        },
    }

    system_prompt = (
        "You control whether a Discord ragebait session continues. "
        "Read the full transcript and decide if the conversation is still about the bot/topic. "
        "If it is off-topic or unrelated, set continue=false and ragebait=''. "
        "If it should continue, set continue=true and provide one short, provocative reply in ragebait. "
        "Return JSON only matching the schema."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Transcript:\n{transcript_text}"},
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
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:600]}")

    data = response.json()
    return _parse_ragebait_structured_output(data)


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


async def _process_ragebait_turn(channel, channel_id: int) -> None:
    lock = _ragebait_locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        session = _ragebait_sessions.get(channel_id)
        if not session or not session.get("active"):
            return

        limit_reason = _ragebait_limits_exceeded(session)
        if limit_reason:
            await _end_ragebait_session(channel, channel_id, limit_reason)
            return

        transcript = session.get("transcript") or []
        try:
            decision = await asyncio.to_thread(_openrouter_ragebait_turn, transcript)
        except Exception as exc:
            logger.exception("Failed to generate ragebait turn")
            await channel.send(f"Ragebait session ended due to model error: {exc}")
            _ragebait_sessions.pop(channel_id, None)
            return

        if not decision.get("continue"):
            await _end_ragebait_session(channel, channel_id, "model_stop")
            return

        reply = str(decision.get("ragebait", "") or "").strip()
        if not reply:
            await _end_ragebait_session(channel, channel_id, "model_stop")
            return

        if len(reply) > 1800:
            reply = reply[:1797] + "..."

        sent = await channel.send(reply)
        session = _ragebait_sessions.get(channel_id)
        if not session or not session.get("active"):
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

    try:
        mjpg = cv2.VideoWriter_fourcc(*"MJPG")
        cap.set(cv2.CAP_PROP_FOURCC, mjpg)
    except Exception:
        pass

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
        logger.warning("%s capture failed for /webcam: %s", name, exc)
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
        logger.warning("%s redaction failed for /webcam: %s", name, exc)
        msg = str(exc).strip() or f"{name} redaction failed."
        return {"name": name, "path": None, "error": msg, "metadata": None}


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


async def _webcam_audio_capture_and_send(channel_id: int, duration_s: int = 10) -> None:
    """Fire-and-forget helper: record microphone for duration_s and send file to channel.

    This helper logs exceptions and attempts to post concise error messages in the
    same channel when failures occur. It intentionally returns None and is meant to be
    scheduled with asyncio.create_task(...) from the /webcam handler.
    """
    channel: Any = None
    tmp_path = None
    audio_device = os.environ.get(AUDIO_CAPTURE_DEVICE_ENV, AUDIO_CAPTURE_DEVICE_DEFAULT)
    safe_device = re.sub(r"[^A-Za-z0-9._-]+", "-", str(audio_device)).strip("-") or "unknown-device"
    timestamp = datetime.now().strftime("%Y-%m-%d_%I-%M-%S-%p")
    upload_name = f"{timestamp}-{safe_device}.wav"
    try:
        # Resolve channel (try cache first, then REST fetch)
        channel = client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except Exception:
                channel = None

        if channel is None:
            logger.warning("Webcam audio helper: could not resolve channel id %s", channel_id)
            return

        # Run blocking capture in a thread to avoid blocking the event loop
        tmp_path = await asyncio.to_thread(_capture_local_audio_to_tempfile, duration_s)

        # Attempt to send the resulting audio file
        try:
            await channel.send(file=discord.File(tmp_path, filename=upload_name))
        except discord.Forbidden:
            # Bot cannot post in channel
            try:
                await channel.send("Failed to send webcam audio: missing permission to post messages here.")
            except Exception:
                logger.exception("Failed to report audio send permission error to channel %s", channel_id)
        except Exception as exc:
            logger.exception("Failed to send webcam audio to channel %s: %s", channel_id, exc)
            try:
                await channel.send(f"Failed to send webcam audio: {exc}")
            except Exception:
                logger.exception("Failed to deliver audio error message to channel %s", channel_id)

    except Exception as exc:
        logger.exception("Unhandled error in webcam audio helper: %s", exc)
        # Try to post a brief error message in the same channel
        try:
            if channel is None:
                channel = client.get_channel(channel_id)
            if channel is not None:
                await channel.send(f"Webcam audio capture failed: {exc}")
        except Exception:
            logger.exception("Failed to report webcam audio helper error to channel %s", channel_id)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                logger.exception("Error removing temporary audio file: %s", tmp_path)


async def _delete_most_recent_bot_message(channel) -> dict:
    """Helper: delete the most recent message authored by this bot in the given channel.

    Returns a dict with keys:
      - deleted: bool
      - message_id: optional id of deleted message
      - error: optional error string on failure

    This helper intentionally performs no interaction responses; callers should
    handle user-facing messages.
    """
    try:
        if client.user is None:
            return {"deleted": False, "message_id": None, "error": "bot_not_ready"}
        # Iterate recent messages (newest first)
        async for msg in channel.history(limit=200):
            if getattr(msg, "author", None) and getattr(msg.author, "id", None) == client.user.id:
                    try:
                        await msg.delete()
                        return {"deleted": True, "message_id": getattr(msg, "id", None), "error": None}
                    except discord.Forbidden:
                        return {"deleted": False, "message_id": None, "error": "forbidden"}
                    except discord.NotFound:
                        # Message disappeared between listing and deletion; continue scanning
                        continue
                    except Exception as exc:
                        logger.exception("Failed to delete bot message %s: %s", getattr(msg, "id", None), exc)
                        return {"deleted": False, "message_id": None, "error": str(exc)}

        return {"deleted": False, "message_id": None, "error": "none_found"}
    except discord.Forbidden:
        return {"deleted": False, "message_id": None, "error": "forbidden"}
    except Exception as exc:
        logger.exception("Error scanning channel history for delete helper: %s", exc)
        return {"deleted": False, "message_id": None, "error": str(exc)}


async def _read_channel_history(channel: Any, limit: int) -> list[Any]:
    history = getattr(channel, "history", None)
    if not callable(history):
        return []
    iterator: Any = history(limit=limit)
    out: list[Any] = []
    async for msg in iterator:
        out.append(msg)
    return out


@client.tree.command(name="delete", description="Delete the most recent bot-authored message in this channel")
async def delete(interaction: discord.Interaction):
    """Slash command handler: delete the most recent bot message in the same channel.

    Provides concise user-facing messages and handles permission errors.
    """
    await interaction.response.defer()
    channel = interaction.channel
    if channel is None:
        await interaction.followup.send("Error: could not determine channel.", ephemeral=True)
        return

    result = await _delete_most_recent_bot_message(channel)
    if result.get("deleted"):
        await interaction.followup.send("Deleted most recent bot message.", ephemeral=True)
        return

    err = result.get("error") or "unknown"
    if err == "none_found":
        await interaction.followup.send("No recent bot message to delete.", ephemeral=True)
    elif err == "forbidden":
        await interaction.followup.send("I lack permission to delete messages in this channel.", ephemeral=True)
    else:
        await interaction.followup.send(f"Failed to delete bot message: {err}", ephemeral=True)



@client.tree.command(name="webcam", description="idek wtf im doing anymore")
async def webcam(interaction: discord.Interaction):
    """Slash command handler: capture local webcam, remote webcam, and HDMI screenshot."""
    await interaction.response.defer()
    # Trigger a fire-and-forget audio capture/send helper exactly once per /webcam invocation.
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
                asyncio.create_task(_webcam_audio_capture_and_send(channel_id, duration_s=duration))
            except Exception:
                logger.exception("Failed to create background task for webcam audio helper")
    except Exception:
        logger.exception("Unexpected error scheduling webcam audio helper")

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
            await interaction.followup.send("Webcam captures failed:\n" + "\n".join(f"- {err}" for err in errors))
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
        logger.exception("Error during /webcam")
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


@client.tree.command(name="ragebait-mo", description="Start live ragebait chat with Mo in this channel")
async def ragebait_mo(interaction: discord.Interaction, debug: bool = False):
    await interaction.response.defer()
    try:
        if not os.environ.get("OPENROUTER_API_KEY"):
            await interaction.followup.send("OPENROUTER_API_KEY is required for /ragebait-mo.", ephemeral=True)
            return

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("Error: could not determine channel.", ephemeral=True)
            return

        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await interaction.followup.send("Error: channel is missing an id.", ephemeral=True)
            return

        history_limit = int(os.environ.get("RAGEBAIT_START_HISTORY_LIMIT", "60"))
        transcript: list[dict[str, Any]] = []

        if callable(getattr(channel, "history", None)):
            try:
                recent = await _read_channel_history(channel, history_limit)
                recent.reverse()
                for m in recent:
                    content = (getattr(m, "content", "") or "").strip()
                    if not content:
                        continue
                    if getattr(getattr(m, "author", None), "bot", False):
                        continue
                    transcript.append(
                        {
                            "role": "user",
                            "author_id": str(getattr(getattr(m, "author", None), "id", "")),
                            "author": getattr(getattr(m, "author", None), "display_name", None)
                            or getattr(getattr(m, "author", None), "name", "user"),
                            "content": content,
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    )
            except discord.Forbidden:
                await interaction.followup.send(
                    "I can't read message history in this channel. Please grant Read Message History.",
                    ephemeral=True,
                )
                return

        now = time.time()
        invoker_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
        bot_user_id = str(getattr(getattr(client, "user", None), "id", "") or "")
        assistant_author_id = invoker_id if debug and invoker_id else bot_user_id

        _ragebait_sessions[channel_id] = {
            "active": True,
            "started_at": now,
            "last_activity": now,
            "turns": 0,
            "transcript": transcript,
            "assistant_author_id": assistant_author_id,
        }

        model = os.environ.get("OPENROUTER_RAGEBAIT_MODEL")
        if not model:
            await interaction.followup.send(
                "OPENROUTER_RAGEBAIT_MODEL is required for /ragebait-mo.",
                ephemeral=True,
            )
            return
        debug_note = " Debug mode: using your user id for assistant transcript entries." if debug else ""
        await interaction.followup.send(
            f"Ragebait session started in this channel using `{model}`. "
            "Send messages and I will keep replying until the model marks the topic as off-topic."
            f"{debug_note}"
        )
    except Exception as exc:
        logger.exception("Unhandled error in /ragebait-mo")
        try:
            await interaction.followup.send(f"Error running /ragebait-mo: {exc}", ephemeral=True)
        except Exception:
            logger.exception("Failed to deliver error message to user.")


if __name__ == "__main__":
    try:
        client.run(DISCORD_TOKEN)
    except Exception:
        logger.exception("Bot terminated unexpectedly.")
        sys.exit(1)
