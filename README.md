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
- Maximum 5 active API tokens per Telegram account; 30 requests/minute by default. Free tier: 2 requests/day. Paid allowances: 50 / 500 / 2,500 requests per daily / weekly / monthly plan.
- Unlimited API access can be unlocked using the server-side `PANDA_UNLIMITED_CODE`.
- Persistent SQLite token/account database stored on Railway volume `/data`.
- Private Android REST API. Fish Audio credentials remain server-side.
- Interactive Swagger API documentation at `/docs`; newly created tokens display Voice and AI Chat endpoints with ready-to-use examples.
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
| `PANDA_DAILY_REQUEST_LIMIT` | Free account requests per UTC day | `2` |
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

Each Telegram user can create at most 5 active API tokens from Developer Center. Tokens have 7, 30, 90 or 365 day expiry; unlimited users can choose never-expiring tokens. Tokens can be revoked in the bot. Token secrets are stored as SHA-256 hashes and shown only once.

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


## AI Chat (OpenRouter)

PANDA VOICE AI includes a persistent AI assistant available from the Telegram menu and the authenticated REST API. The default model is `z-ai/glm-5.3-flash`; reasoning is enabled by default. OpenRouter's opaque `reasoning_details` value is stored and passed back unchanged on follow-up turns when the provider returns it. It is never exposed in API responses or Telegram messages.

Configure these Railway variables (never commit the API key):

| Variable | Required | Default |
|---|---:|---|
| `OPENROUTER_API_KEY` | Yes, for AI Chat | — |
| `OPENROUTER_MODEL` | No | `z-ai/glm-5.3-flash` |
| `OPENROUTER_BASE_URL` | No | `https://openrouter.ai/api/v1` |
| `OPENROUTER_REASONING_ENABLED` | No | `true` |
| `MAX_AI_CHAT_CHARS` | No | `6000` |

### Telegram

- Tap **AI Chat** in the main menu or send `/chat`.
- Send messages normally; the conversation continues with stored context.
- Send `/newchat` to start a fresh conversation.
- Send `/chatex` to leave AI Chat and return to the main menu.

### AI Chat REST endpoints

All endpoints require `X-API-Key`. Sessions are private to the API token owner and stored in the existing persistent SQLite volume.

#### Send a message

`POST /api/v1/chat`

Request:
```json
{
  "message": "Explain how text-to-speech works.",
  "session_id": null
}
```

Response:
```json
{
  "session_id": "returned-session-uuid",
  "reply": "Text-to-speech converts written text into spoken audio...",
  "model": "z-ai/glm-5.3-flash",
  "usage": {}
}
```

Pass the returned `session_id` in the next request to continue the same conversation. Omit it or send `null` to start a new session.

Python example:
```python
import requests

base_url = "https://panda-voice-ai-production.up.railway.app"
headers = {
    "X-API-Key": "YOUR_PANDA_API_TOKEN",
    "Content-Type": "application/json",
}
payload = {
    "message": "Hello! Please introduce yourself.",
    "session_id": None,
}
response = requests.post(
    f"{base_url}/api/v1/chat",
    headers=headers,
    json=payload,
    timeout=120,
)
response.raise_for_status()
data = response.json()
print(data["reply"])
session_id = data["session_id"]

# Continue the same conversation:
follow_up = requests.post(
    f"{base_url}/api/v1/chat",
    headers=headers,
    json={"message": "Can you summarize that?", "session_id": session_id},
    timeout=120,
)
follow_up.raise_for_status()
print(follow_up.json()["reply"])
```

#### List and delete sessions

- `GET /api/v1/chat/sessions?limit=20` — list the authenticated owner's recent sessions.
- `DELETE /api/v1/chat/sessions/{session_id}` — delete one of the authenticated owner's sessions.

The service does not return private reasoning traces. It only returns the assistant's user-facing answer, model name and provider usage metadata.


## Shared quota, subscriptions and languages

- Telegram AI Chat, Telegram TTS and authenticated API Chat/TTS now use one shared per-account daily quota.
- Free tier allows 2 combined requests per UTC day.
- Paid plan allowances: Daily 50 requests / 1 day, Weekly 500 / 7 days, Monthly 2,500 / 30 days.
- Daily, weekly and monthly plans use Telegram Stars invoices. Configure their integer Star prices with `PANDA_PLAN_DAILY_STARS`, `PANDA_PLAN_WEEKLY_STARS` and `PANDA_PLAN_MONTHLY_STARS`. Default Stars prices are Daily 100, Weekly 399, Monthly 700.
- Payments are one-time purchases with manual renewal; automatic recurring billing is not enabled.
- AI Chat supports English, Arabic, Spanish and Classical Latin response modes.
- Added `POST /api/v1/chat/completions`, `GET /api/v1/plans` and `GET /api/v1/subscription`.

See [API.md](API.md) for the complete endpoint reference, request examples, quota rules and payment lifecycle.


## Developer Center and token management

Creating an API token displays the Base URL, all Voice and AI Chat endpoints, Swagger docs and a copy-ready AI Chat request. Tokens can be deleted in Telegram or with `DELETE /api/v1/tokens/{token_id}`. Token secrets are displayed only once.

Telegram's native command menu includes `/start`, `/menu`, `/chat`, `/newchat`, `/chatex`, `/plans`, `/language`, `/settings`, `/developers`, `/help` and `/cancel`.

Free users can make 2 combined AI Chat + Voice requests per UTC day and change style/model twice per day. Paid subscribers can switch styles and speech models freely.
