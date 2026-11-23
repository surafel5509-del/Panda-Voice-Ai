# Panda Voice AI — API reference

Base URL: `https://panda-voice-ai-production.up.railway.app`

All application endpoints require an API token in `X-API-Key`, except `GET /health` and the Telegram webhook. Create and delete per-user API tokens through the Telegram **Developer Center** or authenticated token-management endpoints. Treat tokens as passwords.

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
- `GET /api/v1/subscription` — current subscription and remaining requests.
- `GET /api/v1/tokens` — list owned API token metadata (never returns token secrets).
- `DELETE /api/v1/tokens/{token_id}` — revoke/delete an owned API token.

Free accounts share **2 requests per UTC day** across Telegram AI Chat, Telegram speech generation, and authenticated API Chat/TTS. Paid allowances apply across the full purchased subscription period: Daily = 50 requests / 1 day, Weekly = 500 / 7 days, Monthly = 2,500 / 30 days. All API tokens for one account share the same allowance. Owner/admin unlimited access bypasses the daily quota. Catalog lookups and session-management calls do not consume a request. Free accounts may change voice style/model twice per UTC day; paid accounts have no style/model switching cap.

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
  "model": "s2.1-pro-free",
  "speed": 1.0,
  "volume": 0.0
}
```

Returns MP3 bytes. Optional `model` values are `s2.1-pro-free`, `s2.1-pro`, `s2-pro`, and `s1`; premium engines require a paid subscription. Free users may change style/model a combined 2 times per UTC day; paid subscribers may switch freely. Speech generation consumes one shared account request; failed upstream generation refunds the reservation.

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

### Professional agent endpoint

`POST /api/v1/agent` — same stateful request and response shape as `POST /api/v1/chat`, with bounded tool calling enabled. It uses the same session store, per-account authentication, rate limits and shared subscription quota.

```json
{
  "message": "Research the latest renewable energy trends, compare three sources, and calculate a 15% increase on 2400.",
  "session_id": null
}
```

Built-in tools:
- `calculator` — safe arithmetic and approved math functions.
- `get_current_datetime` — current date/time in an optional IANA timezone.
- `web_search` — live web search through Tavily; requires the server-side `TAVILY_API_KEY`.

`GET /api/v1/agent/tools` returns the registered tool catalogue and whether optional web search is configured. All agent endpoints require `X-API-Key`.

Optional Railway configuration:
- `TAVILY_API_KEY` — enables live web research.
- `AGENT_MAX_TOOL_STEPS` — maximum tool rounds per request (default 5, clamped to 1–8).
- `AGENT_MAX_SEARCH_RESULTS` — maximum web results per search (default 5, clamped to 1–8).

Only registered tools execute. No shell, filesystem, arbitrary code execution or unrestricted HTTP tool is exposed. Select an OpenRouter model with function/tool-calling support.

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
- `/settings`, `/developers`, `/cancel`

## Telegram Stars subscriptions

Daily, weekly and monthly plans are purchased through Telegram Stars invoices. Each purchase adds the plan duration to any active subscription. The current implementation is **manual renewal** (not recurring auto-charge). Default prices are Daily 100 Stars, Weekly 399 Stars, and Monthly 700 Stars. The included request allowances are 50, 500, and 2,500 respectively for each purchased period. Prices can be overridden in Railway using `PANDA_PLAN_DAILY_STARS`, `PANDA_PLAN_WEEKLY_STARS`, and `PANDA_PLAN_MONTHLY_STARS`. Telegram confirms payment; the backend verifies invoice payload, account, currency, and exact amount before activating a subscription.

## OpenAPI

FastAPI-generated schema and interactive docs:
- `/openapi.json`
- `/docs`
- `/redoc`

Do not expose provider credentials. Keep `BOT_TOKEN`, Fish Audio credentials, OpenRouter credentials, the master API key, and the unlimited-access code only in Railway secrets.


## Long-running agent tasks

Use these endpoints for requests that require multi-step research, analysis, planning, writing, and review. They are asynchronous: the create request returns HTTP 202 and a task ID immediately. All endpoints require `X-API-Key` and are scoped to the authenticated account owner.

### Create a task

`POST /api/v1/agent/tasks`

```json
{
  "message": "Research three credible sources about renewable energy adoption, compare their findings, calculate the percentage growth from 2400 to 3100, and prepare a concise report.",
  "language": "en"
}
```

Optional `language`: `en`, `am`, `ar`, or `es`. Omit it to follow the language of the request.

Example response (HTTP 202):

```json
{
  "task_id": "returned-task-uuid",
  "status": "queued",
  "status_url": "/api/v1/agent/tasks/returned-task-uuid",
  "message": "Task accepted. Poll status_url for progress and the final result."
}
```

### Check task progress and result

`GET /api/v1/agent/tasks/{task_id}`

The response includes:
- `status`: `queued`, `running`, `completed`, `failed`, or `cancelled`.
- `progress`, `current_step`, and `total_steps`.
- `plan` and persisted `step_results`.
- Recent `events` showing planning, step completion, retries, and quality review.
- `result` when completed, or `error` when failed.

Poll every 2–5 seconds while the task is queued or running. A completed result is stored in the persistent SQLite database and can be fetched later.

### List recent tasks

`GET /api/v1/agent/tasks?limit=20`

Returns recent task summaries for the authenticated account. The maximum limit is 50.

### Cancel a task

`POST /api/v1/agent/tasks/{task_id}/cancel`

A queued task is cancelled immediately. For a running task, cancellation is requested and takes effect after the current model call completes. Completed or failed tasks are not changed.

### Execution limits and safety

- One shared account request is consumed when a task is accepted; status polling and cancellation do not consume extra requests.
- Plans are limited to 8 steps by default; each step can use the existing bounded agent tools.
- Each step gets one retry after a transient provider failure.
- A final quality-review pass checks coverage, cautions, and missing items before the final answer is assembled.
- Task execution is capped at 15 minutes by default; settings can be adjusted within hard server-side bounds.
- Task state, step outputs, and events are stored in the existing persistent SQLite volume. A 3-minute worker lease is refreshed every 45 seconds, allowing another worker to reclaim interrupted tasks after the lease expires.
- Tasks are isolated by authenticated account owner. Never share task IDs or API keys with untrusted clients.
- The agent has no shell, arbitrary code, filesystem, unrestricted HTTP, or external account-mutation tools.

Optional Railway variables:
- `AGENT_MAX_TASK_STEPS` (default `8`, hard range 1–8).
- `AGENT_MAX_TASK_SECONDS` (default `900`, hard range 60–1,800).
- `AGENT_TASK_POLL_SECONDS` (default `2`, hard range 0.5–10).
- `AGENT_MAX_ACTIVE_TASKS_PER_OWNER` (default `3`, hard range 1–10).


## Telegram AI Agent

The Telegram bot exposes both quick tool-assisted chat and resumable long-running agent tasks:

- `/chat` — quick AI conversation using the same bounded tool-calling runtime as `POST /api/v1/agent`.
- `/agent` — enter Long Task Agent mode; send a multi-step request to queue it for background execution.
- `/task [task_id]` — check the latest task, or a specific task ID. Completed tasks return the saved final result.
- `/cancelagent [task_id]` — cancel the latest task, or a specific task owned by the Telegram account.
- `/agentoff` — leave Long Task Agent mode.
- `/newchat` and `/chatex` — manage quick AI Chat independently.

Long-running Telegram tasks use the same persistent task worker, SQLite storage, owner isolation, retry limits and quality review as the REST task API. Submitting a task consumes one shared account request. Checking or cancelling a task does not consume an additional request. The task ID is private to the account that submitted it.

### API examples

Base URL: `https://panda-voice-ai-production.up.railway.app`

Quick agent request:

```bash
curl -X POST https://panda-voice-ai-production.up.railway.app/api/v1/agent \
  -H 'X-API-Key: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Explain this topic and calculate 15% of 2400.","session_id":null,"language":"am"}'
```

Create a background task:

```bash
curl -X POST https://panda-voice-ai-production.up.railway.app/api/v1/agent/tasks \
  -H 'X-API-Key: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Research this topic, compare reliable sources and prepare a report.","language":"am"}'
```

Check progress/result:

```bash
curl https://panda-voice-ai-production.up.railway.app/api/v1/agent/tasks/TASK_ID \
  -H 'X-API-Key: YOUR_API_KEY'
```

Cancel a task:

```bash
curl -X POST https://panda-voice-ai-production.up.railway.app/api/v1/agent/tasks/TASK_ID/cancel \
  -H 'X-API-Key: YOUR_API_KEY'
```

Use an account API token in the `X-API-Key` header. Do not put API keys in public frontend code or share task IDs with other users.
