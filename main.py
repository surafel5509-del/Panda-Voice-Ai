import asyncio
import logging
import os
import tempfile
from collections import defaultdict, deque
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("panda-voice-ai")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FISH_API_KEY = os.getenv("FISH_API_KEY", "").strip()
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free").strip()
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "3500"))
MAX_PARALLEL_PER_USER = 1

# These public voice IDs are taken from Fish Audio's official developer quickstart.
VOICES = {
    "voice_a": ("Voice A · Default", "933563129e564b19a115bedd57b7406a"),
    "voice_b": ("Voice B", "ca3007f96ae7499ab87d27ea3599956a"),
    "voice_c": ("Voice C", "9a9cf47702da476aa4629e2506d4a857"),
}
STYLES = {
    "normal": ("🎙 መደበኛ / Normal", ""),
    "story": ("📖 ታሪክ / Storytelling", "[warm, expressive storytelling] "),
    "podcast": ("🎧 Podcast", "[clear, engaging podcast delivery] "),
    "voiceover": ("🎬 Voice Over", "[confident, polished voice-over delivery] "),
    "audiobook": ("📚 Audiobook", "[calm, immersive audiobook narration] "),
    "news": ("📰 News", "[clear, professional newsreader delivery] "),
    "documentary": ("🌍 Documentary", "[measured, cinematic documentary narration] "),
    "meditation": ("🧘 Meditation", "[gentle, slow, soothing delivery] "),
    "excited": ("⚡ Energetic", "[energetic, enthusiastic delivery] "),
    "whisper": ("🤫 Soft / Whisper", "[soft, intimate, gentle delivery] "),
}

user_locks = defaultdict(asyncio.Lock)
recent_audio = defaultdict(lambda: deque(maxlen=8))


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ ድምፅ ፍጠር / Create voice", callback_data="make")],
        [InlineKeyboardButton("🎤 ድምፅ ምረጥ / Select voice", callback_data="voices"),
         InlineKeyboardButton("🎭 ዘይቤ / Style", callback_data="styles")],
        [InlineKeyboardButton("🗂 የቅርብ ታሪክ / Recent", callback_data="history"),
         InlineKeyboardButton("⚙️ ቅንብር / Settings", callback_data="settings")],
        [InlineKeyboardButton("ℹ️ እገዛ / Help", callback_data="help")],
    ])


def settings_text(context):
    voice_key = context.user_data.get("voice", "voice_a")
    style_key = context.user_data.get("style", "normal")
    return (
        "⚙️ **የአሁኑ ቅንብር**\n"
        f"🎤 Voice: {VOICES[voice_key][0]}\n"
        f"🎭 Style: {STYLES[style_key][0]}\n"
        f"🧠 Model: {FISH_MODEL}\n"
        f"📝 ከፍተኛ የጽሑፍ ርዝመት: {MAX_TEXT_CHARS} ቁምፊ"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("voice", "voice_a")
    context.user_data.setdefault("style", "normal")
    context.user_data["waiting_text"] = False
    await update.effective_message.reply_text(
        "🐼 **PANDA VOICE AI**\n\n"
        "በFish Audio AI ጽሑፍህን ወደ ተፈጥሯዊ ድምፅ ቀይር!\n"
        "ድምፅና ዘይቤ ምረጥ፣ ጽሑፍ ላክ፣ MP3 አውርድ።",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📌 **እንዴት ይጠቀሙ?**\n"
        "1. Create voice ይጫኑ።\n"
        "2. Voice እና Style ይምረጡ።\n"
        "3. የሚነበበውን ጽሑፍ ይላኩ።\n"
        "4. ቦቱ MP3 ይፈጥርልዎታል።\n\n"
        "ትዕዛዞች: /start /settings /cancel /help",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        settings_text(context), parse_mode="Markdown", reply_markup=main_menu()
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_text"] = False
    await update.effective_message.reply_text("ተሰርዟል። ከታች ይምረጡ።", reply_markup=main_menu())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "make":
        context.user_data["waiting_text"] = True
        await query.message.reply_text(
            f"✍️ አሁን የሚቀየረውን ጽሑፍ ይላኩ።\n"
            f"ከፍተኛ ርዝመት: {MAX_TEXT_CHARS} ቁምፊ።\n"
            "ለመመለስ /cancel ይጻፉ።"
        )
    elif data == "voices":
        rows = [[InlineKeyboardButton(label, callback_data=f"voice:{key}")]
                for key, (label, _) in VOICES.items()]
        rows.append([InlineKeyboardButton("⬅️ ወደ ምናሌ", callback_data="home")])
        await query.message.reply_text("🎤 **የድምፅ ሞዴል ይምረጡ**", parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("voice:"):
        key = data.split(":", 1)[1]
        if key in VOICES:
            context.user_data["voice"] = key
        await query.message.reply_text(f"✅ የተመረጠው: {VOICES[key][0]}", reply_markup=main_menu())
    elif data == "styles":
        rows = [[InlineKeyboardButton(label, callback_data=f"style:{key}")]
                for key, (label, _) in STYLES.items()]
        rows.append([InlineKeyboardButton("⬅️ ወደ ምናሌ", callback_data="home")])
        await query.message.reply_text("🎭 **የንግግር ዘይቤ ይምረጡ**", parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("style:"):
        key = data.split(":", 1)[1]
        if key in STYLES:
            context.user_data["style"] = key
        await query.message.reply_text(f"✅ የተመረጠው: {STYLES[key][0]}", reply_markup=main_menu())
    elif data == "settings":
        await query.message.reply_text(settings_text(context), parse_mode="Markdown",
                                       reply_markup=main_menu())
    elif data == "history":
        items = list(recent_audio[query.from_user.id])
        if not items:
            msg = "ገና የተፈጠረ ድምፅ የለም።"
        else:
            msg = "🗂 **የቅርብ ጊዜ ድምፆች**\n" + "\n".join(
                f"{i+1}. {item[:90]}" for i, item in enumerate(reversed(items))
            )
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu())
    elif data == "help":
        await query.message.reply_text(
            "Create voice → ጽሑፍ ላክ → MP3 ተቀበል።\n"
            "ድምፅና ዘይቤ ከመፍጠርህ በፊት መቀየር ትችላለህ።",
            reply_markup=main_menu()
        )
    elif data == "home":
        await query.message.reply_text("🐼 PANDA VOICE AI", reply_markup=main_menu())


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if not context.user_data.get("waiting_text"):
        await update.effective_message.reply_text(
            "ጽሑፍ ለመቀየር Create voice ይጫኑ።", reply_markup=main_menu()
        )
        return
    if not text:
        return
    if len(text) > MAX_TEXT_CHARS:
        await update.effective_message.reply_text(
            f"ጽሑፉ በጣም ረጅም ነው። እስከ {MAX_TEXT_CHARS} ቁምፊ ድረስ ይላኩ።"
        )
        return
    if not FISH_API_KEY:
        await update.effective_message.reply_text(
            "⚠️ Fish Audio API Key አልተዋቀረም። Railway Variables ውስጥ FISH_API_KEY ያስገቡ።"
        )
        return
    if user_locks[user_id].locked():
        await update.effective_message.reply_text("⏳ አንድ ድምፅ እየተፈጠረ ነው፤ እባክዎ ይጠብቁ።")
        return

    context.user_data["waiting_text"] = False
    voice_key = context.user_data.get("voice", "voice_a")
    style_key = context.user_data.get("style", "normal")
    voice_label, reference_id = VOICES[voice_key]
    style_label, style_prefix = STYLES[style_key]
    spoken_text = style_prefix + text

    async with user_locks[user_id]:
        status = await update.effective_message.reply_text("🎧 ድምፅ እየተፈጠረ ነው... እባክዎ ይጠብቁ።")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE)
        try:
            headers = {
                "Authorization": f"Bearer {FISH_API_KEY}",
                "Content-Type": "application/json",
                "model": FISH_MODEL,
            }
            payload = {
                "text": spoken_text,
                "reference_id": reference_id,
                "format": "mp3",
                "latency": "normal",
            }
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
                response = await client.post("https://api.fish.audio/v1/tts",
                                             headers=headers, json=payload)
            if response.status_code != 200:
                detail = response.text[:600]
                log.warning("Fish Audio returned %s: %s", response.status_code, detail)
                if response.status_code == 401:
                    msg = "API Key ትክክል አይደለም። FISH_API_KEY ያረጋግጡ።"
                elif response.status_code == 402:
                    msg = "የAPI ክሬዲት አልበቃም። FISH_MODEL=s2.1-pro-free መሆኑን ያረጋግጡ።"
                elif response.status_code == 429:
                    msg = "የጥያቄ ገደብ ደርሷል። ትንሽ ቆይተው ይሞክሩ።"
                else:
                    msg = f"ድምፅ መፍጠር አልተሳካም (HTTP {response.status_code})። እንደገና ይሞክሩ።"
                await status.edit_text(msg, reply_markup=main_menu())
                context.user_data["waiting_text"] = True
                return
            if not response.content:
                raise RuntimeError("Fish Audio returned an empty audio response")

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp:
                temp.write(response.content)
                audio_path = Path(temp.name)
            try:
                caption = f"🐼 PANDA VOICE AI\n🎤 {voice_label}\n🎭 {style_label}"
                with audio_path.open("rb") as audio_file:
                    await update.effective_message.reply_audio(
                        audio=audio_file,
                        filename="panda-voice.mp3",
                        title="Panda Voice AI",
                        caption=caption[:1024],
                    )
                recent_audio[user_id].append(text)
                await status.delete()
                await update.effective_message.reply_text(
                    "✅ ድምፅ ተፈጥሯል! ከላይ ማዳመጥ ወይም MP3 ማውረድ ይችላሉ።",
                    reply_markup=main_menu(),
                )
            finally:
                audio_path.unlink(missing_ok=True)
        except httpx.TimeoutException:
            await status.edit_text("⏱️ ጊዜ አልቋል። ጽሑፉን አጭር አድርገው ይሞክሩ።",
                                   reply_markup=main_menu())
            context.user_data["waiting_text"] = True
        except Exception:
            log.exception("Audio generation failed")
            await status.edit_text("❌ ስህተት ተፈጥሯል። እባክዎ በኋላ ይሞክሩ።",
                                   reply_markup=main_menu())
            context.user_data["waiting_text"] = True


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, generate))
    log.info("PANDA VOICE AI bot starting (model=%s)", FISH_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
