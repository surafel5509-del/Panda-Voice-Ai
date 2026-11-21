# PANDA VOICE AI 2.0

Professional Telegram text-to-speech bot and private Android-facing REST API powered by Fish Audio.

## Features

- English-only Telegram UI with a clean inline-keyboard workflow.
- Live public Fish Audio voice catalog, split into female, male and all categories using the catalog's actual tags.
- Preview each voice before selecting it.
- 12 S2 natural-language delivery styles; preview the selected style with the selected voice.
- Automatic style-aware voice ranking using the voice catalog's own tags.
- Speed control (0.5x–2.0x).
- MP3 playback and download in Telegram.
- Developer menu with per-user API token creation, expiry, revocation and usage reporting.
- Maximum 2 active API tokens per Telegram account; default 100 requests/day and 30 requests/minute.
- Unlimited API access can be unlocked using the server-side `PANDA_UNLIMITED_CODE`.
- Persistent SQLite token/account database stored on Railway volume `/data`.
- Private Android REST API. Fish Audio credentials remain server-side.
- No public Swagger page; API routes are not displayed in the Telegram bot.
- HTTP request logging is suppressed to prevent Telegram bot tokens appearing in request URLs.

## Fish Audio implementation

- TTS endpoint: `POST https://api.fish.audio/v1/tts`
- Model header: `s2.1-pro-free` by default.
- Voice selection: `reference_id` from Fish Audio's public voice model catalog.
- S2 emotion/delivery control: natural-language `[bracket]` cues inserted into the text.
- Numeric prosody: `prosody.speed` and `prosody.volume`.
- The app does not pretend that a free-text style label is a separate Fish API parameter.

The Fish Audio API model `s2.1-pro-free` is separate from the consumer web subscription's Free plan. API access, terms, limits and pricing may change; verify current developer pricing before production use.

## Railway environment variables

| Variable | Required | Default |
|---|---:|---|
| `BOT_TOKEN` or `TELEGRAM_BOT_TOKEN` | For Telegram bot | — |
| `FISH_AUDIO_API` or `FISH_API_KEY` | Yes | — |
| `PANDA_API_KEY` | Optional legacy master API key | — |
| `PANDA_UNLIMITED_CODE` | Secret code to unlock per-user unlimited access | — |
| `PANDA_DAILY_REQUEST_LIMIT` | Daily request limit per token | `100` |
| `PANDA_DATABASE_PATH` | Persistent SQLite path | `/data/panda.sqlite3` |
| `FISH_MODEL` | No | `s2.1-pro-free` |
| `MAX_TEXT_CHARS` | No | `4000` |
| `LOG_LEVEL` | No | `INFO` |

Never put Fish Audio or Telegram secrets in the Android app or GitHub.

## REST API

All app endpoints require the `X-API-Key` header.

### List voices

`GET /api/v1/voices?gender=female&style=story`

Gender: `all`, `female`, `male`. Style is optional and can be any supported style key.

### Generate MP3

`POST /api/v1/tts`

Headers:
- `X-API-Key: <your PANDA_API_KEY>`
- `Content-Type: application/json`

JSON body:
```json
{
  "text": "Hello from my Android app.",
  "voice_id": "FISH_AUDIO_VOICE_MODEL_ID",
  "style": "natural",
  "speed": 1.0,
  "volume": 0.0
}
```

Success: raw `audio/mpeg` bytes (MP3), not JSON.

Supported style keys: `natural`, `story`, `podcast`, `voiceover`, `audiobook`, `news`, `documentary`, `meditation`, `energetic`, `whisper`, `empathetic`, `commercial`.

### Additional endpoints

- `GET /api/v1/voices/{voice_id}` — voice detail.
- `GET /api/v1/account` — current token access and daily usage.
- `GET /api/v1/limits` — request limits and text size.
- `GET /api/v1/styles` and `GET /api/v1/engines` — catalogs.

Each Telegram user can create at most 2 active API tokens from Developers. Tokens have 7, 30, 90 or 365 day expiry; unlimited users can choose never-expiring tokens. Tokens can be revoked in the bot. Token secrets are stored as SHA-256 hashes and shown only once.

### Health check

`GET /health`

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="..."
export FISH_AUDIO_API="..."
export PANDA_API_KEY="..."
uvicorn main:app --reload --port 8000
```

Do not commit secrets. For Android production, call this backend from your app server where possible; a key embedded in an APK can be extracted. Use per-user authentication and quotas before opening this API to public users.
