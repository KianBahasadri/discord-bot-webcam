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
import json
import asyncio
import logging
import sqlite3
import time
import subprocess
from pathlib import Path
from typing import Any, cast

import discord
from discord import app_commands
from aiohttp import web

# Import command modules. These modules expose register(tree, client).
from commands import palantir as palantir_cmd
from commands import delete as delete_cmd
from commands import ragebait_mo as ragebait_cmd
from commands import heart_rate as heart_rate_cmd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALLOWED_GUILD_IDS_ENV = "ALLOWED_GUILD_IDS"
UNAUTHORIZED_GUILD_MESSAGE = "This bot is restricted to authorized guilds only."
ENABLE_PALANTIR_COMMAND_ENV = "ENABLE_PALANTIR_COMMAND"
ENABLE_DELETE_COMMAND_ENV = "ENABLE_DELETE_COMMAND"
ENABLE_RAGEBAIT_MO_COMMAND_ENV = "ENABLE_RAGEBAIT_MO_COMMAND"


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
OWNTRACKS_PUBLIC_ENDPOINT_ENV = "OWNTRACKS_PUBLIC_ENDPOINT"
OWNTRACKS_BIND_HOST_ENV = "OWNTRACKS_BIND_HOST"
OWNTRACKS_BIND_PORT_ENV = "OWNTRACKS_BIND_PORT"
OWNTRACKS_DB_PATH_ENV = "OWNTRACKS_DB_PATH"
OWNTRACKS_QUEUE_MAXSIZE_ENV = "OWNTRACKS_QUEUE_MAXSIZE"
OWNTRACKS_BIND_HOST_DEFAULT = "127.0.0.1"
OWNTRACKS_BIND_PORT_DEFAULT = 8787
OWNTRACKS_DB_PATH_DEFAULT = "./opentracks.db"
OWNTRACKS_QUEUE_MAXSIZE_DEFAULT = 1000
OWNTRACKS_MAX_BODY_BYTES = 262144


def _load_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %d", name, raw, default)
        return default

    if value < minimum:
        logger.warning("%s must be >= %d, using default %d", name, minimum, default)
        return default
    return value


def _load_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default

    value = raw.lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False

    logger.warning("Invalid boolean for %s=%r, using default %s", name, raw, default)
    return default


class OwnTracksStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=DELETE;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS owntracks_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at INTEGER NOT NULL,
                    event_type TEXT,
                    user_id TEXT,
                    device_id TEXT,
                    tid TEXT,
                    topic TEXT,
                    tst INTEGER,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS owntracks_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at INTEGER NOT NULL,
                    user_id TEXT,
                    device_id TEXT,
                    tid TEXT,
                    topic TEXT,
                    tst INTEGER,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    acc REAL,
                    alt REAL,
                    vel REAL,
                    cog REAL,
                    batt REAL
                )
                """
            )
            conn.commit()

    def persist_event(self, item: dict[str, Any]) -> None:
        payload = item["payload"]
        event_type = str(payload.get("_type") or "")
        user_id = item.get("user_id")
        device_id = item.get("device_id")
        topic = payload.get("topic")
        tid = payload.get("tid")
        tst = _coerce_int(payload.get("tst"))
        received_at = int(item["received_at"])

        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO owntracks_events (
                    received_at, event_type, user_id, device_id, tid, topic, tst, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (received_at, event_type, user_id, device_id, tid, topic, tst, payload_json),
            )

            if event_type == "location":
                lat = _coerce_float(payload.get("lat"))
                lon = _coerce_float(payload.get("lon"))
                if lat is not None and lon is not None:
                    is_duplicate = False
                    if tst is not None:
                        existing = conn.execute(
                            """
                            SELECT 1
                            FROM owntracks_points
                            WHERE tst = ?
                              AND lat = ?
                              AND lon = ?
                              AND ifnull(device_id, '') = ifnull(?, '')
                              AND ifnull(tid, '') = ifnull(?, '')
                            LIMIT 1
                            """,
                            (tst, lat, lon, device_id, tid),
                        ).fetchone()
                        is_duplicate = existing is not None

                    if not is_duplicate:
                        conn.execute(
                            """
                            INSERT INTO owntracks_points (
                                received_at, user_id, device_id, tid, topic, tst,
                                lat, lon, acc, alt, vel, cog, batt
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                received_at,
                                user_id,
                                device_id,
                                tid,
                                topic,
                                tst,
                                lat,
                                lon,
                                _coerce_float(payload.get("acc")),
                                _coerce_float(payload.get("alt")),
                                _coerce_float(payload.get("vel")),
                                _coerce_float(payload.get("cog")),
                                _coerce_float(payload.get("batt")),
                            ),
                        )

            conn.commit()


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


class OwnTracksIngestServer:
    def __init__(self, host: str, port: int, db_path: Path, queue_maxsize: int) -> None:
        self._host = host
        self._port = port
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_maxsize)
        self._store = OwnTracksStore(db_path)
        self._worker: asyncio.Task[None] | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None
        self._app = web.Application(client_max_size=OWNTRACKS_MAX_BODY_BYTES)
        self._app.router.add_get("/healthz", self._healthz)
        self._app.router.add_post("/{tail:.*}", self._ingest_trackpoint)

    async def start(self) -> None:
        await asyncio.to_thread(self._store.initialize)
        self._worker = asyncio.create_task(self._run_worker(), name="owntracks-persist-worker")
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()
        logger.info("OwnTracks ingest listening on http://%s:%d (POST any path)", self._host, self._port)

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _healthz(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _ingest_trackpoint(self, request: web.Request) -> web.Response:
        if request.content_length == 0:
            return web.Response(status=204)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "invalid_payload"}, status=400)

        item = {
            "received_at": int(time.time()),
            "payload": payload,
            "user_id": request.headers.get("X-Limit-U"),
            "device_id": request.headers.get("X-Limit-D"),
        }

        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("OwnTracks event queue full; dropping event")
            return web.json_response({"error": "busy"}, status=503)

        return web.Response(status=204)

    async def _run_worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await asyncio.to_thread(self._store.persist_event, item)
            except Exception:
                logger.exception("Failed to persist OwnTracks event")
            finally:
                self._queue.task_done()


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
        self.owntracks_server: OwnTracksIngestServer | None = None

    async def setup_hook(self) -> None:
        # Preserve startup audio gain behavior from original bot.
        await asyncio.to_thread(_set_audio_gain_on_startup)

        if os.environ.get(OWNTRACKS_PUBLIC_ENDPOINT_ENV, "").strip():
            logger.info("Ignoring %s; Cloudflare strips public token path before forwarding to origin", OWNTRACKS_PUBLIC_ENDPOINT_ENV)

        host = os.environ.get(OWNTRACKS_BIND_HOST_ENV, OWNTRACKS_BIND_HOST_DEFAULT).strip() or OWNTRACKS_BIND_HOST_DEFAULT
        port = _load_int_env(OWNTRACKS_BIND_PORT_ENV, OWNTRACKS_BIND_PORT_DEFAULT)
        queue_maxsize = _load_int_env(OWNTRACKS_QUEUE_MAXSIZE_ENV, OWNTRACKS_QUEUE_MAXSIZE_DEFAULT)
        db_path = Path(os.environ.get(OWNTRACKS_DB_PATH_ENV, OWNTRACKS_DB_PATH_DEFAULT)).expanduser()
        self.owntracks_server = OwnTracksIngestServer(host=host, port=port, db_path=db_path, queue_maxsize=queue_maxsize)
        await self.owntracks_server.start()

        # Start heart-rate background monitor if configured.
        try:
            heart_rate_cmd.bind_client(self)
            await heart_rate_cmd.start_background_monitor()
        except Exception:
            logger.exception("Failed to initialize heart-rate monitor")

        # Register command modules. Each module should expose register(tree, client).
        try:
            if _load_bool_env(ENABLE_PALANTIR_COMMAND_ENV, True):
                palantir_cmd.register(self.tree, self, allowed_guilds=ALLOWED_GUILDS)
            else:
                logger.info("Skipping /palantir registration; %s disabled", ENABLE_PALANTIR_COMMAND_ENV)

            if _load_bool_env(ENABLE_DELETE_COMMAND_ENV, True):
                delete_cmd.register(self.tree, self, allowed_guilds=ALLOWED_GUILDS)
            else:
                logger.info("Skipping /delete registration; %s disabled", ENABLE_DELETE_COMMAND_ENV)

            if _load_bool_env(ENABLE_RAGEBAIT_MO_COMMAND_ENV, True):
                ragebait_cmd.register(self.tree, self, allowed_guilds=ALLOWED_GUILDS)
            else:
                logger.info("Skipping /ragebait-mo registration; %s disabled", ENABLE_RAGEBAIT_MO_COMMAND_ENV)
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

    async def close(self) -> None:
        if self.owntracks_server is not None:
            try:
                await self.owntracks_server.stop()
            except Exception:
                logger.exception("Failed to stop OwnTracks ingest server cleanly")
            finally:
                self.owntracks_server = None
        await super().close()


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
