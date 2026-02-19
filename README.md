Discord Webcam Snap Bot (Stage 1)

This project provides a Discord bot that captures from multiple sources and sends them in response to the slash command /webcam: local webcam (/dev/video0), optional remote webcam over SSH, and HDMI capture (/dev/video2).

Requirements
- Docker (or Python 3.11+ if running locally)

Environment
- Create a `.env` file in the project root with at least:
  `DISCORD_TOKEN=your_bot_token_here`
- For `/webcam` redaction, also set:
  `OPENROUTER_API_KEY`
- Optional `/webcam` redaction tuning env vars:
  `OPENROUTER_REDACTION_MODEL` (default `google/gemini-2.5-flash`; falls back to `OPENROUTER_MODEL`),
  `OPENROUTER_SITE_URL` (default `http://localhost`),
  `OPENROUTER_APP_NAME` (default `discord-webcam-redaction`),
  `WEBCAM_REDACTION_TIMEOUT_SECONDS` (default `30`),
  `WEBCAM_REDACTION_MAX_SIDE` (default `1280`),
  `WEBCAM_REDACTION_PADDING_RATIO` (default `0.08`),
  `WEBCAM_REDACTION_MIN_BOX_SIDE` (default `12`),
  `WEBCAM_REDACTION_PROTECT_TIMESTAMP` (default `true`; ignores bottom-right timestamp strip),
  `WEBCAM_REDACTION_FAIL_CLOSED` (default `true`; if enabled, full-frame blur is used on model failure)
- For `/ragebait-mo`, also set:
  `MO_USER_ID`, `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID`, `ELEVENLABS_AGENT_PHONE_NUMBER_ID`, `MO_CELL_NUMBER`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`

  Note: MO_CELL_NUMBER is used as the default destination number for the ElevenLabs/Twilio call. You may optionally override the destination per-invocation by passing a phone argument to the `/ragebait-mo` command (E.164 format like +15555555555).

Build and run with Docker

1. Build the image:
   docker build -t discord-cam-bot .

2. Run the container (ensure your host devices are available at /dev/video0 and /dev/video2):
   docker run --rm --env-file .env --device /dev/video0:/dev/video0 --device /dev/video2:/dev/video2 discord-cam-bot

OR use docker-compose (it injects variables from `.env` into the container). The included docker-compose file also maps /dev/video0 and /dev/video2 into the container:
   docker compose up --build

Notes
- The bot registers global slash commands on startup (`/webcam`, `/ragebait-mo`). It may take a few minutes to appear in all guilds.
- The bot uses opencv-python-headless to capture a single frame and sends it as a JPEG with no caption.
- `/webcam` captures from `/dev/video0` (laptop webcam), attempts a remote webcam capture over SSH, and captures from `/dev/video2` (HDMI capture card).
- `/webcam` redacts each captured image before upload by blurring detected explicit content and visible PII (for example: phone numbers, API keys/tokens, and credit card numbers).
- Errors (camera not available, failed capture, missing token) are sent as ephemeral messages to the invoking user.
