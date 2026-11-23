# MotionForge — Autonomous Motion Design Agent

Production-oriented, tool-using AI agent backend for a professional motion-design/video editor. It is mounted under `/motionforge` inside the existing Panda Voice FastAPI service. It shares the current Railway deployment and `/data` volume while keeping its own SQLite database file and API key.

## Architecture

- **Agent orchestrator:** OpenRouter Chat Completions with native tool calling, bounded multi-step execution, conversation history, workspace-scoped durable memory, and persistent plans.
- **Creative skill system:** Markdown specialist skills loaded at runtime from `skills/`.
- **Motion project system:** Validates and stores JSON2Video Movie JSON specifications in SQLite.
- **Render provider:** JSON2Video asynchronous render API; status polling returns MP4 URL when complete.
- **Voiceover:** Optional native JSON2Video `voice` element. JSON2Video supports Azure and ElevenLabs TTS. The agent only adds narration when requested or appropriate, and keeps the script and voice settings in the Movie JSON.
- **Website API:** FastAPI sub-application mounted at `/motionforge`; its interactive OpenAPI docs are at `/motionforge/docs`.
- **Persistence:** SQLite database at `/data/motion-agent.sqlite3`; mount a Railway volume at `/data`.

## Environment

Required:
- `OPENROUTER_API_KEY` — existing OpenRouter key from the Legal Movie Bot environment.
- `MOTION_AGENT_API_KEY` — a new, private API key for your website to call this service.

Optional:
- `OPENROUTER_BASE_URL` — defaults to `https://openrouter.ai/api/v1`.
- `OPENROUTER_MODEL` — defaults to `z-ai/glm-5.3-flash`, matching the existing AI runtime default; choose a tool-calling model available to your OpenRouter account.
- `OPENROUTER_REASONING_ENABLED` — defaults to `true` and enables model reasoning for long-horizon tasks.
- `JSON2VIDEO_API_KEY` — existing Legal Movie project shared variable; required for final video rendering and embedded voiceover synthesis.
- `TAVILY_API_KEY` — enables visual-reference research.
- `MOTION_AGENT_DB` — defaults to `/data/motion-agent.sqlite3`.
- `MOTION_CORS_ORIGINS` — comma-separated allowed origins; set to your editor's exact HTTPS origin(s) in production.
- `MOTION_AGENT_MAX_TOOL_STEPS` — 1–12, defaults to 8.

Never put provider keys in browser code. Only your backend should hold them. Use a separate MotionForge API key; do not reuse the existing Bot API key.

## Deploy on Railway

MotionForge runs inside the existing Railway service:
- Project: `Legal Movie Bot`
- Service: `panda-voice-ai`
- Repository: `surafel5509-del/Panda-Voice-Ai`
- Deployment branch: `main` (after the integration PR is merged)
- Existing persistent volume: `/data` (reused; MotionForge uses `/data/motion-agent.sqlite3`)

No additional Railway service or paid plan is required. The existing `OPENROUTER_API_KEY` and `JSON2VIDEO_API_KEY` project shared variables are inherited by the service. Set a unique service-level `MOTION_AGENT_API_KEY` before using the new API. Keep provider keys on the server; never place them in browser code.

The existing Panda Voice endpoints remain at their current paths. MotionForge endpoints are mounted under `/motionforge`, and its OpenAPI docs are at `/motionforge/docs`.

## REST API

All protected endpoints require `X-API-Key: <MOTION_AGENT_API_KEY>`. The paths below are relative to the `/motionforge` mount prefix.

### Chat
`POST /motionforge/api/v1/motion/agent`
```json
{
  "workspace_id": "surafel-editor",
  "session_id": null,
  "message": "Create a 15-second cinematic product launch motion graphic. Add a warm English voiceover."
}
```
Returns `session_id`, `reply`, `model`, token usage and tool step count. Reuse the returned session ID to continue the conversation.

### Other endpoints
- `GET /motionforge/health` — public health/configuration status; never returns secret values.
- `GET /motionforge/api/v1/motion/tools` — registered agent tools.
- `GET /motionforge/api/v1/motion/skills` — specialist skill catalogue.
- `GET/POST /motionforge/api/v1/motion/memories` — workspace memory.
- `GET/POST /motionforge/api/v1/motion/plans` — persistent work plans.
- `GET /motionforge/api/v1/motion/projects` — saved motion projects.
- `GET /motionforge/api/v1/motion/projects/{project_id}` — project JSON.
- `POST /motionforge/api/v1/motion/projects/{project_id}/render` — submit a paid render.
- `GET /motionforge/api/v1/motion/renders/{render_id}` — render status and MP4 URL.

Interactive API reference: `/motionforge/docs`; OpenAPI schema: `/motionforge/openapi.json`.

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
