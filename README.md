Discord Webcam Snap Bot (Stage 1)

This project provides a minimal Discord bot that captures one image from the host webcam (/dev/video0) and sends it in response to the slash command /webcam.

Requirements
- Docker (or Python 3.11+ if running locally)

Environment
- Create a `.env` file in the project root with:
  `DISCORD_TOKEN=your_bot_token_here`

Build and run with Docker

1. Build the image:
   docker build -t discord-cam-bot .

2. Run the container (ensure your host webcam is available at /dev/video0):
   docker run --rm --env-file .env --device /dev/video0:/dev/video0 discord-cam-bot

OR use docker-compose (it loads `.env` automatically):
   docker compose up --build

Notes
- The bot registers a global slash command named /webcam on startup. It may take a few minutes to appear in all guilds.
- The bot uses opencv-python-headless to capture a single frame and sends it as a JPEG with no caption.
- Errors (camera not available, failed capture, missing token) are sent as ephemeral messages to the invoking user.
