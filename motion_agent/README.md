# MotionForge — Autonomous Motion Design Agent

Production-oriented, tool-using AI agent backend for a professional motion-design/video editor. It is an isolated Railway service rooted at `motion_agent/`; it does not modify or replace the existing Panda Voice service.

## Architecture

- **Agent orchestrator:** OpenRouter Chat Completions with native tool calling, bounded multi-step execution, conversation history, workspace-scoped durable memory, and persistent plans.
- **Creative skill system:** Markdown specialist skills loaded at runtime from `skills/`.
- **Motion project system:** Validates and stores JSON2Video Movie JSON specifications in SQLite.
- **Render provider:** JSON2Video asynchronous render API; status polling returns MP4 URL when complete.
- **Voiceover:** Optional native JSON2Video `voice` element. JSON2Video supports Azure and ElevenLabs TTS. The agent only adds narration when requested or appropriate, and keeps the script and voice settings in the Movie JSON.
- **Website API:** FastAPI REST endpoints and generated OpenAPI docs at `/docs`.
- **Persistence:** SQLite database at `/data/motion-agent.sqlite3`; mount a Railway volume at `/data`.

## Environment

Required:
- `OPENROUTER_API_KEY` — existing OpenRouter key from the Legal Movie Bot environment.
- `MOTION_AGENT_API_KEY` — a new, private API key for your website to call this service.

Optional:
- `OPENROUTER_BASE_URL` — defaults to `https://openrouter.ai/api/v1`.
- `OPENROUTER_MODEL` — defaults to `z-ai/glm-5.3-flash`; choose a tool-calling model available to your OpenRouter account.
- `JSON2VIDEO_API_KEY` — existing JSON2Video key; required only for final video rendering.
- `TAVILY_API_KEY` — enables visual-reference research.
- `MOTION_AGENT_DB` — defaults to `/data/motion-agent.sqlite3`.
- `MOTION_CORS_ORIGINS` — comma-separated allowed origins; set to your editor's exact HTTPS origin(s) in production.
- `MOTION_AGENT_MAX_TOOL_STEPS` — 1–12, defaults to 8.

Never put provider keys in browser code. Only your backend should hold them. Use a separate MotionForge API key; do not reuse the existing Bot API key.

## Deploy on Railway

Create a new service in the existing `Legal Movie Bot` project, with this repository and branch:
- Repository: `surafel5509-del/Panda-Voice-Ai`
- Branch: `motion-agent-foundation`
- Root directory: `/motion_agent`
- Healthcheck: `/health`
- Start command: Dockerfile default
- Persistent volume: `/data`

Set provider keys on the new service using Railway variable references, not by copying or printing secret values:
- `OPENROUTER_API_KEY=${{panda-voice-ai.OPENROUTER_API_KEY}}`
- `JSON2VIDEO_API_KEY=${{panda-voice-ai.JSON2VIDEO_API_KEY}}`

Generate a unique `MOTION_AGENT_API_KEY` and set it only on this service. Configure CORS to the website origin. The existing `panda-voice-ai` service, its variables, database and volume must remain untouched.

## REST API

All protected endpoints require `X-API-Key: <MOTION_AGENT_API_KEY>`.

### Chat
`POST /api/v1/motion/agent`
```json
{
  "workspace_id": "surafel-editor",
  "session_id": null,
  "message": "Create a 15-second cinematic product launch motion graphic. Add a warm English voiceover."
}
```
Returns `session_id`, `reply`, `model`, token usage and tool step count. Reuse the returned session ID to continue the conversation.

### Other endpoints
- `GET /health` — public health/configuration status; never returns secret values.
- `GET /api/v1/motion/tools` — registered agent tools.
- `GET /api/v1/motion/skills` — specialist skill catalogue.
- `GET/POST /api/v1/motion/memories` — workspace memory.
- `GET/POST /api/v1/motion/plans` — persistent work plans.
- `GET /api/v1/motion/projects` — saved motion projects.
- `GET /api/v1/motion/projects/{project_id}` — project JSON.
- `POST /api/v1/motion/projects/{project_id}/render` — submit a paid render.
- `GET /api/v1/motion/renders/{render_id}` — render status and MP4 URL.

Interactive API reference: `/docs`; OpenAPI schema: `/openapi.json`.

## JSON2Video voiceover

A voiceover is an ordinary Movie element:
```json
{
  "type": "voice",
  "text": "Your narration script goes here.",
  "model": "azure",
  "voice": "en-US-EmmaMultilingualNeural",
  "start": 0
}
```
The voice element is optional. Select a voice that exists in the provider catalogue and supports the narration language. Use only lawful narration and authorized voices; do not imitate a real person without authorization. Rendering is asynchronous and may consume account quota/credits. Do not submit a render unless the user asks for the finished video.

## Production notes and current boundaries

- This service is an agent API and motion-project orchestration backend, not yet a full browser-based nonlinear editor.
- JSON2Video renders scene/layer-based compositions and TTS narration; it is not a substitute for a real-time editable 3D engine such as Blender/Three.js.
- The agent must not claim to have rendered or saved an artifact unless the provider/database confirms it.
- The current API key is a service-level shared secret. For a public multi-user SaaS, add per-user auth/JWT, tenant-level quotas, billing, rate limits, audit events and key rotation before exposing it broadly.
- SQLite on a Railway volume is suitable for a first persistent service instance. For horizontal scaling and concurrent multi-instance workloads, migrate memory/session/plan/project records to PostgreSQL and add a Redis-backed job queue.
