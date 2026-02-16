Discord Webcam Snap Bot (Stage 1)

This project provides a minimal Discord bot that captures one image from the host webcam (/dev/video0) and sends it in response to the slash command /snap.

Requirements
- Docker (or Python 3.11+ if running locally)

Environment
- DISCORD_TOKEN: your bot token. Required.

Build and run with Docker

1. Build the image:
   docker build -t discord-cam-bot .

2. Run the container (ensure your host webcam is available at /dev/video0):
   docker run --rm -e DISCORD_TOKEN="$DISCORD_TOKEN" --device /dev/video0:/dev/video0 discord-cam-bot

OR use docker-compose (set DISCORD_TOKEN in your environment):
   docker compose up --build

Notes
- The bot registers a global slash command named /snap on startup. It may take a few minutes to appear in all guilds.
- The bot uses opencv-python-headless to capture a single frame and sends it as a JPEG with no caption.
- Errors (camera not available, failed capture, missing token) are sent as ephemeral messages to the invoking user.
