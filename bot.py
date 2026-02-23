#!/usr/bin/env python3
"""
Thin bootstrap for the Discord bot. It loads configuration, sets up logging,
creates the CamBot client, registers command modules, preserves startup audio
gain behavior, and delegates message events to the ragebait_mo handler.

Command logic lives in commands/*.py and is registered here by calling
their register(tree, client) functions.
"""
from __future__ import annotations

import os
import sys
import asyncio
import logging
import subprocess
from typing import Any

import discord
from discord import app_commands

# Import command modules. These modules expose register(tree, client).
from commands import webcam as webcam_cmd
from commands import delete as delete_cmd
from commands import ragebait_mo as ragebait_cmd
from commands import heart_rate as heart_rate_cmd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Preserve audio startup behavior constants used by the original bot.
AUDIO_CAPTURE_DEVICE_ENV = "WEBCAM_AUDIO_DEVICE"
AUDIO_CAPTURE_DEVICE_DEFAULT = "plughw:CARD=Device,DEV=0"
AUDIO_CAPTURE_GAIN_PERCENT = 100


def _extract_alsa_card_selector(audio_device: str) -> str:
    import re

    card_match = re.search(r"CARD=([^,]+)", audio_device)
    if card_match:
        return card_match.group(1)

    hw_match = re.match(r"(?:plug)?hw:(\d+)(?:,\d+)?", audio_device)
    if hw_match:
        return hw_match.group(1)

    return "Device"


def _set_audio_gain_on_startup() -> None:
    """Raise capture gain at startup so recordings are not too quiet.

    This mirrors the behavior previously embedded in bot.py.
    """
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


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN not set. Exiting.")
    print("Error: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)


class CamBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Preserve startup audio gain behavior from original bot.
        await asyncio.to_thread(_set_audio_gain_on_startup)

        # Start heart-rate background monitor if configured.
        try:
            heart_rate_cmd.bind_client(self)
            await heart_rate_cmd.start_background_monitor()
        except Exception:
            logger.exception("Failed to initialize heart-rate monitor")

        # Register command modules. Each module should expose register(tree, client).
        try:
            webcam_cmd.register(self.tree, self)
            delete_cmd.register(self.tree, self)
            ragebait_cmd.register(self.tree, self)
        except Exception:
            logger.exception("Failed to register command modules")

        # Sync global command tree on startup
        await self.tree.sync()
        logger.info("Command tree synced.")

    async def on_message(self, message: discord.Message) -> None:
        # Delegate per-message handling to ragebait_mo.handle_message
        try:
            await ragebait_cmd.handle_message(message)
        except Exception:
            # Keep exceptions from handler from crashing the bot
            logger.exception("Error in ragebait_mo.handle_message")


def main() -> None:
    client = CamBot()
    try:
        client.run(DISCORD_TOKEN)
    except Exception:
        logger.exception("Bot terminated unexpectedly.")
        sys.exit(1)


if __name__ == "__main__":
    main()
