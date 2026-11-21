import asyncio
import logging
import os
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
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
FISH_BASE = "https://api.fish.audio"
MODEL_PAGE_SIZE = 100

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


def fish_headers(model: str | None = None) -> dict:
    if not FISH_API_KEY:
        raise HTTPException(status_code=503, detail="Speech service is not configured.")
    headers = {"Authorization": f"Bearer {FISH_API_KEY}", "Content-Type": "application/json"}
    if model:
        headers["model"] = model
    return headers


async def fish_tts(text: str, voice_id: str, style: str = "natural", speed: float = 1.0,
                   volume: float = 0.0) -> bytes:
    if style not in STYLES:
        raise HTTPException(status_code=422, detail="Unsupported speaking style.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", voice_id):
        raise HTTPException(status_code=422, detail="Invalid voice identifier.")
    label, prefix, _ = STYLES[style]
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
                                     headers=fish_headers(FISH_MODEL), json=payload) as res:
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
    tags = " ".join(str(x) for x in (item.get("tags") or [])).lower()
    title = str(item.get("title") or "").lower()
    haystack = f"{tags} {title}"
    female = any(x in haystack for x in ("female", "woman", "women", "girl", "feminine"))
    male = any(x in haystack for x in ("male", "man", "men", "boy", "masculine"))
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
        params = {"page_size": MODEL_PAGE_SIZE, "page_number": 1, "sort_by": "score"}
        if gender in ("female", "male"):
            params["tag"] = gender
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(f"{FISH_BASE}/model", headers=fish_headers(), params=params)
            if res.status_code != 200:
                log.warning("Fish model library failed status=%s", res.status_code)
                raise HTTPException(status_code=502, detail="Could not load the voice library.")
            items = res.json().get("items", [])
        except (ValueError, httpx.HTTPError):
            log.exception("Could not parse Fish model library response")
            raise HTTPException(status_code=502, detail="Could not load the voice library.")
        result = []
        seen = set()
        for item in items:
            if item.get("type") != "tts" or item.get("state") != "trained":
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
        return result


def best_voice(voices: list[dict], style: str) -> dict | None:
    if not voices:
        return None
    return max(voices, key=lambda v: (style_score(v, style), len(v.get("languages", []))))


async def require_api_key(x_api_key: str | None):
    if not PANDA_API_KEY:
        raise HTTPException(status_code=503, detail="Application API access is not enabled.")
    import secrets
    if not x_api_key or not secrets.compare_digest(x_api_key, PANDA_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key.")


class TTSInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    voice_id: str = Field(min_length=8, max_length=80)
    style: Literal["natural", "story", "podcast", "voiceover", "audiobook", "news",
                   "documentary", "meditation", "energetic", "whisper", "empathetic",
                   "commercial"] = "natural"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: float = Field(default=0.0, ge=-20.0, le=20.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    if BOT_TOKEN:
        telegram_app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("settings", settings_command))
        telegram_app.add_handler(CommandHandler("cancel", cancel))
        telegram_app.add_handler(CallbackQueryHandler(on_button))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        await telegram_app.initialize()
        if telegram_app.updater:
            await telegram_app.updater.start_polling(drop_pending_updates=True)
        await telegram_app.start()
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
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "PANDA VOICE AI"}


@app.get("/api/v1/voices")
async def api_voices(gender: Literal["all", "female", "male"] = "all",
                     style: str = "natural", x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    if style not in STYLES:
        raise HTTPException(status_code=422, detail="Unsupported speaking style.")
    voices = await get_voices(gender)
    for voice in voices:
        voice["style_match"] = style_score(voice, style)
    voices.sort(key=lambda v: (v["style_match"], v["name"].lower()), reverse=True)
    return {"items": voices, "count": len(voices), "gender": gender, "style": style}


@app.post("/api/v1/tts")
async def api_tts(payload: TTSInput, x_api_key: str | None = Header(default=None)):
    await require_api_key(x_api_key)
    audio = await fish_tts(payload.text.strip(), payload.voice_id, payload.style,
                           payload.speed, payload.volume)
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Content-Disposition": 'attachment; filename="panda-voice.mp3"',
                 "Cache-Control": "no-store"},
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ Create Speech", callback_data="create")],
        [InlineKeyboardButton("🎙 Voice Library", callback_data="voices"),
         InlineKeyboardButton("🎭 Speaking Styles", callback_data="styles")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
         InlineKeyboardButton("🕘 Recent", callback_data="history")],
        [InlineKeyboardButton("❔ Help", callback_data="help")],
    ])


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
        "Choose an action below to get started."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("style", "natural")
    context.user_data.setdefault("speed", 1.0)
    context.user_data.setdefault("gender", "female")
    context.user_data["waiting_text"] = False
    await update.effective_message.reply_text(home_text(context), reply_markup=main_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "HOW TO USE PANDA VOICE AI\n\n"
        "1. Open Voice Library and choose Female or Male voices.\n"
        "2. Preview any voice before selecting it.\n"
        "3. Choose a speaking style and preview that style.\n"
        "4. Tap Create Speech and send your text.\n"
        "5. Receive an MP3 you can play or download.\n\n"
        "Commands: /start /settings /cancel /help",
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
        f"Engine: Fish Audio S2.1 Pro Free"
    )


def settings_keyboard(context) -> InlineKeyboardMarkup:
    speed = float(context.user_data.get("speed", 1.0))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖ Slower", callback_data="speed:down"),
         InlineKeyboardButton(f"{speed:.1f}x", callback_data="noop"),
         InlineKeyboardButton("➕ Faster", callback_data="speed:up")],
        [InlineKeyboardButton("🎙 Change Voice", callback_data="voices"),
         InlineKeyboardButton("🎭 Change Style", callback_data="styles")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="home")],
    ])


async def show_voices(chat, context, gender: str, page: int = 0):
    try:
        voices = await get_voices(gender)
    except HTTPException as exc:
        await chat.reply_text(f"Voice library unavailable: {exc.detail}", reply_markup=main_keyboard())
        return
    context.user_data["voice_page_gender"] = gender
    page_size = 8
    max_page = max(0, (len(voices) - 1) // page_size)
    page = min(max(0, page), max_page)
    context.user_data["voice_page"] = page
    if not voices:
        await chat.reply_text(
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
    await chat.reply_text(
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
    await chat.reply_text("🎭 SPEAKING STYLES\nChoose a delivery style. Preview it with your selected voice.",
                          reply_markup=InlineKeyboardMarkup(rows))


async def send_preview(chat, context, voice: dict, style: str = "natural"):
    if user_locks[chat.id].locked():
        await chat.reply_text("A generation is already running. Please wait.")
        return
    async with user_locks[chat.id]:
        status = await chat.send_message("🎧 Preparing your voice preview…")
        try:
            audio = await fish_tts(
                PREVIEW_TEXT if style == "natural" else STYLE_PREVIEW_TEXT,
                voice["id"], style, float(context.user_data.get("speed", 1.0)), 0.0
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


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat = query.message.chat
    if data == "home":
        context.user_data["waiting_text"] = False
        await query.message.reply_text(home_text(context), reply_markup=main_keyboard())
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
    elif data.startswith("style:"):
        style = data.split(":", 1)[1]
        if style not in STYLES:
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


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_text"] = False
    await update.effective_message.reply_text("Text entry cancelled.", reply_markup=main_keyboard())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
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
            audio = await fish_tts(text, voice["id"], style, speed, 0.0)
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
            await status.edit_text(f"Generation failed: {exc.detail}")
            context.user_data["waiting_text"] = True
        except Exception:
            log.exception("Telegram TTS generation failed")
            await status.edit_text("Something went wrong. Please try again.")
            context.user_data["waiting_text"] = True


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
