import asyncio
import hashlib
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, LabeledPrice, BotCommand, MenuButtonCommands
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, PreCheckoutQueryHandler, filters,
)

from ai_chat import (
    AIChatError, chat_completion, delete_chat_session, init_ai_chat_db,
    list_chat_sessions,
)
from billing import (
    activate_paid_subscription, access_state, consume_request, create_pending_payment,
    current_subscription, init_billing_db, plan_catalog, refund_request,
    validate_pending_payment,
)

# Never log HTTP request URLs: Telegram bot URLs contain the bot token.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
for noisy_logger in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)
log = logging.getLogger("panda-voice-ai")

BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
FISH_API_KEY = (os.getenv("FISH_API_KEY") or os.getenv("FISH_AUDIO_API") or "").strip()
PANDA_API_KEY = os.getenv("PANDA_API_KEY", "").strip()
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free").strip()
MAX_TEXT_CHARS = min(max(int(os.getenv("MAX_TEXT_CHARS", "4000")), 100), 10000)
MAX_AI_CHAT_CHARS = min(max(int(os.getenv("MAX_AI_CHAT_CHARS", "6000")), 100), 10000)
FISH_BASE = "https://api.fish.audio"
MODEL_PAGE_SIZE = 100
VOICE_MAX_PAGES = 10
DB_PATH = os.getenv("PANDA_DATABASE_PATH", "/data/panda.sqlite3")
PANDA_UNLIMITED_CODE = os.getenv("PANDA_UNLIMITED_CODE", "")
DAILY_REQUEST_LIMIT = max(1, int(os.getenv("PANDA_DAILY_REQUEST_LIMIT", "2")))

# These are S2 natural-language direction prompts, not imaginary API style fields.
STYLES = {
    "natural": ("Natural", "", "A clean, natural speaking voice."),
    "story": ("Storytelling", "[warm, expressive storytelling with varied pacing] ", "Warm, expressive narration."),
    "podcast": ("Podcast", "[conversational, engaging podcast delivery] ", "Conversational and engaging."),
    "voiceover": ("Voice Over", "[confident, polished voice-over delivery] ", "Confident studio delivery."),
    "audiobook": ("Audiobook", "[immersive audiobook narration, clear and expressive] ", "Immersive long-form narration."),
    "news": ("News", "[professional newsreader delivery, precise articulation, measured pace] ", "Precise and measured."),
    "documentary": ("Documentary", "[calm, authoritative documentary narration] ", "Calm and authoritative."),
    "meditation": ("Meditation", "[gentle, soothing, unhurried delivery] ", "Gentle and soothing."),
    "energetic": ("Energetic", "[energetic, upbeat, enthusiastic delivery] ", "Upbeat and enthusiastic."),
    "whisper": ("Whisper", "[soft, intimate whispering delivery] ", "Soft and intimate."),
    "empathetic": ("Empathetic", "[warm, empathetic, reassuring delivery] ", "Warm and reassuring."),
    "commercial": ("Commercial", "[confident, persuasive, polished commercial delivery] ", "Polished promotional delivery."),
}
STYLE_TAG_HINTS = {
    "story": ("story", "narrat", "audiobook"),
    "podcast": ("podcast", "conversational", "talk"),
    "voiceover": ("voice over", "voiceover", "narrat"),
    "audiobook": ("audiobook", "narrat", "story"),
    "news": ("news", "reporter", "anchor"),
    "documentary": ("documentary", "narrat"),
    "meditation": ("meditation", "calm", "soothing"),
    "energetic": ("energetic", "upbeat", "excited"),
    "whisper": ("whisper", "soft"),
    "empathetic": ("empathetic", "warm", "gentle"),
    "commercial": ("commercial", "advert", "promo"),
}
PREVIEW_TEXT = "Hello! This is a short voice preview from Panda Voice AI."
STYLE_PREVIEW_TEXT = "Welcome. Let me show you how this selected speaking style sounds."

voice_cache = {}
voice_cache_lock = asyncio.Lock()
VOICE_CACHE_SECONDS = 300
user_locks = defaultdict(asyncio.Lock)
recent_history = defaultdict(lambda: deque(maxlen=8))
api_rate = defaultdict(deque)
telegram_app: Application | None = None
db_lock = asyncio.Lock()


def db_connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_session():
    conn = db_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_session() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS panda_users (
            user_id INTEGER PRIMARY KEY, unlimited INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT, revoked_at TEXT, last_used_at TEXT,
            usage_day TEXT, usage_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES panda_users(user_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS panda_customization_usage (user_id INTEGER NOT NULL, usage_day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, usage_day))")
        conn.execute("CREATE TABLE IF NOT EXISTS panda_api_preferences (user_id INTEGER PRIMARY KEY, style TEXT NOT NULL DEFAULT 'natural', model TEXT NOT NULL DEFAULT 's2.1-pro-free')")
        conn.commit()


def utc_now():
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_user(conn, user_id: int):
    conn.execute("INSERT OR IGNORE INTO panda_users(user_id, unlimited, created_at) VALUES(?, 0, ?)",
                 (user_id, utc_now().isoformat()))


def active_tokens(conn, user_id: int):
    now = utc_now().isoformat()
    return conn.execute(
        "SELECT * FROM api_tokens WHERE user_id=? AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC", (user_id, now)
    ).fetchall()


def create_user_token(user_id: int, name: str, days: int):
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_user(conn, user_id)
        user = conn.execute("SELECT unlimited FROM panda_users WHERE user_id=?", (user_id,)).fetchone()
        tokens = active_tokens(conn, user_id)
        max_tokens = max(1, int(os.getenv("PANDA_MAX_ACTIVE_TOKENS", "5")))
        if len(tokens) >= max_tokens:
            raise ValueError(f"You can have up to {max_tokens} active API tokens. Delete one first.")
        token = "panda_" + secrets.token_urlsafe(32)
        created = utc_now()
        if days not in (0, 7, 30, 90, 365) or (days == 0 and not user["unlimited"]):
            raise ValueError("Invalid token validity period.")
        expires = None if days == 0 else (created + timedelta(days=days)).isoformat()
        cur = conn.execute(
            "INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, name[:48], token_hash(token), token[:13], created.isoformat(), expires))
        return {"id": cur.lastrowid, "token": token, "name": name[:48],
                "expires_at": expires, "unlimited": bool(user["unlimited"])}


def get_user_tokens(user_id: int):
    with db_session() as conn:
        ensure_user(conn, user_id)
        user = conn.execute("SELECT unlimited FROM panda_users WHERE user_id=?", (user_id,)).fetchone()
        rows = conn.execute(
            "SELECT id,name,token_prefix,created_at,expires_at,revoked_at,last_used_at,usage_count,usage_day "
            "FROM api_tokens WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
        return bool(user["unlimited"]), [dict(r) for r in rows]


def revoke_user_token(user_id: int, token_id: int) -> bool:
    # Permanently remove the owned token so it disappears from the account and can no longer authenticate.
    with db_session() as conn:
        cur = conn.execute("DELETE FROM api_tokens WHERE id=? AND user_id=?", (token_id, user_id))
        return cur.rowcount > 0


def unlock_user_unlimited(user_id: int):
    with db_session() as conn:
        ensure_user(conn, user_id)
        conn.execute("UPDATE panda_users SET unlimited=1 WHERE user_id=?", (user_id,))


def fish_headers(model: str | None = None) -> dict:
    if not FISH_API_KEY:
        raise HTTPException(status_code=503, detail="Speech service is not configured.")
    headers = {"Authorization": f"Bearer {FISH_API_KEY}", "Content-Type": "application/json"}
    if model:
        headers["model"] = model
    return headers


async def fish_tts(text: str, voice_id: str, style: str = "natural", speed: float = 1.0,
                   volume: float = 0.0, model: str | None = None) -> bytes:
    if style not in STYLES:
        raise HTTPException(status_code=422, detail="Unsupported speaking style.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", voice_id):
        raise HTTPException(status_code=422, detail="Invalid voice identifier.")
    label, prefix, _ = STYLES[style]
    selected_model = model or FISH_MODEL
    if selected_model not in {"s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"}:
        raise HTTPException(status_code=422, detail="Unsupported speech model.")
    payload = {
        "text": prefix + text,
        "reference_id": voice_id,
        "format": "mp3",
        "mp3_bitrate": 128,
        "latency": "normal",
        "prosody": {"speed": speed, "volume": volume, "normalize_loudness": True},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            async with client.stream("POST", f"{FISH_BASE}/v1/tts",
                                     headers=fish_headers(selected_model), json=payload) as res:
                if res.status_code != 200:
                    raw = (await res.aread()).decode("utf-8", "replace")[:500]
                    log.warning("Fish TTS failed status=%s body=%s", res.status_code, raw)
                    if res.status_code == 401:
                        raise HTTPException(status_code=502, detail="Speech provider authentication failed.")
                    if res.status_code == 402:
                        raise HTTPException(status_code=402, detail="The selected speech model requires API credit.")
                    if res.status_code == 429:
                        raise HTTPException(status_code=429, detail="Speech provider is busy. Try again shortly.")
                    raise HTTPException(status_code=502, detail="Speech generation failed.")
                chunks = []
                size = 0
                async for chunk in res.aiter_bytes():
                    size += len(chunk)
                    if size > 25 * 1024 * 1024:
                        raise HTTPException(status_code=413, detail="Generated audio is too large.")
                    chunks.append(chunk)
                audio = b"".join(chunks)
                if not audio:
                    raise HTTPException(status_code=502, detail="Speech provider returned empty audio.")
                return audio
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Speech generation timed out.")
    except httpx.HTTPError:
        log.exception("Fish Audio network error")
        raise HTTPException(status_code=502, detail="Could not reach the speech provider.")


def detect_gender(item: dict) -> str:
    # Tokenize words: substring matching made "female" also match "male".
    raw_tags = item.get("tags") or []
    source = " ".join([str(x) for x in raw_tags] + [str(item.get("title") or "")])
    words = set(re.findall(r"[a-z]+", source.lower()))
    female = bool(words & {"female", "woman", "women", "girl", "feminine"})
    male = bool(words & {"male", "man", "men", "boy", "masculine"})
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return "unclassified"


def style_score(item: dict, style: str) -> int:
    terms = STYLE_TAG_HINTS.get(style, ())
    haystack = " ".join([
        str(item.get("title") or ""),
        str(item.get("description") or ""),
        " ".join(str(x) for x in (item.get("tags") or [])),
    ]).lower()
    return sum(1 for term in terms if term in haystack)


async def get_voices(gender: str = "all", force: bool = False) -> list[dict]:
    if gender not in ("all", "female", "male"):
        raise HTTPException(status_code=422, detail="Gender must be all, female, or male.")
    now = time.monotonic()
    cached = voice_cache.get(gender)
    if cached and not force and now - cached[0] < VOICE_CACHE_SECONDS:
        return cached[1]
    async with voice_cache_lock:
        cached = voice_cache.get(gender)
        if cached and not force and now - cached[0] < VOICE_CACHE_SECONDS:
            return cached[1]
        if not FISH_API_KEY:
            raise HTTPException(status_code=503, detail="Voice library is not configured.")
        all_items = []
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                for page in range(1, VOICE_MAX_PAGES + 1):
                    params = {"page_size": MODEL_PAGE_SIZE, "page_number": page, "sort_by": "score"}
                    # Do not send a gender tag filter: Fish's catalog tags are inconsistent.
                    res = await client.get(f"{FISH_BASE}/model", headers=fish_headers(), params=params)
                    if res.status_code != 200:
                        log.warning("Fish model library failed status=%s page=%s", res.status_code, page)
                        raise HTTPException(status_code=502, detail="Could not load the voice library.")
                    body = res.json()
                    items = body.get("items", []) if isinstance(body, dict) else []
                    all_items.extend(items)
                    if len(items) < MODEL_PAGE_SIZE:
                        break
        except HTTPException:
            raise
        except (ValueError, httpx.HTTPError):
            log.exception("Could not parse Fish model library response")
            raise HTTPException(status_code=502, detail="Could not load the voice library.")
        result, seen = [], set()
        for item in all_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in (None, "tts") or item.get("state") not in (None, "trained"):
                continue
            if item.get("visibility") not in (None, "public"):
                continue
            voice_id = str(item.get("_id", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", voice_id) or voice_id in seen:
                continue
            item_gender = detect_gender(item)
            if gender in ("female", "male") and item_gender != gender:
                continue
            seen.add(voice_id)
            result.append({
                "id": voice_id,
                "name": str(item.get("title") or "Untitled voice")[:100],
                "gender": item_gender,
                "tags": [str(x)[:40] for x in (item.get("tags") or [])[:20]],
                "languages": [str(x)[:20] for x in (item.get("languages") or [])[:20]],
                "style_score": 0,
            })
        voice_cache[gender] = (now, result)
        log.info("Voice catalog loaded gender=%s count=%s scanned=%s", gender, len(result), len(all_items))
        return result


def best_voice(voices: list[dict], style: str) -> dict | None:
    if not voices:
        return None
    ranked = max(voices, key=lambda v: (style_score(v, style), len(v.get("languages", []))))
    return ranked if style_score(ranked, style) > 0 else None


async def require_api_key(x_api_key: str | None):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header.")
    fingerprint = token_hash(x_api_key)
    now_mono = time.monotonic()
    window = api_rate[fingerprint]
    while window and now_mono - window[0] > 60:
        window.popleft()
    if len(window) >= int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "30")):
        raise HTTPException(status_code=429, detail="PANDA API rate limit exceeded. Retry in one minute.")
    window.append(now_mono)

    if PANDA_API_KEY and secrets.compare_digest(x_api_key, PANDA_API_KEY):
        return {"user_id": None, "unlimited": True, "master": True, "owner_key": "master:" + fingerprint}
    with db_session() as conn:
        row = conn.execute(
            "SELECT t.*, u.unlimited AS user_unlimited FROM api_tokens t "
            "JOIN panda_users u ON u.user_id=t.user_id WHERE t.token_hash=? AND t.revoked_at IS NULL",
            (fingerprint,)).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid API key.")
        now = utc_now()
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= now:
            raise HTTPException(status_code=401, detail="API token has expired.")
        unlimited = bool(row["user_unlimited"])
        day = now.date().isoformat()
        count = row["usage_count"] if row["usage_day"] == day else 0
        conn.execute(
            "UPDATE api_tokens SET usage_day=?, usage_count=?, last_used_at=? WHERE id=?",
            (day, count + 1, now.isoformat(), row["id"]))
        return {"user_id": row["user_id"], "token_id": row["id"],
                "unlimited": unlimited, "master": False, "daily_used": count + 1,
                "owner_key": "telegram-user:" + str(row["user_id"])}


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_AI_CHAT_CHARS)
    session_id: str | None = Field(default=None, max_length=64)
    language: Literal["en", "ar", "es", "la"] | None = None


class ChatMessage(BaseModel):
    role: Literal["user"]
    content: str = Field(min_length=1, max_length=MAX_AI_CHAT_CHARS)


class ChatCompletionsInput(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=1)
    session_id: str | None = Field(default=None, max_length=64)
    language: Literal["en", "ar", "es", "la"] | None = None


class TTSInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    voice_id: str = Field(min_length=8, max_length=80)
    style: Literal["natural", "story", "podcast", "voiceover", "audiobook", "news",
                   "documentary", "meditation", "energetic", "whisper", "empathetic",
                   "commercial"] = "natural"
    model: Literal["s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"] | None = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: float = Field(default=0.0, ge=-20.0, le=20.0)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    init_db()
    init_ai_chat_db()
    init_billing_db()
    if BOT_TOKEN:
        telegram_app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("chat", ai_chat_command))
        telegram_app.add_handler(CommandHandler("newchat", new_chat_command))
        telegram_app.add_handler(CommandHandler("chatex", exit_chat_command))
        telegram_app.add_handler(CommandHandler("menu", menu_command))
        telegram_app.add_handler(CommandHandler("plans", plans_command))
        telegram_app.add_handler(CommandHandler("language", language_command))
        telegram_app.add_handler(CommandHandler("settings", settings_command))
        telegram_app.add_handler(CommandHandler("developers", developers_command))
        telegram_app.add_handler(CommandHandler("cancel", cancel))
        telegram_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
        telegram_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
        telegram_app.add_handler(CallbackQueryHandler(on_button))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        await telegram_app.initialize()
        await telegram_app.bot.set_my_commands([
            BotCommand("start", "Open Panda Voice AI"), BotCommand("menu", "Show main menu"),
            BotCommand("chat", "Start AI Chat"), BotCommand("newchat", "New AI conversation"),
            BotCommand("chatex", "Exit AI Chat"), BotCommand("plans", "Subscription plans"),
            BotCommand("language", "AI Chat language"), BotCommand("settings", "Voice and model settings"),
            BotCommand("developers", "API tokens and developer tools"), BotCommand("help", "Help"),
            BotCommand("cancel", "Cancel current action"),
        ])
        await telegram_app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        await telegram_app.start()
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
        if not webhook_url and domain:
            webhook_url = f"https://{domain}/telegram/webhook"
        webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        if webhook_url and webhook_secret:
            await telegram_app.bot.set_webhook(
                url=webhook_url,
                secret_token=webhook_secret,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            log.info("Telegram webhook registered.")
        elif telegram_app.updater:
            log.warning("Webhook configuration missing; using polling for local development.")
            await telegram_app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started.")
    else:
        log.warning("Telegram bot is disabled: no bot token configured.")
    yield
    if telegram_app:
        if telegram_app.updater and telegram_app.updater.running:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(
    title="PANDA VOICE AI",
    version="3.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "PANDA VOICE AI", "version": "3.1.0", "database": os.path.exists(DB_PATH)}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Telegram bot is not initialized.")
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")
    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception:
        log.exception("Could not process Telegram webhook update")
        raise HTTPException(status_code=400, detail="Invalid Telegram update.")
    return {"ok": True}


@app.get("/api/v1/account")
async def api_account(x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    if account["master"]:
        return {"access": "master", "daily_limit": None, "usage_today": None}
    with db_session() as conn:
        row = conn.execute("SELECT usage_count,usage_day,expires_at,name FROM api_tokens WHERE id=?",
                           (account["token_id"],)).fetchone()
    used = row["usage_count"] if row["usage_day"] == utc_now().date().isoformat() else 0
    quota = access_state(account["user_id"], account["unlimited"])
    return {"token_name": row["name"], **quota, "token_usage_today": used,
            "token_expires_at": row["expires_at"]}


@app.get("/api/v1/limits")
async def api_limits(x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    quota = access_state(account["user_id"], account["unlimited"]) if account["user_id"] is not None else {
        "access": "unlimited", "daily_limit": None, "used_today": None, "remaining_today": None
    }
    return {"max_active_tokens": int(os.getenv("PANDA_MAX_ACTIVE_TOKENS", "5")), **quota,
            "per_minute_limit": int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "30")),
            "max_text_chars": MAX_TEXT_CHARS, "token_expiry_required": not account["unlimited"]}


@app.get("/api/v1/styles")
async def api_styles(x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    return {"items": [
        {"id": key, "name": value[0], "description": value[2]}
        for key, value in STYLES.items()
    ]}


@app.get("/api/v1/engines")
async def api_engines(x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    return {"default": FISH_MODEL, "items": [
        {"id": "s2.1-pro-free", "name": "S2.1 Pro Free", "free": True},
        {"id": "s2.1-pro", "name": "S2.1 Pro", "free": False},
        {"id": "s2-pro", "name": "S2 Pro", "free": False},
        {"id": "s1", "name": "S1 (legacy)", "free": False},
    ]}


@app.get("/api/v1/voices")
async def api_voices(gender: Literal["all", "female", "male"] = "all",
                     style: str = "natural",
                     page: int = Query(default=1, ge=1, le=100),
                     page_size: int = Query(default=20, ge=1, le=50),
                     x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    if style not in STYLES:
        raise HTTPException(status_code=422, detail="Unsupported speaking style.")
    voices = await get_voices(gender)
    for voice in voices:
        voice["style_match"] = style_score(voice, style)
    voices.sort(key=lambda v: (v["style_match"], v["name"].lower()), reverse=True)
    start = (page - 1) * page_size
    return {
        "items": voices[start:start + page_size],
        "count": len(voices),
        "page": page,
        "page_size": page_size,
        "has_more": start + page_size < len(voices),
        "gender": gender,
        "style": style,
    }


@app.get("/api/v1/voices/{voice_id}")
async def api_voice_detail(voice_id: str, gender: Literal["all", "female", "male"] = "all",
                           x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    voices = await get_voices(gender)
    voice = next((v for v in voices if v["id"] == voice_id), None)
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found in the public catalog.")
    return voice


@app.post("/api/v1/preview")
async def api_preview(payload: TTSInput, x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    audio = await fish_tts(payload.text.strip(), payload.voice_id, payload.style,
                           payload.speed, payload.volume)
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Content-Disposition": 'inline; filename="panda-preview.mp3"',
                 "Cache-Control": "no-store"},
    )


@app.post("/api/v1/tts")
async def api_tts(payload: TTSInput, x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    if account["user_id"] is not None:
        state = access_state(account["user_id"], account["unlimited"])
        selected_model = payload.model or FISH_MODEL
        if selected_model != "s2.1-pro-free" and state["access"] == "free":
            raise HTTPException(status_code=403, detail="Paid subscription required for premium speech models.")
        try:
            enforce_api_customization(account["user_id"], payload.style, payload.model)
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
    reserved = account["user_id"] is not None
    if reserved:
        try:
            consume_request(account["user_id"], account["unlimited"])
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
    try:
        audio = await fish_tts(payload.text.strip(), payload.voice_id, payload.style,
                               payload.speed, payload.volume, payload.model)
    except Exception:
        if reserved:
            refund_request(account["user_id"])
        raise
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Content-Disposition": 'attachment; filename="panda-voice.mp3"',
                 "Cache-Control": "no-store"},
    )


@app.post("/api/v1/chat")
async def api_chat(payload: ChatInput, x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    reserved = account["user_id"] is not None
    if reserved:
        try:
            consume_request(account["user_id"], account["unlimited"])
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
    try:
        return await chat_completion(account["owner_key"], payload.message, payload.session_id, payload.language)
    except AIChatError as exc:
        if reserved:
            refund_request(account["user_id"])
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@app.post("/api/v1/agent")
async def api_agent(payload: ChatInput, x_api_key: str | None = Header(default=None)):
    """Professional tool-using agent; shares the existing account quota and session store."""
    return await api_chat(payload, x_api_key)


@app.get("/api/v1/agent/tools")
async def api_agent_tools(x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    return {
        "items": [
            {"name": "calculator", "description": "Safe arithmetic and common math functions", "available": True},
            {"name": "get_current_datetime", "description": "Current date and time with IANA timezone support", "available": True},
            {"name": "web_search", "description": "Live web research with sourced results",
             "available": bool(os.getenv("TAVILY_API_KEY", "").strip()),
             "configuration": "Set TAVILY_API_KEY in Railway variables to enable."},
        ],
        "max_tool_steps": min(max(int(os.getenv("AGENT_MAX_TOOL_STEPS", "5")), 1), 8),
        "security": "Only registered tools can execute; no shell or arbitrary code execution is exposed.",
    }


@app.post("/api/v1/chat/completions")
async def api_chat_completions(payload: ChatCompletionsInput, x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    reserved = account["user_id"] is not None
    if reserved:
        try:
            consume_request(account["user_id"], account["unlimited"])
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
    try:
        result = await chat_completion(account["owner_key"], payload.messages[-1].content, payload.session_id, payload.language)
    except AIChatError as exc:
        if reserved:
            refund_request(account["user_id"])
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {
        "id": "chatcmpl-" + result["session_id"].replace("-", "")[:24],
        "object": "chat.completion", "model": result["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result["reply"]}, "finish_reason": "stop"}],
        "usage": result.get("usage") or {}, "session_id": result["session_id"],
    }


@app.get("/api/v1/plans")
async def api_plans(x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    return {"currency": "XTR", "items": plan_catalog(), "note": "Telegram Stars plans are one-time purchases; users renew manually."}


@app.get("/api/v1/subscription")
async def api_subscription(x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    if account["user_id"] is None:
        return {"access": "unlimited", "plan": "master"}
    return {**access_state(account["user_id"], account["unlimited"]), "subscription": current_subscription(account["user_id"])}


@app.get("/api/v1/tokens")
async def api_tokens(x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    if account["user_id"] is None:
        return {"items": [], "note": "Master API credentials are not account tokens."}
    _, tokens = get_user_tokens(account["user_id"])
    return {"items": [{"id": t["id"], "name": t["name"], "prefix": t["token_prefix"],
        "created_at": t["created_at"], "expires_at": t["expires_at"], "revoked_at": t["revoked_at"],
        "last_used_at": t["last_used_at"],
        "usage_today": t["usage_count"] if t["usage_day"] == utc_now().date().isoformat() else 0} for t in tokens]}


@app.delete("/api/v1/tokens/{token_id}")
async def api_delete_token(token_id: int, x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    if account["user_id"] is None:
        raise HTTPException(status_code=403, detail="Master keys cannot manage account tokens.")
    if not revoke_user_token(account["user_id"], token_id):
        raise HTTPException(status_code=404, detail="API token not found or already deleted.")
    return {"deleted": True, "token_id": token_id}


@app.get("/api/v1/chat/sessions")
async def api_chat_sessions(limit: int = Query(default=20, ge=1, le=50),
                            x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    return {"items": list_chat_sessions(account["owner_key"], limit)}


@app.delete("/api/v1/chat/sessions/{session_id}")
async def api_delete_chat_session(session_id: str, x_api_key: str | None = Header(default=None)):
    account = await require_api_key(x_api_key)
    try:
        deleted = delete_chat_session(account["owner_key"], session_id)
    except AIChatError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat session not found.")
    return {"deleted": True, "session_id": session_id}


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 Create Voice", callback_data="create"), InlineKeyboardButton("🤖 AI Assistant", callback_data="ai_chat")],
        [InlineKeyboardButton("🎙 Voice Library", callback_data="voices"), InlineKeyboardButton("🎨 Voice Styles", callback_data="styles")],
        [InlineKeyboardButton("⭐ Upgrade Plan", callback_data="plans"), InlineKeyboardButton("🌍 Language", callback_data="language")],
        [InlineKeyboardButton("⚙️ Studio Settings", callback_data="settings"), InlineKeyboardButton("🕘 History", callback_data="history")],
        [InlineKeyboardButton("👨‍💻 Developer Center", callback_data="developers")],
        [InlineKeyboardButton("❔ Help", callback_data="help")],
    ])


def consume_customization_change(user_id: int):
    state = access_state(user_id)
    if state["access"] in ("subscription", "unlimited"):
        return
    day = utc_now().date().isoformat()
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT used FROM panda_customization_usage WHERE user_id=? AND usage_day=?", (user_id, day)).fetchone()
        used = int(row["used"]) if row else 0
        if used >= 2:
            raise ValueError("Free plan allows only 2 combined style/model changes per day. Upgrade to switch freely.")
        conn.execute("INSERT INTO panda_customization_usage(user_id,usage_day,used) VALUES(?,?,1) ON CONFLICT(user_id,usage_day) DO UPDATE SET used=used+1", (user_id, day))


def enforce_api_customization(user_id: int, style: str, model: str | None):
    chosen_model = model or FISH_MODEL
    with db_session() as conn:
        conn.execute("INSERT OR IGNORE INTO panda_api_preferences(user_id,style,model) VALUES(?,?,?)",
                     (user_id, "natural", FISH_MODEL))
        row = conn.execute("SELECT style,model FROM panda_api_preferences WHERE user_id=?", (user_id,)).fetchone()
        changed = (row["style"] != style) + (row["model"] != chosen_model)
    if changed:
        for _ in range(int(changed)):
            consume_customization_change(user_id)
        with db_session() as conn:
            conn.execute("UPDATE panda_api_preferences SET style=?,model=? WHERE user_id=?",
                         (style, chosen_model, user_id))


def selected_voice(context) -> dict | None:
    return context.user_data.get("voice")


def current_style(context) -> str:
    return context.user_data.get("style", "natural")


def home_text(context) -> str:
    voice = selected_voice(context)
    style = current_style(context)
    voice_name = voice["name"] if voice else "Not selected"
    return (
        "🐼 PANDA VOICE AI\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Professional text-to-speech studio\n\n"
        f"🎙 Voice: {voice_name}\n"
        f"🎭 Style: {STYLES[style][0]}\n"
        f"⚡ Speed: {context.user_data.get('speed', 1.0):.1f}x\n\n"
        "Choose an action below to get started.\n"
        "Free access: 2 shared Chat + Voice requests per day. Use ⭐ Plans to upgrade."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["_telegram_user_id"] = update.effective_user.id
    context.user_data.setdefault("style", "natural")
    context.user_data.setdefault("engine", "s2.1-pro-free")
    context.user_data.setdefault("speed", 1.0)
    context.user_data.setdefault("gender", "female")
    context.user_data["waiting_text"] = False
    await update.effective_message.reply_text(home_text(context), reply_markup=main_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_text"] = False
    await update.effective_message.reply_text(home_text(context), reply_markup=main_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "HOW TO USE PANDA VOICE AI\n\n"
        "1. Open Voice Library and choose Female or Male voices.\n"
        "2. Preview any voice before selecting it.\n"
        "3. Choose a speaking style and preview that style.\n"
        "4. Tap Create Speech and send your text.\n"
        "5. Receive an MP3 you can play or download.\n"
        "6. Tap AI Chat or send /chat to talk with the AI assistant.\n\n"
        "AI chat commands: /chat /newchat /chatex\n"
        "Other commands: /start /menu /plans /language /settings /cancel /help",
        reply_markup=main_keyboard(),
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(settings_text(context), reply_markup=settings_keyboard(context))


def settings_text(context) -> str:
    voice = selected_voice(context)
    return (
        "⚙️ SETTINGS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Voice: {voice['name'] if voice else 'Not selected'}\n"
        f"Style: {STYLES[current_style(context)][0]}\n"
        f"Speed: {context.user_data.get('speed', 1.0):.1f}x\n"
        f"Engine: {context.user_data.get('engine', 's2.1-pro-free')}\n"
        "Free accounts: 2 style/model changes per UTC day; subscribers can switch freely."
    )


def settings_keyboard(context) -> InlineKeyboardMarkup:
    speed = float(context.user_data.get("speed", 1.0))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖ Slower", callback_data="speed:down"),
         InlineKeyboardButton(f"{speed:.1f}x", callback_data="noop"),
         InlineKeyboardButton("➕ Faster", callback_data="speed:up")],
        [InlineKeyboardButton("🎙 Change Voice", callback_data="voices"),
         InlineKeyboardButton("🎭 Change Style", callback_data="styles")],
        [InlineKeyboardButton("🧠 Change Speech Model", callback_data="engines")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
    ])


async def show_voices(chat, context, gender: str, page: int = 0):
    try:
        voices = await get_voices(gender)
    except HTTPException as exc:
        await chat.send_message(f"Voice library unavailable: {exc.detail}", reply_markup=main_keyboard())
        return
    context.user_data["voice_page_gender"] = gender
    page_size = 8
    max_page = max(0, (len(voices) - 1) // page_size)
    page = min(max(0, page), max_page)
    context.user_data["voice_page"] = page
    if not voices:
        await chat.send_message(
            f"No publicly listed {gender} voices with clear gender tags were returned by Fish Audio. "
            "Choose the other category or try again later.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="voices")]]),
        )
        return
    rows = []
    for voice in voices[page * page_size:(page + 1) * page_size]:
        name = voice["name"][:34]
        rows.append([
            InlineKeyboardButton(f"▶ Preview · {name}", callback_data=f"vpre:{voice['id']}"),
            InlineKeyboardButton("Select", callback_data=f"vsel:{voice['id']}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"vpage:{page - 1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"vpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Gender", callback_data="voices"),
                 InlineKeyboardButton("🏠 Home", callback_data="home")])
    await chat.send_message(
        f"🎙 {gender.upper()} VOICES · Page {page + 1}/{max_page + 1}\n"
        "Preview a voice before selecting it. Voices are loaded from the public Fish Audio library.",
        reply_markup=InlineKeyboardMarkup(rows),
    )

async def show_styles(chat, context):
    rows = []
    for key, (label, _, description) in STYLES.items():
        rows.append([InlineKeyboardButton(f"{label} · {description}", callback_data=f"style:{key}")])
    rows.append([InlineKeyboardButton("▶ Preview Current Style", callback_data="stylepreview")])
    rows.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="home")])
    await chat.send_message("🎭 SPEAKING STYLES\nChoose a delivery style. Preview it with your selected voice.",
                          reply_markup=InlineKeyboardMarkup(rows))


async def send_preview(chat, context, voice: dict, style: str = "natural"):
    if user_locks[chat.id].locked():
        await chat.send_message("A generation is already running. Please wait.")
        return
    async with user_locks[chat.id]:
        status = await chat.send_message("🎧 Preparing your voice preview…")
        try:
            audio = await fish_tts(
                PREVIEW_TEXT if style == "natural" else STYLE_PREVIEW_TEXT,
                voice["id"], style, float(context.user_data.get("speed", 1.0)), 0.0,
                context.user_data.get("engine", "s2.1-pro-free")
            )
            file = BytesIO(audio)
            file.name = "panda-preview.mp3"
            await chat.send_audio(audio=file, title=f"{voice['name']} · {STYLES[style][0]}",
                                  caption=f"Voice preview · {voice['name']} · {STYLES[style][0]}")
            await status.delete()
        except HTTPException as exc:
            await status.edit_text(f"Preview failed: {exc.detail}")
        except Exception:
            log.exception("Preview generation failed")
            await status.edit_text("Preview failed. Please try again.")


async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "⭐ PANDA SUBSCRIPTION PLANS\n\n"
        "☀️ Daily · 100 Stars · 50 requests · 1 day\n"
        "📅 Weekly · 399 Stars · 500 requests · 7 days\n"
        "🗓 Monthly · 700 Stars · 2,500 requests · 30 days\n\n"
        "Requests are shared across AI Chat, Voice and API usage. Purchases use Telegram Stars; renewals are manual.",
        reply_markup=plans_keyboard(),
    )


def plans_keyboard():
    rows = []
    for plan in plan_catalog():
        label = f"{plan['name']} · {plan['stars']} ⭐" if plan["available"] else f"{plan['name']} · Price not configured"
        rows.append([InlineKeyboardButton(label, callback_data=f"plan:{plan['id']}" if plan["available"] else "noop")])
    rows.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="home")])
    return InlineKeyboardMarkup(rows)


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🌐 Choose the language for AI Chat responses:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("English", callback_data="lang:en")],
            [InlineKeyboardButton("العربية · Arabic", callback_data="lang:ar")],
            [InlineKeyboardButton("Español · Spanish", callback_data="lang:es")],
            [InlineKeyboardButton("Lingua Latina · Latin", callback_data="lang:la")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
        ]),
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    valid = validate_pending_payment(query.from_user.id, query.invoice_payload, query.currency, query.total_amount)
    await query.answer(ok=valid, error_message=None if valid else "This invoice is invalid. Please create a new one.")


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.effective_message.successful_payment
    activated = activate_paid_subscription(update.effective_user.id, payment.invoice_payload, payment.currency,
                                           payment.total_amount, payment.telegram_payment_charge_id)
    if not activated:
        log.error("Telegram Stars payment could not be reconciled user_id=%s", update.effective_user.id)
        await update.effective_message.reply_text("Payment received but activation needs support review. Keep your Telegram receipt and contact support.")
        return
    await update.effective_message.reply_text(
        f"✅ {activated['plan_id'].title()} plan activated until {activated['expires_at'][:10]} UTC.\n"
        "Renewal is manual; automatic recurring billing is not enabled.", reply_markup=main_keyboard())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat = query.message.chat
    if data == "home":
        context.user_data["waiting_text"] = False
        await query.message.reply_text(home_text(context), reply_markup=main_keyboard())
    elif data == "plans":
        await query.message.reply_text("⭐ Choose a subscription plan:", reply_markup=plans_keyboard())
    elif data.startswith("plan:"):
        try:
            invoice = create_pending_payment(query.from_user.id, data.split(":", 1)[1])
            await context.bot.send_invoice(chat_id=query.message.chat_id, title=f"PANDA {invoice['name']} plan",
                description=f"{invoice['days']} days and {invoice['requests']} requests of Panda Voice AI access.", payload=invoice["payload"],
                provider_token="", currency="XTR", prices=[LabeledPrice(label=f"{invoice['name']} access", amount=invoice["stars"])])
        except ValueError as exc:
            await query.message.reply_text(str(exc), reply_markup=plans_keyboard())
    elif data == "language":
        await language_command(update, context)
    elif data.startswith("lang:"):
        language = data.split(":", 1)[1]
        if language in ("en", "ar", "es", "la"):
            context.user_data["ai_language"] = language
            names = {"en": "English", "ar": "Arabic", "es": "Spanish", "la": "Latin"}
            await query.message.reply_text(f"✅ AI Chat language set to {names[language]}.", reply_markup=main_keyboard())
    elif data == "ai_chat":
        context.user_data["waiting_text"] = False
        context.user_data["ai_chat_active"] = True
        await query.message.reply_text(
            "🤖 PANDA AI CHAT is ready. Send a message to start.\n"
            "Use /newchat to clear this conversation or /chatex to exit.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆕 New conversation", callback_data="ai_new")],
                [InlineKeyboardButton("⬅️ Exit AI Chat", callback_data="ai_exit")],
            ]),
        )
    elif data == "ai_new":
        context.user_data["ai_chat_active"] = True
        context.user_data["ai_chat_session_id"] = None
        await query.message.reply_text("🆕 New AI conversation started. Send your message.")
    elif data == "ai_exit":
        context.user_data["ai_chat_active"] = False
        await query.message.reply_text("AI Chat closed.", reply_markup=main_keyboard())
    elif data == "create":
        if not selected_voice(context):
            await query.message.reply_text("Please choose a voice first.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open Voice Library", callback_data="voices")]]))
            return
        context.user_data["waiting_text"] = True
        await query.message.reply_text(
            f"✍️ SEND YOUR TEXT\nMaximum length: {MAX_TEXT_CHARS:,} characters.\n"
            "Send /cancel to stop.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel")]]),
        )
    elif data == "cancel":
        context.user_data["waiting_text"] = False
        await query.message.reply_text("Text entry cancelled.", reply_markup=main_keyboard())
    elif data == "voices":
        await query.message.reply_text("Choose a voice category:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("♀ Female Voices", callback_data="gender:female")],
                [InlineKeyboardButton("♂ Male Voices", callback_data="gender:male")],
                [InlineKeyboardButton("🌐 All Voices", callback_data="gender:all")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
            ]))
    elif data.startswith("gender:"):
        await show_voices(chat, context, data.split(":", 1)[1], 0)
    elif data.startswith("vpage:"):
        await show_voices(chat, context, context.user_data.get("voice_page_gender", "all"),
                          int(data.split(":", 1)[1]))
    elif data.startswith("vpre:") or data.startswith("vsel:"):
        action, voice_id = data.split(":", 1)
        gender = context.user_data.get("voice_page_gender", "all")
        try:
            candidates = await get_voices(gender)
            voice = next((v for v in candidates if v["id"] == voice_id), None)
            if not voice:
                raise HTTPException(status_code=404, detail="That voice is no longer available.")
            if action == "vsel":
                context.user_data["voice"] = voice
                context.user_data["gender"] = gender
                style = current_style(context)
                preferred = best_voice(candidates, style)
                if preferred and style_score(preferred, style) > style_score(voice, style):
                    context.user_data["voice"] = preferred
                    voice = preferred
                await query.message.reply_text(
                    f"✅ Selected voice: {voice['name']}\n"
                    f"Speaking style: {STYLES[current_style(context)][0]}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎭 Choose Style", callback_data="styles")],
                        [InlineKeyboardButton("▶ Preview Voice", callback_data=f"vpre:{voice['id']}")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
                    ]),
                )
            else:
                await send_preview(chat, context, voice, "natural")
        except HTTPException as exc:
            await query.message.reply_text(exc.detail)
    elif data == "styles":
        await show_styles(chat, context)
    elif data == "engines":
        await query.message.reply_text("🧠 Choose a speech model:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("S2.1 Pro Free", callback_data="engine:s2.1-pro-free")],
            [InlineKeyboardButton("S2.1 Pro", callback_data="engine:s2.1-pro")],
            [InlineKeyboardButton("S2 Pro", callback_data="engine:s2-pro")],
            [InlineKeyboardButton("S1 Legacy", callback_data="engine:s1")],
            [InlineKeyboardButton("⬅️ Settings", callback_data="settings")],
        ]))
    elif data.startswith("engine:"):
        engine = data.split(":", 1)[1]
        if engine not in {"s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"}:
            return
        if engine != "s2.1-pro-free" and access_state(query.from_user.id)["access"] == "free":
            await query.message.reply_text("Premium speech models require an active subscription.", reply_markup=plans_keyboard())
            return
        if engine != context.user_data.get("engine", "s2.1-pro-free"):
            try:
                consume_customization_change(query.from_user.id)
            except ValueError as exc:
                await query.message.reply_text(str(exc), reply_markup=plans_keyboard())
                return
        context.user_data["engine"] = engine
        await query.message.reply_text(f"✅ Speech model set to {engine}.", reply_markup=settings_keyboard(context))
    elif data.startswith("style:"):
        style = data.split(":", 1)[1]
        if style not in STYLES:
            return
        if style != current_style(context):
            try:
                consume_customization_change(query.from_user.id)
            except ValueError as exc:
                await query.message.reply_text(str(exc), reply_markup=plans_keyboard())
                return
        context.user_data["style"] = style
        voice = selected_voice(context)
        gender = context.user_data.get("gender", "female")
        try:
            candidates = await get_voices(gender)
            preferred = best_voice(candidates, style)
            if preferred and (not voice or style_score(preferred, style) > style_score(voice, style)):
                context.user_data["voice"] = preferred
                voice = preferred
        except HTTPException:
            pass
        await query.message.reply_text(
            f"✅ Style selected: {STYLES[style][0]}\n"
            f"Voice: {voice['name'] if voice else 'Choose a voice'}\n"
            "You can preview this style before generating your speech.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶ Preview This Style", callback_data="stylepreview")],
                [InlineKeyboardButton("🎙 Change Voice", callback_data="voices")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
            ]),
        )
    elif data == "stylepreview":
        voice = selected_voice(context)
        if not voice:
            await query.message.reply_text("Choose a voice first.", reply_markup=main_keyboard())
        else:
            await send_preview(chat, context, voice, current_style(context))
    elif data == "developers":
        await developers_screen(query.message, query.from_user.id)
    elif data == "create_token":
        context.user_data["waiting_api_name"] = True
        await query.message.reply_text("Enter an API token name (for example: My Android App). Up to 5 active tokens are allowed; the secret is shown once.")
    elif data.startswith("token_days:"):
        days = int(data.split(":", 1)[1])
        name = context.user_data.pop("pending_api_name", "My App")
        try:
            token = create_user_token(query.from_user.id, name, days)
            expiry = "Never expires" if token["expires_at"] is None else token["expires_at"][:10]
            sample = (
                "curl -X POST https://panda-voice-ai-production.up.railway.app/api/v1/chat \\\n"
                "  -H 'X-API-Key: " + token["token"] + "' \\\n"
                "  -H 'Content-Type: application/json' \\\n"
                "  -d '{\"message\":\"Hello Panda AI\",\"language\":\"en\"}'")
            await query.message.reply_text(
                "✅ API TOKEN CREATED\n\n"
                f"Name: {token['name']}\nExpires: {expiry}\n"
                f"Account allowance: {'Unlimited' if token['unlimited'] else 'Shared free/subscription quota'}\n\n"
                "⚠️ Copy this secret now. It will not be shown again.\n"
                f"<code>{token['token']}</code>\n\n"
                "<b>BASE URL</b>\nhttps://panda-voice-ai-production.up.railway.app\n\n"
                "<b>VOICE API</b>\nGET /api/v1/voices?gender=all\nGET /api/v1/voices/{voice_id}\nGET /api/v1/styles\nGET /api/v1/engines\nPOST /api/v1/preview\nPOST /api/v1/tts\n\n"
                "<b>AI CHAT API</b>\nPOST /api/v1/chat\nPOST /api/v1/chat/completions\nGET /api/v1/chat/sessions\nDELETE /api/v1/chat/sessions/{session_id}\n\n"
                "<b>DEVELOPER / ACCOUNT</b>\nGET /api/v1/account\nGET /api/v1/limits\nGET /api/v1/plans\nGET /api/v1/subscription\nGET /api/v1/tokens\nDELETE /api/v1/tokens/{token_id}\n\n"
                "<b>INTERACTIVE DOCS</b>\nhttps://panda-voice-ai-production.up.railway.app/docs\n"
                "https://github.com/surafel5509-del/Panda-Voice-Ai/blob/main/API.md\n\n"
                "<b>AI CHAT CURL EXAMPLE</b>\n<pre>" + sample.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;") + "</pre>",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗂 My API Tokens", callback_data="my_tokens")],
                    [InlineKeyboardButton("👨‍💻 Developers", callback_data="developers")]]))
        except ValueError as exc:
            await query.message.reply_text(str(exc), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🗂 My API Tokens", callback_data="my_tokens")],
                 [InlineKeyboardButton("⬅️ Developers", callback_data="developers")]]))
    elif data == "my_tokens":
        unlimited, tokens = get_user_tokens(query.from_user.id)
        lines = ["🗂 YOUR API TOKENS", f"Access: {'UNLIMITED' if unlimited else str(DAILY_REQUEST_LIMIT) + ' requests/day'}"]
        buttons = []
        for item in tokens:
            state = "REVOKED" if item["revoked_at"] else (
                "EXPIRED" if item["expires_at"] and datetime.fromisoformat(item["expires_at"]) <= utc_now() else "ACTIVE")
            expiry = "Never" if item["expires_at"] is None else item["expires_at"][:10]
            lines.append(f"\n#{item['id']} · {item['name']} · {state}\n{item['token_prefix']}… · Expires: {expiry}\nUsage today: {item['usage_count'] if item['usage_day'] == utc_now().date().isoformat() else 0}")
            if state == "ACTIVE":
                buttons.append([InlineKeyboardButton(f"🗑 Delete #{item['id']}", callback_data=f"revoke:{item['id']}")])
        buttons.append([InlineKeyboardButton("🔑 Create API Token", callback_data="create_token")])
        buttons.append([InlineKeyboardButton("⬅️ Developers", callback_data="developers")])
        await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("revoke:"):
        token_id = int(data.split(":", 1)[1])
        ok = revoke_user_token(query.from_user.id, token_id)
        await query.message.reply_text("Token deleted. Any app using it has lost access immediately." if ok else "Token not found or already deleted.",
                                       reply_markup=InlineKeyboardMarkup([
                                           [InlineKeyboardButton("🗂 My API Tokens", callback_data="my_tokens")],
                                           [InlineKeyboardButton("⬅️ Developers", callback_data="developers")]]))
    elif data == "unlimited":
        context.user_data["waiting_unlimited_code"] = True
        await query.message.reply_text("Enter your unlimited access code to unlock unlimited API usage.")
    elif data == "settings":
        await query.message.reply_text(settings_text(context), reply_markup=settings_keyboard(context))
    elif data.startswith("speed:"):
        speed = float(context.user_data.get("speed", 1.0))
        speed += -0.1 if data.endswith("down") else 0.1
        context.user_data["speed"] = round(min(2.0, max(0.5, speed)), 1)
        await query.message.reply_text(settings_text(context), reply_markup=settings_keyboard(context))
    elif data == "history":
        items = list(reversed(recent_history[query.from_user.id]))
        message = "🕘 RECENT GENERATIONS\n\n" + ("\n".join(f"• {x[:80]}" for x in items) if items else "No recent generations yet.")
        await query.message.reply_text(message, reply_markup=main_keyboard())
    elif data == "help":
        await help_command(update, context)
    elif data == "noop":
        return


async def developers_screen(message, user_id: int):
    unlimited, tokens = get_user_tokens(user_id)
    active = sum(1 for t in tokens if not t["revoked_at"] and (not t["expires_at"] or datetime.fromisoformat(t["expires_at"]) > utc_now()))
    cap = int(os.getenv("PANDA_MAX_ACTIVE_TOKENS", "5"))
    await message.reply_text(
        "👨‍💻 PANDA DEVELOPER CENTER\n━━━━━━━━━━━━━━━━━━\n"
        "Create API credentials, integrate AI Chat and Voice, and manage access securely.\n\n"
        f"Active tokens: {active}/{cap}\n"
        f"Account access: {'UNLIMITED' if unlimited else 'Free / subscription quota'}\n\n"
        "Base URL: https://panda-voice-ai-production.up.railway.app\n"
        "Swagger: https://panda-voice-ai-production.up.railway.app/docs\n"
        "Full guide: https://github.com/surafel5509-del/Panda-Voice-Ai/blob/main/API.md",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Create API Token", callback_data="create_token")],
            [InlineKeyboardButton("🗂 Manage / Delete Tokens", callback_data="my_tokens")],
            [InlineKeyboardButton("📖 Open API Docs", url="https://panda-voice-ai-production.up.railway.app/docs")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
        ]))


async def developers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await developers_screen(update.effective_message, update.effective_user.id)


async def ai_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_text"] = False
    context.user_data["ai_chat_active"] = True
    await update.effective_message.reply_text(
        "🤖 PANDA AI CHAT is ready. Send a message to start.\n"
        "Use /newchat to clear this conversation or /chatex to exit.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 New conversation", callback_data="ai_new")],
            [InlineKeyboardButton("⬅️ Exit AI Chat", callback_data="ai_exit")],
        ]),
    )


async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ai_chat_active"] = True
    context.user_data["ai_chat_session_id"] = None
    await update.effective_message.reply_text("🆕 New AI conversation started. Send your message.")


async def exit_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ai_chat_active"] = False
    await update.effective_message.reply_text("AI Chat closed.", reply_markup=main_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_text"] = False
    await update.effective_message.reply_text("Text entry cancelled.", reply_markup=main_keyboard())

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["_telegram_user_id"] = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if context.user_data.get("ai_chat_active"):
        if not text:
            return
        status = await update.effective_message.reply_text("🤖 Thinking…")
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
            consume_request(update.effective_user.id)
            result = await chat_completion(
                "telegram-user:" + str(update.effective_user.id), text,
                context.user_data.get("ai_chat_session_id"), context.user_data.get("ai_language"),
            )
            context.user_data["ai_chat_session_id"] = result["session_id"]
            await status.delete()
            answer = result["reply"]
            for offset in range(0, len(answer), 4000):
                await update.effective_message.reply_text(answer[offset:offset + 4000])
        except AIChatError as exc:
            refund_request(update.effective_user.id)
            await status.edit_text(str(exc))
        except ValueError as exc:
            await status.edit_text(str(exc))
        except Exception:
            refund_request(update.effective_user.id)
            log.exception("Telegram AI Chat request failed")
            await status.edit_text("AI Chat failed unexpectedly. Please try again.")
        return
    if context.user_data.pop("waiting_api_name", False):
        if not text or len(text) > 48:
            await update.effective_message.reply_text("Name must be between 1 and 48 characters. Tap Create API Token again.")
            return
        context.user_data["pending_api_name"] = text
        unlimited, _ = get_user_tokens(update.effective_user.id)
        choices = [
            [InlineKeyboardButton("7 days", callback_data="token_days:7"),
             InlineKeyboardButton("30 days", callback_data="token_days:30")],
            [InlineKeyboardButton("90 days", callback_data="token_days:90"),
             InlineKeyboardButton("365 days", callback_data="token_days:365")],
        ]
        if unlimited:
            choices.append([InlineKeyboardButton("♾ Never expires", callback_data="token_days:0")])
        choices.append([InlineKeyboardButton("⬅️ Cancel", callback_data="developers")])
        await update.effective_message.reply_text("Choose this API token's validity period:",
                                                   reply_markup=InlineKeyboardMarkup(choices))
        return
    if context.user_data.pop("waiting_unlimited_code", False):
        if PANDA_UNLIMITED_CODE and secrets.compare_digest(text, PANDA_UNLIMITED_CODE):
            unlock_user_unlimited(update.effective_user.id)
            await update.effective_message.reply_text(
                "♾ Unlimited API access unlocked for your account. You can now create tokens without an expiry date.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👨‍💻 Developers", callback_data="developers")]]))
        else:
            await update.effective_message.reply_text("Invalid code. Unlimited access was not enabled.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👨‍💻 Developers", callback_data="developers")]]))
        return
    if not context.user_data.get("waiting_text"):
        await update.effective_message.reply_text("Choose Create Speech to start.", reply_markup=main_keyboard())
        return
    if not text:
        return
    if len(text) > MAX_TEXT_CHARS:
        await update.effective_message.reply_text(f"Text is too long. Maximum: {MAX_TEXT_CHARS:,} characters.")
        return
    voice = selected_voice(context)
    if not voice:
        context.user_data["waiting_text"] = False
        await update.effective_message.reply_text("Please select a voice first.", reply_markup=main_keyboard())
        return
    user_id = update.effective_user.id
    if user_locks[user_id].locked():
        await update.effective_message.reply_text("A generation is already running. Please wait.")
        return
    context.user_data["waiting_text"] = False
    style = current_style(context)
    speed = float(context.user_data.get("speed", 1.0))
    async with user_locks[user_id]:
        status = await update.effective_message.reply_text("🎧 Generating your speech…")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
        try:
            consume_request(user_id)
            audio = await fish_tts(text, voice["id"], style, speed, 0.0, context.user_data.get("engine", "s2.1-pro-free"))
            file = BytesIO(audio)
            file.name = "panda-voice.mp3"
            await update.effective_message.reply_audio(
                audio=file, filename="panda-voice.mp3", title="Panda Voice AI",
                caption=f"🐼 PANDA VOICE AI\n{voice['name']} · {STYLES[style][0]} · {speed:.1f}x",
            )
            recent_history[user_id].append(text)
            await status.delete()
            await update.effective_message.reply_text("✅ Your MP3 is ready. Play it or download it below.",
                                                       reply_markup=main_keyboard())
        except HTTPException as exc:
            refund_request(user_id)
            await status.edit_text(f"Generation failed: {exc.detail}")
            context.user_data["waiting_text"] = True
        except ValueError as exc:
            await status.edit_text(str(exc))
            context.user_data["waiting_text"] = True
        except Exception:
            refund_request(user_id)
            log.exception("Telegram TTS generation failed")
            await status.edit_text("Something went wrong. Please try again.")
            context.user_data["waiting_text"] = True


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
