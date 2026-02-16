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

import discord
from discord import app_commands

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
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync global command tree on startup
        await self.tree.sync()
        logger.info("Command tree synced.")


client = CamBot()


@client.tree.command(name="webcam", description="Take a picture from kian's laptop webcam")
async def webcam(interaction: discord.Interaction):
    """Slash command handler: capture and send one JPEG from /dev/video0."""
    await interaction.response.defer()
    tmp_path = None
    cap = None
    try:
        # Try to open the host device. Prefer /dev/video0, fallback to index 0.
        if hasattr(cv2, "CAP_V4L2"):
            cap = cv2.VideoCapture("/dev/video0", cv2.CAP_V4L2)
        else:
            cap = cv2.VideoCapture("/dev/video0")

        if not cap.isOpened():
            # fallback to device index 0
            try:
                cap.release()
            except Exception:
                pass
            cap = cv2.VideoCapture(0)

        if not cap.isOpened():
            raise RuntimeError("Could not open video device /dev/video0 (or index 0). Ensure /dev/video0 is passed into the container and allowed.")

        ret, frame = cap.read()
        if not ret or frame is None:
            raise RuntimeError("Failed to read frame from camera.")
        # Burn a timestamp into the image (Eastern timezone)
        try:
            try:
                now = datetime.now(ZoneInfo("America/New_York"))
            except ZoneInfoNotFoundError:
                logger.exception("IANA timezone data unavailable; falling back to UTC.")
                now = datetime.utcnow()
            timestamp = now.strftime("%Y-%m-%d %-I:%M:%S %p")

            # Determine text size/placement
            h, w = frame.shape[:2]
            font = cv2.FONT_HERSHEY_SIMPLEX
            # Scale font relative to image size
            font_scale = max(0.6, min(w, h) / 1000.0)
            thickness = max(1, int(round(font_scale * 2)))
            (text_w, text_h), baseline = cv2.getTextSize(timestamp, font, font_scale, thickness)

            pad = int(round(min(w, h) * 0.02))  # padding from edges
            rect_pad = int(round(min(w, h) * 0.01))

            x = w - text_w - pad
            y = h - pad

            # Rectangle behind text (translucent)
            rect_tl = (max(0, x - rect_pad), max(0, y - text_h - rect_pad - baseline))
            rect_br = (min(w, x + text_w + rect_pad), min(h, y + rect_pad))

            overlay = frame.copy()
            cv2.rectangle(overlay, rect_tl, rect_br, (0, 0, 0), thickness=-1)
            alpha = 0.5
            # blend the rectangle onto the image
            frame = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0)

            # Draw text with a thin black outline for extra contrast
            cv2.putText(frame, timestamp, (x, y), font, font_scale, (0, 0, 0), thickness=thickness + 2, lineType=cv2.LINE_AA)
            cv2.putText(frame, timestamp, (x, y), font, font_scale, (255, 255, 255), thickness=thickness, lineType=cv2.LINE_AA)
        except Exception:
            # If timestamping fails for any reason, continue without overlay
            logger.exception("Failed to burn timestamp into image. Proceeding without timestamp.")

        # Write to a temporary JPEG file
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp_path = tmp.name
        tmp.close()
        ok = cv2.imwrite(tmp_path, frame)
        if not ok:
            raise RuntimeError("Failed to write image to temporary file.")

        # Send the image with no caption
        await interaction.followup.send(file=discord.File(tmp_path))

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
        # Release camera and remove temporary file
        try:
            if cap is not None:
                cap.release()
        except Exception:
            logger.exception("Error releasing camera.")
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                logger.exception("Error removing temporary file: %s", tmp_path)


if __name__ == "__main__":
    try:
        client.run(DISCORD_TOKEN)
    except Exception:
        logger.exception("Bot terminated unexpectedly.")
        sys.exit(1)
