# TranslatorAPP

TranslatorAPP is a free Discord translation bot with offline language detection, per-channel language routing, private manual translations, message context translation, protected Discord formatting, caching, a bounded translation queue, automatic retry and provider cooldown controls, rotating debug logs, activity logging, and usage statistics. It uses deep-translator and does not require a paid translation API key.

## Setup

1. Copy `.env.example` to `.env` and add the required credentials.
2. Enable Message Content Intent in the Discord Developer Portal.
3. Install dependencies with `python3 -m pip install -r requirements.txt`.
4. Start the bot with `python3 bot.py`.
5. Run `/translator setup` in Discord.

## Commands

- `/translate` privately translates text into a selected language.
- `/translator setup` enables the system and configures its first channel.
- `/translator add-channel` adds or updates automatic channel routing.
- `/translator remove-channel` removes automatic channel routing.
- `/translator toggle` pauses or resumes automatic translation.
- `/translator status` shows configuration and statistics.
- `/translator health` shows queue, worker, cache, cooldown, retry, and database diagnostics.
- `Apps > Translate message` privately translates an existing message into the server default language.
