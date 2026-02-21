"""
Command module for /delete.

Expose register(tree, client) to register the slash command. Helper
functions are private and behavior matches the original implementation
in bot.py (error keys and user-facing messages preserved).
"""
from typing import Any
import logging

import discord

logger = logging.getLogger(__name__)


async def _delete_most_recent_bot_message(
    channel: Any,
    client: discord.Client,
    skip_message_id: int | None = None,
) -> dict:
    """Delete the most recent message authored by the bot in the channel.

    Returns a dict with keys: deleted(bool), message_id(optional), error(optional).
    Error strings preserve the original semantics: "bot_not_ready", "forbidden",
    "none_found", or other error text.
    """
    try:
        if client.user is None:
            return {"deleted": False, "message_id": None, "error": "bot_not_ready"}
        # Iterate recent messages (newest first)
        async for msg in channel.history(limit=200):
            if skip_message_id is not None and getattr(msg, "id", None) == skip_message_id:
                continue

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


def register(tree: discord.app_commands.CommandTree, client: discord.Client) -> None:
    """Register the /delete slash command on the provided CommandTree.

    The registered handler uses the provided client for operations (so callers
    should pass the running discord.Client instance).
    """

    @tree.command(name="delete", description="Delete the most recent bot-authored message in this channel")
    async def delete(interaction: discord.Interaction):
        """Slash command handler: delete the most recent bot message in the same channel.

        Provides concise user-facing messages and handles permission errors.
        """
        # Keep acknowledgement public, but skip deleting that placeholder message.
        await interaction.response.defer()
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("Error: could not determine channel.")
            return

        original_response = await interaction.original_response()
        original_response_id = getattr(original_response, "id", None)

        result = await _delete_most_recent_bot_message(
            channel,
            client,
            skip_message_id=original_response_id,
        )
        if result.get("deleted"):
            await interaction.followup.send("Deleted most recent bot message.")
            return

        err = result.get("error") or "unknown"
        if err == "none_found":
            await interaction.followup.send("No recent bot message to delete.")
        elif err == "forbidden":
            await interaction.followup.send("I lack permission to delete messages in this channel.")
        else:
            await interaction.followup.send(f"Failed to delete bot message: {err}")
