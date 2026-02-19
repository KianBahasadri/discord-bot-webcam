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
from typing import Callable, Optional

import discord
import asyncio
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
        # Sync global command tree on startup
        await self.tree.sync()
        logger.info("Command tree synced.")


client = CamBot()


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


@client.tree.command(name="webcam", description="idek wtf im doing anymore")
async def webcam(interaction: discord.Interaction):
    """Slash command handler: capture local webcam, remote webcam, and HDMI screenshot."""
    await interaction.response.defer()
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


@client.tree.command(name="ragebait-mo", description="Ragebait Mohammad Sarhat and show the proof")
async def ragebait_mo(interaction: discord.Interaction, phone: Optional[str] = None):
    """Slash command handler: extract topic from Mo's recent messages and run helper-driven flow."""
    await interaction.response.defer()
    try:
        # Allow an optional per-invocation phone override. If provided, MO_CELL_NUMBER is not required.
        def _sanitize_phone_input(raw: Optional[str]) -> Optional[str]:
            if not raw:
                return None
            s = raw.strip()
            # strip common separators and tel: prefix
            s = re.sub(r"[ \-\(\)\.]", "", s)
            s = re.sub(r"^tel:", "", s, flags=re.I)
            # normalize digits and leading +
            if s.count("+") > 1:
                return None
            if not s.startswith("+"):
                digits = re.sub(r"\D", "", s)
                s = "+" + digits
            else:
                s = "+" + re.sub(r"\D", "", s[1:])
            # E.164-ish validation: + followed by 2-15 digits, not starting with 0
            if re.match(r"^\+[1-9]\d{1,14}$", s):
                return s
            return None

        phone_override = None
        if phone is not None:
            phone_override = _sanitize_phone_input(phone)
            if phone_override is None:
                await interaction.followup.send(
                    "Invalid phone number provided. Please supply an E.164-style number like +15555555555 or just digits.",
                    ephemeral=True,
                )
                return

        base_required = [
            "MO_USER_ID",
            "ELEVENLABS_API_KEY",
            "ELEVENLABS_AGENT_ID",
            "ELEVENLABS_AGENT_PHONE_NUMBER_ID",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION",
        ]
        required_env_vars = list(base_required)
        if not phone_override:
            required_env_vars.append("MO_CELL_NUMBER")

        missing = [name for name in required_env_vars if not os.environ.get(name)]
        if missing:
            await interaction.followup.send(
                f"Missing required environment variables for /ragebait-mo: {', '.join(missing)}",
                ephemeral=True,
            )
            return

        try:
            mo_user_id_raw = os.environ.get("MO_USER_ID")
            mo_user_id = int(mo_user_id_raw) if mo_user_id_raw is not None else None
        except Exception:
            await interaction.followup.send("MO_USER_ID must be a valid Discord user ID (integer).", ephemeral=True)
            return
        if mo_user_id is None:
            await interaction.followup.send("MO_USER_ID must be set for /ragebait-mo.", ephemeral=True)
            return

        try:
            from ragebait_helpers import (
                generate_topic_with_azure,
                start_elevenlabs_call,
                poll_conversation_until_terminal,
                format_transcript,
                fetch_conversation_audio_with_retry,
            )
        except Exception as exc:
            logger.exception("Failed to import ragebait_helpers in /ragebait-mo")
            await interaction.followup.send(
                f"/ragebait-mo is unavailable: failed to import ragebait_helpers ({exc}).",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("Error: could not determine channel.", ephemeral=True)
            return

        # Fetch up to 100 recent messages from the channel
        try:
            msgs = [m async for m in channel.history(limit=100)]
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't read message history in this channel (missing access). "
                "Please grant View Channel and Read Message History. "
                "Helpful note: this can also happen if the Discord application was installed as an app-only install "
                "(applications.commands) instead of including a bot install (bot scope).",
                ephemeral=True,
            )
            return

        # Filter to MO_USER_ID, non-bot, non-empty content
        mo_msgs = [m for m in msgs if getattr(m, "author", None) and getattr(m.author, "id", None) == mo_user_id and not getattr(m.author, "bot", False) and getattr(m, "content", "") and m.content.strip()]

        if not mo_msgs:
            prompt_text = "2026 Iran US peace negotiations"
            await interaction.followup.send(
                "No recent messages from Mo found; using default topic context: 2026 Iran US peace negotiations.",
                ephemeral=False,
            )
        else:
            # Build input text (chronological)
            mo_msgs = list(reversed(mo_msgs))
            prompt_text = "\n\n".join(m.content.strip() for m in mo_msgs if m.content and m.content.strip())

        # Generate topic using Azure helper (run in thread to avoid blocking)
        await interaction.followup.send("Extracting topic from recent messages...", ephemeral=False)
        try:
            dynamic_topic = await asyncio.to_thread(generate_topic_with_azure, prompt_text, "Mo recent conversation")
        except Exception as exc:
            # Do not hard-fail if topic generation errors; log and continue with a
            # sensible fallback topic string so the command can proceed.
            logger.exception("generate_topic_with_azure failed")
            dynamic_topic = "Mo recent conversation"
            await interaction.followup.send("Failed to generate dynamic topic via Azure; using fallback topic.", ephemeral=False)

        await interaction.followup.send(f"Generated topic: {dynamic_topic}", ephemeral=False)

        # Start ElevenLabs agent call
        await interaction.followup.send("Starting ElevenLabs agent call...", ephemeral=False)
        try:
            effective_to = phone_override if phone_override else os.environ.get("MO_CELL_NUMBER")
            start_resp = await asyncio.to_thread(
                start_elevenlabs_call, dynamic_topic, override_to_number=effective_to
            )
        except Exception as exc:
            logger.exception("start_elevenlabs_call failed")
            await interaction.followup.send(f"Failed to start ElevenLabs call: {exc}", ephemeral=True)
            return

        # Attempt to extract conversation id from start response with robust fallbacks
        def _recursive_find_id(obj, depth=0, max_depth=6):
            if depth > max_depth:
                return None
            # dict: check common id keys first, then traverse
            if isinstance(obj, dict):
                for key in ("conversation_id", "conversationId"):
                    if key in obj and obj[key] not in (None, ""):
                        val = obj[key]
                        if isinstance(val, (str, int)):
                            return str(val)
                        if isinstance(val, dict):
                            # nested id under dict
                            for ik in ("conversation_id", "conversationId"):
                                if ik in val and val[ik]:
                                    return str(val[ik])
                # prefer certain container keys to speed up search
                for container in ("conversation", "data", "result", "payload", "body", "response"):
                    if container in obj:
                        res = _recursive_find_id(obj[container], depth + 1, max_depth)
                        if res:
                            return res
                # fallback: search all values
                for v in obj.values():
                    res = _recursive_find_id(v, depth + 1, max_depth)
                    if res:
                        return res

            # list: iterate
            if isinstance(obj, list):
                for item in obj:
                    res = _recursive_find_id(item, depth + 1, max_depth)
                    if res:
                        return res

            # string: try parsing JSON, JSON substring, then regex patterns
            if isinstance(obj, str):
                s = obj.strip()
                # try full JSON parse
                try:
                    parsed = json.loads(s)
                    return _recursive_find_id(parsed, depth + 1, max_depth)
                except Exception:
                    pass

                # try to find a balanced JSON substring and parse it
                start = s.find("{")
                if start != -1:
                    depth_count = 0
                    for i in range(start, len(s)):
                        ch = s[i]
                        if ch == "{":
                            depth_count += 1
                        elif ch == "}":
                            depth_count -= 1
                            if depth_count == 0:
                                jsub = s[start : i + 1]
                                try:
                                    parsed = json.loads(jsub)
                                    res = _recursive_find_id(parsed, depth + 1, max_depth)
                                    if res:
                                        return res
                                except Exception:
                                    pass
                                break

                # regex search for common id keys
                patterns = [
                    r'"conversation_id"\s*:\s*"([^\"]+)"',
                    r'"conversationId"\s*:\s*"([^\"]+)"',
                    r'conversation_id=([A-Za-z0-9_\-:\.]+)',
                    r'conversationId=([A-Za-z0-9_\-:\.]+)',
                ]
                for pat in patterns:
                    m = re.search(pat, s)
                    if m:
                        return m.group(1)

            return None

        conversation_id = _recursive_find_id(start_resp)
        # keep backward-compatible behavior: if response was a raw string, allow it
        if not conversation_id and isinstance(start_resp, str):
            conversation_id = start_resp

        if not conversation_id:
            logger.error("Could not determine conversation id from start_elevenlabs_call response: %r", start_resp)
            await interaction.followup.send("Failed to determine conversation id from ElevenLabs start response.", ephemeral=True)
            return

        await interaction.followup.send(f"Conversation started (id: {conversation_id}). Polling until finished...", ephemeral=False)

        # Poll until terminal
        try:
            conversation_payload = await asyncio.to_thread(poll_conversation_until_terminal, str(conversation_id))
        except Exception as exc:
            logger.exception("poll_conversation_until_terminal failed")
            await interaction.followup.send(f"Failed while polling conversation: {exc}", ephemeral=True)
            return

        # Format transcript
        try:
            transcript_text = await asyncio.to_thread(format_transcript, conversation_payload)
        except Exception as exc:
            logger.exception("format_transcript failed")
            await interaction.followup.send(f"Failed to format transcript: {exc}", ephemeral=True)
            return

        if not transcript_text:
            await interaction.followup.send("Transcript is empty.", ephemeral=True)
            return

        # Attempt to download the conversation audio from the documented
        # ElevenLabs endpoint and upload it to Discord so we can include a
        # stable recording URL in the final transcript. Failure here should
        # not abort the command; we continue without a recording link.
        audio_url = None
        try:
            # Download audio bytes with bounded retry policy (run in thread to avoid blocking)
            audio_bytes, audio_ext = await asyncio.to_thread(fetch_conversation_audio_with_retry, str(conversation_id))
            # Write to temp file and upload as attachment, capturing the message
            tmp_audio = tempfile.NamedTemporaryFile(suffix=audio_ext or ".mp3", delete=False)
            try:
                tmp_audio.write(audio_bytes)
                tmp_audio.flush()
                tmp_audio.close()
                # Send the audio as a followup attachment and wait for the message
                msg_with_audio = await interaction.followup.send(file=discord.File(tmp_audio.name), wait=True)
                if msg_with_audio and getattr(msg_with_audio, "attachments", None):
                    audio_url = msg_with_audio.attachments[0].url
            finally:
                try:
                    os.remove(tmp_audio.name)
                except Exception:
                    logger.exception("Failed to remove temporary audio file: %s", tmp_audio.name)
        except Exception as exc:
            # Distinguish expected transient "audio not ready" exhaustion from other errors.
            msg = str(exc or "")
            if isinstance(exc, RuntimeError) and msg.startswith("Exhausted retries fetching conversation audio"):
                # Expected transient case: warn but do not log a noisy traceback.
                logger.warning("Conversation audio unavailable after retries for %s: %s", conversation_id, msg)
            else:
                # Unexpected: log full exception with traceback for diagnostics
                logger.exception("Failed to download or upload conversation audio; proceeding without recording link")

        # Deliver final transcript. If long, attach as a file
        if len(transcript_text) <= 1900:
            msg = f"Final transcript:\n\n{transcript_text}"
            if audio_url:
                msg += f"\n\nRecording: {audio_url}"
            await interaction.followup.send(msg)
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
            tmp_path = tmp.name
            try:
                tmp.write(transcript_text.encode("utf-8"))
                tmp.flush()
                tmp.close()
                header = "Final transcript (attached)."
                if audio_url:
                    header += f"\nRecording: {audio_url}"
                await interaction.followup.send(header, file=discord.File(tmp_path))
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    logger.exception("Failed to remove temporary transcript file: %s", tmp_path)

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
