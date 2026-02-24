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
from typing import Any, cast

import discord
from discord import app_commands

# Import command modules. These modules expose register(tree, client).
from commands import palantir as palantir_cmd
from commands import delete as delete_cmd
from commands import ragebait_mo as ragebait_cmd
from commands import heart_rate as heart_rate_cmd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_GUILD_IDS_ENV = "ALLOWED_GUILD_IDS"
UNAUTHORIZED_GUILD_MESSAGE = "This bot is restricted to authorized guilds only."


def _load_allowed_guild_ids() -> set[int]:
    raw = os.environ.get(ALLOWED_GUILD_IDS_ENV, "")
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        logger.error("%s must be set to a comma-separated list of Discord guild IDs.", ALLOWED_GUILD_IDS_ENV)
        print(f"Error: {ALLOWED_GUILD_IDS_ENV} environment variable not set.", file=sys.stderr)
        sys.exit(1)

    guild_ids: set[int] = set()
    for part in parts:
        if not part.isdigit():
            logger.error("Invalid guild id '%s' in %s; expected numeric snowflakes.", part, ALLOWED_GUILD_IDS_ENV)
            print(f"Error: invalid guild id '{part}' in {ALLOWED_GUILD_IDS_ENV}.", file=sys.stderr)
            sys.exit(1)
        guild_ids.add(int(part))

    return guild_ids


ALLOWED_GUILD_IDS = _load_allowed_guild_ids()
ALLOWED_GUILDS = tuple(discord.Object(id=guild_id) for guild_id in ALLOWED_GUILD_IDS)

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
assert DISCORD_TOKEN is not None


class CamBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = RestrictedCommandTree(self)

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
            palantir_cmd.register(self.tree, self, allowed_guilds=ALLOWED_GUILDS)
            delete_cmd.register(self.tree, self, allowed_guilds=ALLOWED_GUILDS)
            ragebait_cmd.register(self.tree, self, allowed_guilds=ALLOWED_GUILDS)
        except Exception:
            logger.exception("Failed to register command modules")

        # Copy registered global commands into authorized guild scopes.
        for guild in ALLOWED_GUILDS:
            self.tree.copy_global_to(guild=guild)

        # Ensure commands are only visible in authorized guilds.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        for guild in ALLOWED_GUILDS:
            await self.tree.sync(guild=guild)
        logger.info("Command tree synced to allowed guilds: %s", sorted(ALLOWED_GUILD_IDS))

    async def on_message(self, message: discord.Message) -> None:
        if not _is_allowed_guild_id(getattr(getattr(message, "guild", None), "id", None)):
            return

        # Delegate per-message handling to ragebait_mo.handle_message
        try:
            await ragebait_cmd.handle_message(message)
        except Exception:
            # Keep exceptions from handler from crashing the bot
            logger.exception("Error in ragebait_mo.handle_message")


def _is_allowed_guild_id(guild_id: int | None) -> bool:
    return guild_id in ALLOWED_GUILD_IDS


class RestrictedCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_allowed_guild_id(getattr(interaction, "guild_id", None)):
            return True

        try:
            if interaction.response.is_done():
                await interaction.followup.send(UNAUTHORIZED_GUILD_MESSAGE, ephemeral=True)
            else:
                await interaction.response.send_message(UNAUTHORIZED_GUILD_MESSAGE, ephemeral=True)
        except Exception:
            logger.warning("Failed to send unauthorized guild response for interaction %s", interaction.id)
        return False


def main() -> None:
    client = CamBot()
    try:
        client.run(cast(str, DISCORD_TOKEN))
    except Exception:
        logger.exception("Bot terminated unexpectedly.")
        sys.exit(1)


if __name__ == "__main__":
    main()
