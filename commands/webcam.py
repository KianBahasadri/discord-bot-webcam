#!/usr/bin/env python3
"""
Command module for the /webcam slash command.

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
from typing import Any, Callable, Optional

import discord
import asyncio
from media_redaction import redact_sensitive_media_inplace

try:
    import cv2
except Exception:
    # Match bot.py behavior: surface a clear error if OpenCV is not available.
    print("Error: failed to import OpenCV (opencv-python-headless).", file=sys.stderr)
    raise

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

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
        if _client is None:
            logger.warning("Webcam audio helper: no client available to resolve channel %s", channel_id)
            return
        channel = _client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await _client.fetch_channel(channel_id)
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
            if channel is None and _client is not None:
                channel = _client.get_channel(channel_id)
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


def register(tree: "discord.app_commands.CommandTree", client: discord.Client) -> None:
    """Register the /webcam command on the provided CommandTree and bind the client.

    The registered handler mirrors the behavior from bot.py. Helper functions in
    this module are intentionally private (leading underscore).
    """
    global _client
    _client = client

    @tree.command(name="webcam", description="idek wtf im doing anymore")
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
