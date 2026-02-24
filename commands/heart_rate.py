#!/usr/bin/env python3
"""Background BLE heart-rate monitor and /palantir message helper."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Optional

import discord

BleakClient: Any = None
BleakScanner: Any = None
try:
    bleak_module = importlib.import_module("bleak")
    BleakClient = getattr(bleak_module, "BleakClient", None)
    BleakScanner = getattr(bleak_module, "BleakScanner", None)
except Exception:
    BleakClient = None
    BleakScanner = None


logger = logging.getLogger(__name__)

HEART_RATE_DEVICE_ADDRESS_ENV = "HEART_RATE_DEVICE_ADDRESS"

HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

HEART_RATE_SCAN_TIMEOUT_SECONDS = 8.0
HEART_RATE_CONNECT_TIMEOUT_SECONDS = 10.0
HEART_RATE_MAX_STALE_SECONDS = 20.0
HEART_RATE_RECONNECT_BASE_SECONDS = 1.0
HEART_RATE_RECONNECT_MAX_SECONDS = 8.0
HEART_RATE_MESSAGE_WAIT_SECONDS = 6.0

HEART_RATE_EMOJI = "\u2764\ufe0f"


@dataclass
class _HeartRateState:
    bpm: Optional[int] = None
    measured_monotonic: Optional[float] = None
    connected: bool = False
    device_name: Optional[str] = None
    device_address: Optional[str] = None
    last_error: Optional[str] = None


@dataclass(frozen=True)
class HeartRateSnapshot:
    bpm: Optional[int]
    age_seconds: Optional[int]
    connected: bool
    device_name: Optional[str]
    device_address: Optional[str]
    available: bool
    last_error: Optional[str]


_state = _HeartRateState()
_state_lock = Lock()
_monitor_task: Optional[asyncio.Task] = None
_client: Optional[discord.Client] = None


def bind_client(client: discord.Client) -> None:
    """Bind Discord client so heart-rate helpers can resolve channels."""
    global _client
    _client = client


def is_enabled() -> bool:
    return bool(os.environ.get(HEART_RATE_DEVICE_ADDRESS_ENV, "").strip())


def _normalize_address(address: str) -> str:
    return address.strip().lower()


def _parse_heart_rate_measurement(data: bytearray) -> Optional[int]:
    if not data:
        return None

    flags = data[0]
    is_16_bit = bool(flags & 0x01)
    if is_16_bit:
        if len(data) < 3:
            return None
        return int.from_bytes(data[1:3], "little")

    if len(data) < 2:
        return None
    return int(data[1])


def _update_connection_state(*, connected: bool, error: Optional[str] = None) -> None:
    with _state_lock:
        _state.connected = connected
        _state.last_error = error


def _record_heart_rate(*, bpm: int, device_name: Optional[str], device_address: Optional[str]) -> None:
    with _state_lock:
        _state.bpm = bpm
        _state.measured_monotonic = time.monotonic()
        _state.connected = True
        _state.device_name = device_name
        _state.device_address = device_address
        _state.last_error = None


def _format_exception(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def get_snapshot() -> HeartRateSnapshot:
    with _state_lock:
        bpm = _state.bpm
        measured_monotonic = _state.measured_monotonic
        connected = _state.connected
        device_name = _state.device_name
        device_address = _state.device_address
        last_error = _state.last_error

    age_seconds: Optional[int]
    if measured_monotonic is None:
        age_seconds = None
    else:
        age_seconds = max(0, int(time.monotonic() - measured_monotonic))

    available = bpm is not None and age_seconds is not None and age_seconds <= int(HEART_RATE_MAX_STALE_SECONDS)
    return HeartRateSnapshot(
        bpm=bpm,
        age_seconds=age_seconds,
        connected=connected,
        device_name=device_name,
        device_address=device_address,
        available=available,
        last_error=last_error,
    )


def format_palantir_message() -> str:
    snapshot = get_snapshot()
    if snapshot.available and snapshot.bpm is not None:
        return f"{HEART_RATE_EMOJI} Heart rate: {snapshot.bpm} bpm"

    if snapshot.age_seconds is None:
        if snapshot.connected:
            return f"{HEART_RATE_EMOJI} Heart rate: unavailable (waiting for first reading)"
        return f"{HEART_RATE_EMOJI} Heart rate: unavailable (monitor disconnected)"

    return f"{HEART_RATE_EMOJI} Heart rate: unavailable (last update {snapshot.age_seconds}s ago)"


async def _scan_for_target_device(target_address: str):
    if BleakScanner is None:
        raise RuntimeError("Bleak scanner is unavailable.")

    discovered = await BleakScanner.discover(timeout=HEART_RATE_SCAN_TIMEOUT_SECONDS, return_adv=True)
    target_address_normalized = _normalize_address(target_address)

    for found_address, (device, adv) in discovered.items():
        if _normalize_address(found_address) != target_address_normalized:
            continue

        service_uuids = [uuid.lower() for uuid in (adv.service_uuids or [])]
        if HEART_RATE_SERVICE_UUID not in service_uuids:
            logger.debug(
                "Heart-rate target matched by address but did not advertise HR service in this scan cycle: %s",
                found_address,
            )
        return device

    return None


async def _connect_and_stream(target: Any) -> None:
    if BleakClient is None:
        raise RuntimeError("Bleak client is unavailable.")

    disconnected_event = asyncio.Event()

    def on_disconnect(_: Any) -> None:
        disconnected_event.set()

    device_name = getattr(target, "name", None)
    device_address = getattr(target, "address", None)
    if isinstance(target, str):
        device_address = target

    async with BleakClient(
        target,
        timeout=HEART_RATE_CONNECT_TIMEOUT_SECONDS,
        disconnected_callback=on_disconnect,
    ) as client:
        _update_connection_state(connected=True, error=None)

        def on_notify(_: Any, data: bytearray) -> None:
            bpm = _parse_heart_rate_measurement(data)
            if bpm is None:
                return
            _record_heart_rate(bpm=bpm, device_name=device_name, device_address=device_address)

        await client.start_notify(HEART_RATE_MEASUREMENT_UUID, on_notify)
        logger.info(
            "Heart-rate monitor connected: %s [%s].",
            device_name or "Unknown",
            device_address or "unknown-address",
        )

        try:
            await disconnected_event.wait()
        finally:
            with contextlib.suppress(Exception):
                await client.stop_notify(HEART_RATE_MEASUREMENT_UUID)


async def _monitor_loop(target_address: str) -> None:
    retry_delay = HEART_RATE_RECONNECT_BASE_SECONDS
    while True:
        try:
            device = await _scan_for_target_device(target_address)
            target = device if device is not None else target_address
            retry_delay = HEART_RATE_RECONNECT_BASE_SECONDS
            await _connect_and_stream(target)
            _update_connection_state(connected=False, error="Heart-rate monitor disconnected.")

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, HEART_RATE_RECONNECT_MAX_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_message = _format_exception(exc)
            _update_connection_state(connected=False, error=error_message)
            logger.warning("Heart-rate monitor loop error: %s", error_message)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, HEART_RATE_RECONNECT_MAX_SECONDS)


async def _wait_for_recent_reading(max_wait_s: float) -> None:
    if max_wait_s <= 0:
        return

    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        snapshot = get_snapshot()
        if snapshot.available:
            return
        await asyncio.sleep(0.5)


async def start_background_monitor() -> None:
    """Start a single background heart-rate monitor task if configured."""
    global _monitor_task

    if _monitor_task is not None and not _monitor_task.done():
        return

    if BleakClient is None or BleakScanner is None:
        _update_connection_state(connected=False, error="Bleak import failed.")
        logger.warning("Heart-rate monitor disabled: failed to import bleak.")
        return

    if not is_enabled():
        _update_connection_state(
            connected=False,
            error=f"Set {HEART_RATE_DEVICE_ADDRESS_ENV} to enable heart-rate monitoring.",
        )
        logger.info(
            "Heart-rate monitor disabled: %s is not set.",
            HEART_RATE_DEVICE_ADDRESS_ENV,
        )
        return

    target_address = os.environ.get(HEART_RATE_DEVICE_ADDRESS_ENV, "").strip()

    _monitor_task = asyncio.create_task(
        _monitor_loop(target_address),
        name="heart-rate-monitor",
    )
    logger.info("Heart-rate monitor background task started for %s.", target_address)


async def send_palantir_heart_rate(channel_id: int) -> None:
    """Send a standalone heart-rate message to the channel used by /palantir."""
    channel: Any = None
    try:
        if not is_enabled():
            return

        if _client is None:
            logger.warning("Heart-rate helper: no Discord client bound for channel %s.", channel_id)
            return

        channel = _client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await _client.fetch_channel(channel_id)
            except Exception:
                channel = None

        if channel is None:
            logger.warning("Heart-rate helper: failed to resolve channel %s.", channel_id)
            return

        await _wait_for_recent_reading(HEART_RATE_MESSAGE_WAIT_SECONDS)
        await channel.send(format_palantir_message())
    except discord.Forbidden:
        logger.warning("Heart-rate helper: missing permission to post in channel %s.", channel_id)
    except Exception as exc:
        logger.exception("Heart-rate helper failed for channel %s: %s", channel_id, exc)
