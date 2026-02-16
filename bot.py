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


@client.tree.command(name="snap", description="Take a picture from kian's laptop webcam")
async def snap(interaction: discord.Interaction):
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
        logger.exception("Error during /snap")
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
