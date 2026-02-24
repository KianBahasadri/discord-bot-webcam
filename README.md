Discord Webcam Snap Bot (Stage 1)

This project provides a Discord bot that captures from multiple sources and sends them in response to the slash command /palantir: local webcam (/dev/video0), optional remote webcam over SSH, and HDMI capture (/dev/video2).

Requirements
- Docker (or Python 3.11+ if running locally)

Environment
- Create a `.env` file in the project root with at least:
  `DISCORD_TOKEN=your_bot_token_here`
  `ALLOWED_GUILD_IDS=1300638017731563613,1151470261145702400` (required: comma-separated guild IDs)
  `CLOUDFLARED_TUNNEL_TOKEN=...` (optional; enables `cloudflared` sidecar in compose)
- For OpenRouter-backed features (`/palantir` redaction and `/ragebait-mo`), also set:
  `OPENROUTER_API_KEY`
- Optional shared OpenRouter headers:
  `OPENROUTER_SITE_URL` (default `http://localhost`),
  `OPENROUTER_APP_NAME` (default is feature-specific if unset)
 - Optional Linear integration (audience-facing roast/recap shown once per /palantir):
   `LINEAR_API_KEY` (personal Linear API key; required to enable summaries)
   `LINEAR_SUMMARY_ENABLED` (default `true`; set to `false` to disable the summary)
   `LINEAR_STATUSES` (comma-separated status names; default `In Progress,Todo`)
   `LINEAR_LOOKAHEAD_DAYS` (integer days to look ahead for upcoming due dates; default `3`)
- Optional `/palantir` capture/audio tuning env vars:
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
  `HEART_RATE_DEVICE_ADDRESS` (for `/palantir` heart-rate side message; example `F8:AF:75:80:4A:48`)
- Optional OwnTracks ingest env vars:
  `OWNTRACKS_BIND_HOST` (default `127.0.0.1`; compose overrides to `0.0.0.0` for sidecar access),
  `OWNTRACKS_BIND_PORT` (default `8787`),
  `OWNTRACKS_DB_PATH` (default `./opentracks.db`)
- Optional reminder-only var:
  `OWNTRACKS_PUBLIC_ENDPOINT` (ignored by bot; useful as a note for your tokenized public URI)
- Required `/palantir` redaction model env var:
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

2. Run the container (ensure your host devices are available at /dev/video0 and /dev/video2; include /dev/snd for `/palantir` audio capture, mount host D-Bus socket for BLE heart-rate access, and mount `./mo_beliefs.json` to persist Mo belief storage):
   docker run --rm --env-file .env --device /dev/video0:/dev/video0 --device /dev/video2:/dev/video2 --device /dev/snd:/dev/snd -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket -v "$(pwd)/mo_beliefs.json:/app/mo_beliefs.json" discord-cam-bot

OR use docker-compose (it injects variables from `.env` into the container). The included docker-compose file maps /dev/video0, /dev/video2, /dev/snd, `/run/dbus/system_bus_socket`, and `./mo_beliefs.json:/app/mo_beliefs.json`:
   docker compose up --build

OwnTracks ingest

- The bot starts an aiohttp server with:
  - `GET /healthz`
  - `POST` on any path (so Cloudflare can forward tokenized paths without origin path rewrite)
- `OWNTRACKS_PUBLIC_ENDPOINT` is intentionally ignored in app code; cloudflared/Cloudflare handles public URI routing.
- The endpoint accepts intermittent OwnTracks HTTP payloads, ignores zero-length bodies with `204`, queues valid JSON payloads, and persists events to SQLite.
- Location payloads (`_type=location`) are normalized into `owntracks_points`; all payloads are stored in `owntracks_events` in `opentracks.db`.
- In Docker Compose, `cloudflared` runs as a sidecar and the bot binds to `0.0.0.0:8787` on the internal network only (no host port published).

Notes
- The bot registers slash commands only in guilds listed by `ALLOWED_GUILD_IDS`.
- The bot uses opencv-python-headless to capture a single frame and sends it as a JPEG with no caption.
- `/palantir` captures from `/dev/video0` (laptop webcam), attempts a remote webcam capture over SSH, and captures from `/dev/video2` (HDMI capture card).
- `/palantir` also sends a standalone heart-rate message (for example `❤️ Heart rate: 72 bpm`) when `HEART_RATE_DEVICE_ADDRESS` is configured.
 - `/palantir` now also optionally posts a short Linear status recap to the same channel when `LINEAR_API_KEY` is configured. This summary is audience-facing (for friends) and intentionally stylized — it may include a frustrated tone and light profanity when numeric facts indicate poor performance. Disable with `LINEAR_SUMMARY_ENABLED=false`.
- `/palantir` redacts each captured image before upload by blurring detected explicit content and visible PII (for example: phone numbers, API keys/tokens, and credit card numbers).
- `/ragebait-mo` runs in-channel multi-turn chat mode using OpenRouter and stops when the model marks the conversation off-topic.
- `/ragebait-mo` loads Mo beliefs from `mo_beliefs.json` (host-mounted). The model can emit optional `belief_updates` in JSON output; new beliefs are appended to this file automatically.
- Errors (camera not available, failed capture, missing token) are sent as ephemeral messages to the invoking user.
