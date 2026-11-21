# 🐼 PANDA VOICE AI

A Telegram text-to-speech bot powered by the official Fish Audio TTS API.

## Features
- Amharic-first Telegram menus
- Fish Audio voice selection using `reference_id`
- Storytelling, Normal, Podcast, Voice-over, Audiobook, News, Documentary, Meditation, Energetic and Soft presets
- MP3 generation, in-chat playback and downloadable audio
- Per-user settings and recent-generation history
- Friendly API error handling and configurable text limit
- Railway-ready deployment

## Deploy on Railway

1. Deploy this GitHub repository from the `main` branch.
2. Add these service variables in Railway → Variables:
   - `TELEGRAM_BOT_TOKEN`: token from Telegram's @BotFather
   - `FISH_API_KEY`: API key from [Fish Audio Developers](https://fish.audio/developers/)
   - `FISH_MODEL`: `s2.1-pro-free` (default)
3. Redeploy. The start command is `python main.py`.

Never commit API keys or bot tokens to GitHub.

## Fish Audio model and voices

The default model is `s2.1-pro-free`. Change `FISH_MODEL` to a paid model only after enabling API billing in Fish Audio.

The three sample voice IDs in `main.py` are from Fish Audio's official developer quickstart. Replace them with voice model IDs you have permission to use. Voice IDs are not the same as model names.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Add TELEGRAM_BOT_TOKEN and FISH_API_KEY to .env
python main.py
```

## Environment variables

| Variable | Required | Default |
|---|---|---|
| TELEGRAM_BOT_TOKEN | Yes | — |
| FISH_API_KEY | Yes for generation | — |
| FISH_MODEL | No | s2.1-pro-free |
| MAX_TEXT_CHARS | No | 3500 |
| LOG_LEVEL | No | INFO |

## Commands

- `/start` — main menu
- `/settings` — current voice and style
- `/help` — usage help
- `/cancel` — cancel text entry

## Notes

Audio is generated on demand and sent back as an MP3. The bot does not permanently store audio files. The recent history list is kept in memory and resets when the service restarts. Fish Audio API pricing, availability and fair-use limits are controlled by Fish Audio.
