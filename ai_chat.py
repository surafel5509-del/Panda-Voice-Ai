"""OpenRouter-backed persistent AI chat for PANDA VOICE AI."""
import asyncio
import json
import os
import sqlite3
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx

DB_PATH = os.getenv("PANDA_DATABASE_PATH", "/data/panda.sqlite3")
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
OPENROUTER_MODEL = (os.getenv("OPENROUTER_MODEL") or "z-ai/glm-5.3-flash").strip()
OPENROUTER_BASE_URL = (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_REASONING = (os.getenv("OPENROUTER_REASONING_ENABLED") or "true").lower() in {"1", "true", "yes", "on"}
MAX_CHAT_HISTORY_MESSAGES = 16
SYSTEM_PROMPT = (
    "You are PANDA VOICE AI, a helpful, accurate, friendly multilingual assistant. "
    "Reply in the language used by the user unless asked otherwise. "
    "Be clear and practical. Do not claim to have performed actions you did not perform. "
    "For requests involving current facts, explain uncertainty when you cannot verify them."
)
_session_locks = defaultdict(asyncio.Lock)


class AIChatError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_ai_chat_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_chat_sessions (
                session_id TEXT PRIMARY KEY,
                owner_key TEXT NOT NULL,
                title TEXT NOT NULL,
                messages_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_chat_owner_updated ON ai_chat_sessions(owner_key, updated_at DESC)")


async def chat_completion(owner_key: str, user_text: str, session_id: str | None = None, language: str | None = None):
    if not OPENROUTER_API_KEY:
        raise AIChatError("AI Chat is not configured. Add OPENROUTER_API_KEY to Railway variables.", 503)
    user_text = (user_text or "").strip()
    if not user_text:
        raise AIChatError("Message cannot be empty.", 422)
    if len(user_text) > int(os.getenv("MAX_AI_CHAT_CHARS", "6000")):
        raise AIChatError("Message is too long.", 413)

    requested_id = session_id
    sid = session_id or str(uuid.uuid4())
    try:
        sid = str(uuid.UUID(sid))
    except (ValueError, TypeError, AttributeError):
        raise AIChatError("Invalid session_id.", 422)

    async with _session_locks[sid]:
        with _db() as conn:
            row = conn.execute(
                "SELECT owner_key, title, messages_json, created_at FROM ai_chat_sessions WHERE session_id=?",
                (sid,),
            ).fetchone()
        if row and row["owner_key"] != owner_key:
            raise AIChatError("Chat session not found.", 404)
        if requested_id and not row:
            raise AIChatError("Chat session not found.", 404)
        history = json.loads(row["messages_json"]) if row else []
        language_prompts = {
            "en": "Respond in English.",
            "ar": "Respond in clear, natural Arabic.",
            "es": "Respond in clear, natural Spanish.",
            "la": "Respond in Classical Latin (Latinitas), using grammatically correct Latin.",
        }
        system_prompt = SYSTEM_PROMPT + (" " + language_prompts[language] if language in language_prompts else "")
        messages = [{"role": "system", "content": system_prompt}, *history,
                    {"role": "user", "content": user_text}]
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "reasoning": {"enabled": OPENROUTER_REASONING},
            "temperature": 0.7,
            "max_tokens": 2048,
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://panda-voice-ai-production.up.railway.app",
            "X-Title": "PANDA VOICE AI",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
                response = await client.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException:
            raise AIChatError("AI Chat timed out. Please try again.", 504)
        except httpx.HTTPError:
            raise AIChatError("Could not connect to the AI provider.", 502)

        if response.status_code == 401:
            raise AIChatError("OpenRouter authentication failed. Check OPENROUTER_API_KEY.", 503)
        if response.status_code == 429:
            raise AIChatError("AI provider rate limit reached. Please try again shortly.", 429)
        if response.status_code >= 500:
            raise AIChatError("AI provider is temporarily unavailable.", 502)
        if response.status_code >= 400:
            raise AIChatError("AI provider rejected the request. Check the configured model and request.", 502)
        try:
            data = response.json()
            assistant_message = data["choices"][0]["message"]
            answer = assistant_message.get("content")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("Empty assistant response")
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIChatError("AI provider returned an invalid response.", 502)

        # Keep OpenRouter's reasoning_details opaque and unmodified for continuation.
        saved_assistant = {"role": "assistant", "content": answer}
        if "reasoning_details" in assistant_message:
            saved_assistant["reasoning_details"] = assistant_message["reasoning_details"]
        new_history = (history + [{"role": "user", "content": user_text}, saved_assistant])[-MAX_CHAT_HISTORY_MESSAGES:]
        now = datetime.now(timezone.utc).isoformat()
        title = row["title"] if row else user_text[:72]
        with _connect() as conn:
            conn.execute("""
                INSERT INTO ai_chat_sessions(session_id, owner_key, title, messages_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages_json=excluded.messages_json, updated_at=excluded.updated_at
            """, (sid, owner_key, title, json.dumps(new_history, ensure_ascii=False),
                  row["created_at"] if row else now, now))
        return {
            "session_id": sid,
            "reply": answer,
            "model": data.get("model", OPENROUTER_MODEL),
            "usage": data.get("usage"),
        }


def list_chat_sessions(owner_key: str, limit: int = 20):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT session_id, title, created_at, updated_at FROM ai_chat_sessions "
            "WHERE owner_key=? ORDER BY updated_at DESC LIMIT ?",
            (owner_key, max(1, min(limit, 50))),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_chat_session(owner_key: str, session_id: str) -> bool:
    try:
        sid = str(uuid.UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        raise AIChatError("Invalid session_id.", 422)
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM ai_chat_sessions WHERE session_id=? AND owner_key=?",
            (sid, owner_key),
        )
        return cur.rowcount > 0
