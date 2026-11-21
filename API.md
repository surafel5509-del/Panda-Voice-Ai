# Panda Voice AI — API reference

Base URL: `https://panda-voice-ai-production.up.railway.app`

All application endpoints require an API token in `X-API-Key`, except `GET /health` and the Telegram webhook. Create and revoke per-user API tokens through the Telegram **Developers** menu. Treat tokens as passwords.

## Authentication and errors

```http
X-API-Key: panda_your_token
Content-Type: application/json
```

Errors use the standard JSON shape `{"detail":"..."}`. Common statuses: 401 invalid/expired key, 413 oversized message, 422 invalid input, 429 shared daily quota or rate limit reached, 502 upstream provider error, 503 missing provider configuration, 504 upstream timeout.

## Health

- `GET /health` — public liveness and database-presence check.

## Account and quota

- `GET /api/v1/account` — access tier, plan, shared daily usage, remaining requests and token expiry.
- `GET /api/v1/limits` — active-token cap, shared quota and rate limits.
- `GET /api/v1/plans` — Telegram Stars plan catalogue and configured prices.
- `GET /api/v1/subscription` — current subscription and expiry.

Free accounts share **10 requests per UTC day** across Telegram AI Chat, Telegram speech generation, and authenticated API Chat/TTS. API token count does not multiply the account quota. Paid subscriptions have the configured paid daily limit (default 100). Owner/admin unlimited access bypasses the daily quota. Catalog lookups and session-management calls do not consume a request.

## Speech

### List voices
`GET /api/v1/voices?gender=all&style=natural&page=1&page_size=20`

Gender: `all`, `female`, `male`. Style: `natural`, `story`, `podcast`, `voiceover`, `audiobook`, `news`, `documentary`, `meditation`, `energetic`, `whisper`, `empathetic`, `commercial`.

### Voice details
`GET /api/v1/voices/{voice_id}?gender=all`

### Generate speech
`POST /api/v1/tts`

```json
{
  "text": "Hello from Panda Voice AI.",
  "voice_id": "FISH_AUDIO_VOICE_MODEL_ID",
  "style": "natural",
  "speed": 1.0,
  "volume": 0.0
}
```

Returns MP3 bytes. Text generation consumes one shared daily request after reserving quota; failed upstream generation refunds the reservation.

### Preview
`POST /api/v1/preview` — same request body as TTS, returns inline MP3. Preview requests are not counted against the daily quota.

### Styles and engines
- `GET /api/v1/styles`
- `GET /api/v1/engines`

## AI Chat

### Stateful chat
`POST /api/v1/chat`

```json
{
  "message": "Explain text-to-speech in simple terms.",
  "session_id": null,
  "language": "en"
}
```

Supported language values: `en` English, `ar` Arabic, `es` Spanish, `la` Classical Latin. Omit language to let the assistant follow the user's language. Response includes `session_id`, `reply`, `model`, and provider usage metadata. Pass the returned session ID to continue.

### Chat Completions-compatible endpoint
`POST /api/v1/chat/completions`

```json
{
  "messages": [{"role": "user", "content": "Salve! Quid agis?"}],
  "session_id": null,
  "language": "la"
}
```

For this stateful endpoint, send exactly one new user message per request; use `session_id` to continue the conversation. The response contains an OpenAI-style `choices[0].message.content` plus `session_id`.

### Session management
- `GET /api/v1/chat/sessions?limit=20`
- `DELETE /api/v1/chat/sessions/{session_id}`

Sessions are isolated by API-token owner/account. Never share session IDs or API keys with untrusted clients.

## Telegram bot commands

- `/start`, `/menu`, `/help`
- `/chat`, `/newchat`, `/chatex`
- `/plans`, `/language`
- `/settings`, `/cancel`

## Telegram Stars subscriptions

Daily, weekly and monthly plans are purchased through Telegram Stars invoices. Each purchase adds the plan duration to any active subscription. The current implementation is **manual renewal** (not recurring auto-charge). Plan prices must be configured by the owner in Railway:

- `PANDA_PLAN_DAILY_STARS`
- `PANDA_PLAN_WEEKLY_STARS`
- `PANDA_PLAN_MONTHLY_STARS`

A value of `0` disables that plan until a price is set. Paid daily request limit is controlled by `PANDA_SUBSCRIPTION_DAILY_LIMIT` (default 100). Prices are integer Star amounts. Telegram confirms payment; the backend verifies invoice payload, account, currency, and exact amount before activating a subscription.

## OpenAPI

FastAPI-generated schema and interactive docs:
- `/openapi.json`
- `/docs`
- `/redoc`

Do not expose provider credentials. Keep `BOT_TOKEN`, Fish Audio credentials, OpenRouter credentials, the master API key, and the unlimited-access code only in Railway secrets.
