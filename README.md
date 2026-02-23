Discord Webcam Snap Bot (Stage 1)

This project provides a Discord bot that captures from multiple sources and sends them in response to the slash command /webcam: local webcam (/dev/video0), optional remote webcam over SSH, and HDMI capture (/dev/video2).

Requirements
- Docker (or Python 3.11+ if running locally)

Environment
- Create a `.env` file in the project root with at least:
  `DISCORD_TOKEN=your_bot_token_here`
- For OpenRouter-backed features (`/webcam` redaction and `/ragebait-mo`), also set:
  `OPENROUTER_API_KEY`
- Optional shared OpenRouter headers:
  `OPENROUTER_SITE_URL` (default `http://localhost`),
  `OPENROUTER_APP_NAME` (default is feature-specific if unset)
- Optional `/webcam` capture/audio tuning env vars:
  `WEBCAM_AUDIO_DEVICE` (default `plughw:CARD=Device,DEV=0`),
  `WEBCAM_AUDIO_DURATION_SECONDS` (default `10`),
  `WEBCAM_CAPTURE_TIMEOUT_SECONDS` (default `20`),
  `REMOTE_WEBCAM_SSH_TARGET` (default `root@laptop3`),
  `REMOTE_WEBCAM_DEVICE` (default `/dev/video0`),
  `REMOTE_WEBCAM_OUTPUT` (default `/tmp/discord-remote-webcam.jpg`),
  `REMOTE_WEBCAM_TIMEOUT_SECONDS` (default `12`),
  `REMOTE_WEBCAM_WARMUP_FRAMES` (default `30`),
  `REMOTE_WEBCAM_WARMUP_FPS` (default `15`),
  `REMOTE_WEBCAM_SETTLE_SECONDS` (default `0.4`),
  `HEART_RATE_DEVICE_ADDRESS` (for `/webcam` heart-rate side message; example `F8:AF:75:80:4A:48`)
- Required `/webcam` redaction model env var:
  `OPENROUTER_REDACTION_MODEL` (required; example `google/gemini-2.5-flash`),
  `WEBCAM_REDACTION_TIMEOUT_SECONDS` (default `30`),
  `WEBCAM_REDACTION_MAX_SIDE` (default `1280`),
  `WEBCAM_REDACTION_PADDING_RATIO` (default `0.08`),
  `WEBCAM_REDACTION_MIN_BOX_SIDE` (default `12`),
  `WEBCAM_REDACTION_PROTECT_TIMESTAMP` (default `true`; ignores bottom-right timestamp strip),
  `WEBCAM_REDACTION_FAIL_CLOSED` (default `true`; if enabled, full-frame blur is used on model failure)
- Required `/ragebait-mo` model env var:
  `OPENROUTER_RAGEBAIT_MODEL` (required; example `x-ai/grok-4.1-fast`),
  `MO_USER_ID` (required unless `/ragebait-mo debug:true`; limits replies/history to Mo only)
- Optional `/ragebait-mo` tuning env vars:
  `RAGEBAIT_START_HISTORY_LIMIT` (default `60`),
  `RAGEBAIT_MAX_TURNS` (default `40`),
  `RAGEBAIT_MAX_DURATION_SECONDS` (default `1800`),
  `RAGEBAIT_IDLE_TIMEOUT_SECONDS` (default `300`),
  `RAGEBAIT_MO_BELIEFS_PATH` (default `/app/mo_beliefs.json`)

Build and run with Docker

1. Build the image:
   docker build -t discord-cam-bot .

2. Run the container (ensure your host devices are available at /dev/video0 and /dev/video2; include /dev/snd for `/webcam` audio capture, mount host D-Bus socket for BLE heart-rate access, and mount `./mo_beliefs.json` to persist Mo belief storage):
   docker run --rm --env-file .env --device /dev/video0:/dev/video0 --device /dev/video2:/dev/video2 --device /dev/snd:/dev/snd -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket -v "$(pwd)/mo_beliefs.json:/app/mo_beliefs.json" discord-cam-bot

OR use docker-compose (it injects variables from `.env` into the container). The included docker-compose file maps /dev/video0, /dev/video2, /dev/snd, `/run/dbus/system_bus_socket`, and `./mo_beliefs.json:/app/mo_beliefs.json`:
   docker compose up --build

Notes
- The bot registers global slash commands on startup (`/webcam`, `/ragebait-mo`). It may take a few minutes to appear in all guilds.
- The bot uses opencv-python-headless to capture a single frame and sends it as a JPEG with no caption.
- `/webcam` captures from `/dev/video0` (laptop webcam), attempts a remote webcam capture over SSH, and captures from `/dev/video2` (HDMI capture card).
- `/webcam` also sends a standalone heart-rate message (for example `❤️ Heart rate: 72 bpm`) when `HEART_RATE_DEVICE_ADDRESS` is configured.
- `/webcam` redacts each captured image before upload by blurring detected explicit content and visible PII (for example: phone numbers, API keys/tokens, and credit card numbers).
- `/ragebait-mo` runs in-channel multi-turn chat mode using OpenRouter and stops when the model marks the conversation off-topic.
- `/ragebait-mo` loads Mo beliefs from `mo_beliefs.json` (host-mounted). The model can emit optional `belief_updates` in JSON output; new beliefs are appended to this file automatically.
- Errors (camera not available, failed capture, missing token) are sent as ephemeral messages to the invoking user.
