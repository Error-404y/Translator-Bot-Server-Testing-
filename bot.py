import asyncio
import html
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from collections import OrderedDict
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from deep_translator import GoogleTranslator
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from lingua import Language, LanguageDetectorBuilder


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_VALUE = os.getenv("GUILD_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

GUILD_ID = int(GUILD_ID_VALUE) if GUILD_ID_VALUE else None
DATABASE_FILE = Path(__file__).with_name("translator.db")
LOG_DIRECTORY = Path(__file__).with_name("logs")
LOG_FILE = LOG_DIRECTORY / "translator.log"
MAX_INPUT_LENGTH = 5000
MAX_EMBED_LENGTH = 4000
CACHE_SIZE = 2000
CACHE_TTL = 3600
USER_RATE_LIMIT = 2.0
TRANSLATION_WORKERS = 3
PROVIDER_CONCURRENCY = 2
TRANSLATION_QUEUE_SIZE = 500
TRANSLATION_TIMEOUT = 60
PROVIDER_ATTEMPTS = 3
PROVIDER_COOLDOWN = 15
BRAND_COLOR = discord.Color.from_rgb(74, 105, 189)
SUCCESS_COLOR = discord.Color.from_rgb(47, 137, 89)
ERROR_COLOR = discord.Color.from_rgb(177, 57, 57)

LOG_DIRECTORY.mkdir(exist_ok=True)
log_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
log_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger = logging.getLogger("translator")
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)
logger.propagate = False
discord_logger = logging.getLogger("discord")
discord_logger.setLevel(logging.INFO)
discord_logger.addHandler(log_handler)
discord_logger.propagate = False

LANGUAGES = OrderedDict(
    [
        ("en", "English"),
        ("es", "Spanish"),
        ("fr", "French"),
        ("de", "German"),
        ("it", "Italian"),
        ("pt", "Portuguese"),
        ("nl", "Dutch"),
        ("pl", "Polish"),
        ("tr", "Turkish"),
        ("el", "Greek"),
        ("ru", "Russian"),
        ("uk", "Ukrainian"),
        ("ar", "Arabic"),
        ("he", "Hebrew"),
        ("hi", "Hindi"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
        ("zh", "Chinese"),
        ("sv", "Swedish"),
        ("no", "Norwegian"),
        ("da", "Danish"),
        ("fi", "Finnish"),
        ("cs", "Czech"),
        ("ro", "Romanian"),
        ("hu", "Hungarian"),
    ]
)

LANGUAGE_CODES = {
    Language.ENGLISH: "en",
    Language.SPANISH: "es",
    Language.FRENCH: "fr",
    Language.GERMAN: "de",
    Language.ITALIAN: "it",
    Language.PORTUGUESE: "pt",
    Language.DUTCH: "nl",
    Language.POLISH: "pl",
    Language.TURKISH: "tr",
    Language.GREEK: "el",
    Language.RUSSIAN: "ru",
    Language.UKRAINIAN: "uk",
    Language.ARABIC: "ar",
    Language.HEBREW: "he",
    Language.HINDI: "hi",
    Language.JAPANESE: "ja",
    Language.KOREAN: "ko",
    Language.CHINESE: "zh",
    Language.SWEDISH: "sv",
    Language.BOKMAL: "no",
    Language.NYNORSK: "no",
    Language.DANISH: "da",
    Language.FINNISH: "fi",
    Language.CZECH: "cs",
    Language.ROMANIAN: "ro",
    Language.HUNGARIAN: "hu",
}

language_detector = (
    LanguageDetectorBuilder.from_languages(*LANGUAGE_CODES.keys())
    .with_preloaded_language_models()
    .build()
)

PROTECTED_PATTERNS = (
    re.compile(r"```[\s\S]*?```"),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>|<a?:[A-Za-z0-9_]+:\d+>"),
    re.compile(r"https?://[^\s<>]+"),
)


class Database:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                default_language TEXT NOT NULL DEFAULT 'en',
                log_channel_id INTEGER,
                show_original INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_settings (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                target_language TEXT NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS translation_stats (
                guild_id INTEGER PRIMARY KEY,
                automatic_count INTEGER NOT NULL DEFAULT 0,
                manual_count INTEGER NOT NULL DEFAULT 0,
                characters_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.commit()

    def guild(self, guild_id):
        row = self.connection.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return dict(row) if row else None

    def setup_guild(self, guild_id, language, log_channel_id):
        self.connection.execute(
            """
            INSERT INTO guild_settings (guild_id, enabled, default_language, log_channel_id)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled = 1,
                default_language = excluded.default_language,
                log_channel_id = excluded.log_channel_id
            """,
            (guild_id, language, log_channel_id),
        )
        self.connection.commit()

    def set_enabled(self, guild_id, enabled):
        self.connection.execute(
            "UPDATE guild_settings SET enabled = ? WHERE guild_id = ?",
            (int(enabled), guild_id),
        )
        self.connection.commit()

    def set_channel(self, guild_id, channel_id, language):
        self.connection.execute(
            """
            INSERT INTO channel_settings (guild_id, channel_id, target_language)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                target_language = excluded.target_language
            """,
            (guild_id, channel_id, language),
        )
        self.connection.commit()

    def remove_channel(self, guild_id, channel_id):
        cursor = self.connection.execute(
            "DELETE FROM channel_settings WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def channel_language(self, guild_id, channel_id):
        row = self.connection.execute(
            "SELECT target_language FROM channel_settings WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        return row["target_language"] if row else None

    def channels(self, guild_id):
        rows = self.connection.execute(
            "SELECT channel_id, target_language FROM channel_settings WHERE guild_id = ? ORDER BY channel_id",
            (guild_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def increment(self, guild_id, kind, characters=0):
        self.connection.execute(
            "INSERT OR IGNORE INTO translation_stats (guild_id) VALUES (?)",
            (guild_id,),
        )
        column = {
            "automatic": "automatic_count",
            "manual": "manual_count",
            "error": "error_count",
        }[kind]
        self.connection.execute(
            f"UPDATE translation_stats SET {column} = {column} + 1, characters_count = characters_count + ? WHERE guild_id = ?",
            (characters, guild_id),
        )
        self.connection.commit()

    def stats(self, guild_id):
        row = self.connection.execute(
            "SELECT * FROM translation_stats WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return dict(row) if row else {
            "automatic_count": 0,
            "manual_count": 0,
            "characters_count": 0,
            "error_count": 0,
        }

    def close(self):
        self.connection.close()


class TranslationCache:
    def __init__(self, limit, ttl):
        self.limit = limit
        self.ttl = ttl
        self.items = OrderedDict()

    def get(self, text, target):
        key = (text, target)
        item = self.items.get(key)
        if not item:
            return None
        if time.monotonic() - item[0] > self.ttl:
            self.items.pop(key, None)
            return None
        self.items.move_to_end(key)
        return item[1]

    def put(self, text, target, value):
        key = (text, target)
        self.items[key] = (time.monotonic(), value)
        self.items.move_to_end(key)
        while len(self.items) > self.limit:
            self.items.popitem(last=False)


class RateLimiter:
    def __init__(self, interval):
        self.interval = interval
        self.entries = {}

    def retry_after(self, guild_id, user_id):
        now = time.monotonic()
        key = (guild_id, user_id)
        previous = self.entries.get(key, 0)
        remaining = self.interval - (now - previous)
        if remaining > 0:
            return remaining
        self.entries[key] = now
        if len(self.entries) > 5000:
            self.entries = {
                entry: used
                for entry, used in self.entries.items()
                if now - used < self.interval * 10
            }
        return 0


def protect_content(text):
    protected = {}
    index = 0
    for pattern in PROTECTED_PATTERNS:
        def replace(match):
            nonlocal index
            token = f"ZXQPROTECTED{index}QXZ"
            protected[token] = match.group(0)
            index += 1
            return token
        text = pattern.sub(replace, text)
    return text, protected


def restore_content(text, protected):
    for token, original in protected.items():
        text = re.sub(re.escape(token), lambda match: original, text, flags=re.IGNORECASE)
    return text


def meaningful_text(text):
    stripped, _ = protect_content(text)
    stripped = re.sub(r"[^\w\u00C0-\uFFFF]+", "", stripped, flags=re.UNICODE)
    return len(stripped) >= 2


def language_name(code):
    if not code:
        return "Unknown"
    return LANGUAGES.get(code.lower(), code.upper())


class TranslationService:
    def __init__(self):
        self.cache = TranslationCache(CACHE_SIZE, CACHE_TTL)
        self.queue = asyncio.Queue(maxsize=TRANSLATION_QUEUE_SIZE)
        self.provider_semaphore = asyncio.Semaphore(PROVIDER_CONCURRENCY)
        self.workers = []
        self.pending = {}
        self.cooldown_until = 0.0
        self.completed = 0
        self.failed = 0
        self.retried = 0

    async def start(self):
        if self.workers:
            return
        self.workers = [
            asyncio.create_task(self._worker(index), name=f"translation-worker-{index}")
            for index in range(TRANSLATION_WORKERS)
        ]
        logger.info("Translation queue started with %s workers", TRANSLATION_WORKERS)

    async def stop(self):
        if not self.workers:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Translation queue did not drain before shutdown")
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        logger.info("Translation queue stopped")

    async def detect(self, text):
        detection_text, _ = protect_content(text)
        detection_text = re.sub(r"ZXQPROTECTED\d+QXZ", " ", detection_text)
        detection_text = re.sub(r"\s+", " ", detection_text).strip()
        if len(detection_text) < 3:
            return None
        language = await asyncio.to_thread(language_detector.detect_language_of, detection_text)
        return LANGUAGE_CODES.get(language)

    async def translate(self, text, target):
        text = text.strip()
        if not text:
            raise ValueError("Enter some text to translate.")
        if len(text) > MAX_INPUT_LENGTH:
            raise ValueError(f"Text cannot exceed {MAX_INPUT_LENGTH:,} characters.")
        cached = self.cache.get(text, target)
        if cached:
            return cached
        source = await self.detect(text)
        if source == target:
            return {"text": text, "source": source}
        key = (text, target)
        existing = self.pending.get(key)
        if existing:
            return await asyncio.wait_for(asyncio.shield(existing), timeout=TRANSLATION_TIMEOUT)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[key] = future
        try:
            self.queue.put_nowait((key, text, target, source, future))
        except asyncio.QueueFull:
            self.pending.pop(key, None)
            raise RuntimeError("The translation queue is full. Please try again shortly.")
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=TRANSLATION_TIMEOUT)
        finally:
            if future.done():
                self.pending.pop(key, None)

    async def _worker(self, index):
        while True:
            item = await self.queue.get()
            key, text, target, source, future = item
            try:
                if future.cancelled():
                    continue
                result = await self._translate_with_retry(text, target, source)
                self.cache.put(text, target, result)
                self.completed += 1
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as error:
                self.failed += 1
                if not future.done():
                    future.set_exception(error)
                logger.exception("Translation worker %s failed for target %s", index, target)
            finally:
                self.pending.pop(key, None)
                self.queue.task_done()

    async def _translate_with_retry(self, text, target, source):
        prepared, protected = protect_content(text)
        last_error = None
        for attempt in range(1, PROVIDER_ATTEMPTS + 1):
            cooldown = self.cooldown_until - time.monotonic()
            if cooldown > 0:
                await asyncio.sleep(cooldown)
            try:
                async with self.provider_semaphore:
                    translated = await asyncio.to_thread(
                        GoogleTranslator(source="auto", target=target).translate,
                        prepared,
                    )
                translated = html.unescape(translated or "").strip()
                if not translated:
                    raise RuntimeError("The translation provider returned no text.")
                translated = restore_content(translated, protected)
                normalized_original = re.sub(r"\s+", " ", text).strip().casefold()
                normalized_translation = re.sub(r"\s+", " ", translated).strip().casefold()
                detected = source or (target if normalized_original == normalized_translation else "auto")
                return {"text": translated, "source": detected}
            except Exception as error:
                last_error = error
                if attempt >= PROVIDER_ATTEMPTS:
                    break
                self.retried += 1
                delay = min(8.0, 0.8 * (2 ** (attempt - 1))) + random.uniform(0.1, 0.6)
                logger.warning(
                    "Provider attempt %s/%s failed for target %s; retrying in %.2fs: %s",
                    attempt,
                    PROVIDER_ATTEMPTS,
                    target,
                    delay,
                    type(error).__name__,
                )
                await asyncio.sleep(delay)
        self.cooldown_until = time.monotonic() + PROVIDER_COOLDOWN
        raise RuntimeError("The free translation provider is temporarily unavailable.") from last_error

    def health(self):
        return {
            "queue_size": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
            "workers": sum(not worker.done() for worker in self.workers),
            "completed": self.completed,
            "failed": self.failed,
            "retried": self.retried,
            "cooldown": max(0, round(self.cooldown_until - time.monotonic(), 1)),
            "cache_entries": len(self.cache.items),
        }


database = Database(DATABASE_FILE)
service = TranslationService()
rate_limiter = RateLimiter(USER_RATE_LIMIT)


def translation_embed(author, result, target, original=None):
    translated = result["text"]
    if len(translated) > MAX_EMBED_LENGTH:
        translated = translated[: MAX_EMBED_LENGTH - 3] + "..."
    embed = discord.Embed(description=translated, color=BRAND_COLOR)
    embed.set_author(name=author.display_name, icon_url=author.display_avatar.url)
    if original:
        original = original[:1000] + ("..." if len(original) > 1000 else "")
        embed.add_field(name="Original", value=original, inline=False)
    return embed


def response_embed(title, description, success=True):
    return discord.Embed(
        title=title,
        description=description,
        color=SUCCESS_COLOR if success else ERROR_COLOR,
    )


async def log_translation(guild, settings, member, source, target, kind):
    channel_id = settings.get("log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(title="Translation Activity", color=BRAND_COLOR, timestamp=discord.utils.utcnow())
    embed.add_field(name="Member", value=f"{member.mention}\n{member.id}", inline=True)
    embed.add_field(name="Direction", value=f"{language_name(source)} to {language_name(target)}", inline=True)
    embed.add_field(name="Type", value=kind.title(), inline=True)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


class TranslatorBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self):
        await service.start()
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        print(f"Connected to {len(self.guilds)} server(s)")
        print("Translator system is ready.")
        logger.info("Bot ready as %s in %s servers", self.user, len(self.guilds))

    async def close(self):
        logger.info("Bot shutdown requested")
        await service.stop()
        await super().close()

    async def on_error(self, event_method, *args, **kwargs):
        error_id = secrets.token_hex(4)
        logger.exception("Unhandled Discord event error_id=%s event=%s", error_id, event_method)

    async def on_message(self, message):
        if message.author.bot or message.guild is None or not message.content:
            return
        settings = database.guild(message.guild.id)
        if not settings or not settings["enabled"]:
            return
        target = database.channel_language(message.guild.id, message.channel.id)
        if not target or not meaningful_text(message.content):
            return
        if rate_limiter.retry_after(message.guild.id, message.author.id) > 0:
            return
        try:
            result = await service.translate(message.content, target)
            if not result["source"] or result["source"].lower() == target.lower():
                return
            embed = translation_embed(
                message.author,
                result,
                target,
                message.content if settings["show_original"] else None,
            )
            await message.reply(embed=embed, mention_author=False)
            database.increment(message.guild.id, "automatic", len(message.content))
            await log_translation(message.guild, settings, message.author, result["source"], target, "automatic")
        except ValueError:
            return
        except Exception as error:
            database.increment(message.guild.id, "error")
            error_id = secrets.token_hex(4)
            logger.exception(
                "Automatic translation failed error_id=%s guild=%s channel=%s user=%s",
                error_id,
                message.guild.id,
                message.channel.id,
                message.author.id,
            )


bot = TranslatorBot()

language_choices = [
    app_commands.Choice(name=name, value=code)
    for code, name in LANGUAGES.items()
]
translator_group = app_commands.Group(name="translator", description="Configure automatic translation.")


@bot.tree.command(name="translate", description="Privately translate text with automatic language detection.")
@app_commands.describe(text="Text to translate", target="Language for the translation")
@app_commands.choices(target=language_choices)
async def translate_command(interaction: discord.Interaction, text: str, target: app_commands.Choice[str]):
    if interaction.guild is None:
        await interaction.response.send_message("This command is available inside servers.", ephemeral=True)
        return
    retry = rate_limiter.retry_after(interaction.guild.id, interaction.user.id)
    if retry > 0:
        await interaction.response.send_message(f"Please wait {retry:.1f} seconds before translating again.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await service.translate(text, target.value)
        if result["source"] and result["source"].lower() == target.value:
            await interaction.followup.send(
                embed=response_embed("Already translated", f"The text is already in {target.name}."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=translation_embed(interaction.user, result, target.value),
            ephemeral=True,
        )
        database.increment(interaction.guild.id, "manual", len(text))
        settings = database.guild(interaction.guild.id)
        if settings:
            await log_translation(interaction.guild, settings, interaction.user, result["source"], target.value, "manual")
    except ValueError as error:
        await interaction.followup.send(embed=response_embed("Invalid text", str(error), False), ephemeral=True)
    except Exception as error:
        database.increment(interaction.guild.id, "error")
        error_id = secrets.token_hex(4)
        logger.exception(
            "Manual translation failed error_id=%s guild=%s user=%s",
            error_id,
            interaction.guild.id,
            interaction.user.id,
        )
        await interaction.followup.send(
            embed=response_embed(
                "Translation unavailable",
                f"The request could not be completed. Reference: {error_id}",
                False,
            ),
            ephemeral=True,
        )


@translator_group.command(name="setup", description="Enable translation and configure the first automatic channel.")
@app_commands.describe(
    channel="Channel whose messages should be translated",
    target="Language messages should be translated into",
    log_channel="Optional private activity log channel",
)
@app_commands.choices(target=language_choices)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    target: app_commands.Choice[str],
    log_channel: discord.TextChannel | None = None,
):
    guild = interaction.guild
    permissions = channel.permissions_for(guild.me)
    if not permissions.view_channel or not permissions.send_messages or not permissions.embed_links or not permissions.read_message_history:
        await interaction.response.send_message(
            embed=response_embed(
                "Missing permissions",
                "I need View Channel, Send Messages, Embed Links, and Read Message History in the translation channel.",
                False,
            ),
            ephemeral=True,
        )
        return
    database.setup_guild(guild.id, target.value, log_channel.id if log_channel else None)
    database.set_channel(guild.id, channel.id, target.value)
    await interaction.response.send_message(
        embed=response_embed(
            "Translator configured",
            f"Messages in {channel.mention} will be detected automatically and translated into {target.name}.",
        ),
        ephemeral=True,
    )


@translator_group.command(name="add-channel", description="Enable automatic translation in another channel.")
@app_commands.describe(channel="Channel to enable", target="Target language for this channel")
@app_commands.choices(target=language_choices)
@app_commands.checks.has_permissions(manage_guild=True)
async def add_channel_command(interaction: discord.Interaction, channel: discord.TextChannel, target: app_commands.Choice[str]):
    if not database.guild(interaction.guild.id):
        await interaction.response.send_message(embed=response_embed("Not configured", "Run /translator setup first.", False), ephemeral=True)
        return
    permissions = channel.permissions_for(interaction.guild.me)
    if not permissions.view_channel or not permissions.send_messages or not permissions.embed_links:
        await interaction.response.send_message(embed=response_embed("Missing permissions", "I cannot send translation embeds in that channel.", False), ephemeral=True)
        return
    database.set_channel(interaction.guild.id, channel.id, target.value)
    await interaction.response.send_message(
        embed=response_embed("Channel enabled", f"{channel.mention} will translate detected languages into {target.name}."),
        ephemeral=True,
    )


@translator_group.command(name="remove-channel", description="Disable automatic translation in a channel.")
@app_commands.describe(channel="Channel to disable")
@app_commands.checks.has_permissions(manage_guild=True)
async def remove_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    removed = database.remove_channel(interaction.guild.id, channel.id)
    message = f"Automatic translation was disabled in {channel.mention}." if removed else "That channel was not configured."
    await interaction.response.send_message(embed=response_embed("Channel updated", message), ephemeral=True)


@translator_group.command(name="toggle", description="Pause or resume all automatic translation.")
@app_commands.describe(enabled="Whether automatic translation should run")
@app_commands.checks.has_permissions(manage_guild=True)
async def toggle_command(interaction: discord.Interaction, enabled: bool):
    if not database.guild(interaction.guild.id):
        await interaction.response.send_message(embed=response_embed("Not configured", "Run /translator setup first.", False), ephemeral=True)
        return
    database.set_enabled(interaction.guild.id, enabled)
    state = "enabled" if enabled else "paused"
    await interaction.response.send_message(embed=response_embed("Translator updated", f"Automatic translation is now {state}."), ephemeral=True)


@translator_group.command(name="status", description="Show configuration and translation statistics.")
@app_commands.checks.has_permissions(manage_guild=True)
async def status_command(interaction: discord.Interaction):
    settings = database.guild(interaction.guild.id)
    if not settings:
        await interaction.response.send_message(embed=response_embed("Not configured", "Run /translator setup first.", False), ephemeral=True)
        return
    configured = database.channels(interaction.guild.id)
    stats = database.stats(interaction.guild.id)
    channel_lines = []
    for item in configured:
        channel = interaction.guild.get_channel(item["channel_id"])
        channel_lines.append(f"{channel.mention if channel else 'Missing channel'} to {language_name(item['target_language'])}")
    embed = discord.Embed(title="Translator Status", color=BRAND_COLOR)
    embed.add_field(name="Automatic translation", value="Enabled" if settings["enabled"] else "Paused", inline=True)
    embed.add_field(name="Configured channels", value=str(len(configured)), inline=True)
    embed.add_field(name="Characters translated", value=f"{stats['characters_count']:,}", inline=True)
    embed.add_field(name="Automatic translations", value=f"{stats['automatic_count']:,}", inline=True)
    embed.add_field(name="Manual translations", value=f"{stats['manual_count']:,}", inline=True)
    embed.add_field(name="Provider errors", value=f"{stats['error_count']:,}", inline=True)
    embed.add_field(name="Channel routing", value="\n".join(channel_lines)[:1024] or "No channels", inline=False)
    embed.set_footer(text="Language detection is automatic for every configured channel")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@translator_group.command(name="health", description="Check translator reliability and diagnostics.")
@app_commands.checks.has_permissions(manage_guild=True)
async def health_command(interaction: discord.Interaction):
    health = service.health()
    database_ok = True
    try:
        database.connection.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        database_ok = False
        logger.exception("Database health check failed for guild %s", interaction.guild.id)
    workers_ok = health["workers"] == TRANSLATION_WORKERS
    cooldown_text = f"{health['cooldown']} seconds" if health["cooldown"] else "Ready"
    overall = database_ok and workers_ok and health["queue_size"] < health["queue_capacity"]
    embed = discord.Embed(
        title="Translator Health",
        description="All core systems are operational." if overall else "One or more systems need attention.",
        color=SUCCESS_COLOR if overall else ERROR_COLOR,
    )
    embed.add_field(name="Database", value="Operational" if database_ok else "Unavailable", inline=True)
    embed.add_field(name="Workers", value=f"{health['workers']}/{TRANSLATION_WORKERS}", inline=True)
    embed.add_field(name="Provider cooldown", value=cooldown_text, inline=True)
    embed.add_field(name="Queue", value=f"{health['queue_size']}/{health['queue_capacity']}", inline=True)
    embed.add_field(name="Cache", value=f"{health['cache_entries']}/{CACHE_SIZE}", inline=True)
    embed.add_field(name="Automatic retries", value=f"{health['retried']:,}", inline=True)
    embed.add_field(name="Completed this session", value=f"{health['completed']:,}", inline=True)
    embed.add_field(name="Failed this session", value=f"{health['failed']:,}", inline=True)
    embed.add_field(name="Debug log", value="logs/translator.log", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def translate_message_context(interaction: discord.Interaction, message: discord.Message):
    settings = database.guild(interaction.guild.id) if interaction.guild else None
    target = settings["default_language"] if settings else "en"
    if not message.content:
        await interaction.response.send_message(embed=response_embed("Nothing to translate", "That message has no text content.", False), ephemeral=True)
        return
    retry = rate_limiter.retry_after(interaction.guild.id, interaction.user.id)
    if retry > 0:
        await interaction.response.send_message(f"Please wait {retry:.1f} seconds before translating again.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await service.translate(message.content, target)
        if result["source"] == target:
            await interaction.followup.send(
                embed=response_embed("Already translated", f"The message is already in {language_name(target)}."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=translation_embed(message.author, result, target), ephemeral=True)
        database.increment(interaction.guild.id, "manual", len(message.content))
    except Exception as error:
        database.increment(interaction.guild.id, "error")
        error_id = secrets.token_hex(4)
        logger.exception(
            "Context translation failed error_id=%s guild=%s user=%s message=%s",
            error_id,
            interaction.guild.id,
            interaction.user.id,
            message.id,
        )
        await interaction.followup.send(
            embed=response_embed("Translation unavailable", f"The message could not be translated. Reference: {error_id}", False),
            ephemeral=True,
        )


translate_context_menu = app_commands.ContextMenu(name="Translate message", callback=translate_message_context)
bot.tree.add_command(translate_context_menu)
bot.tree.add_command(translator_group)


@bot.tree.error
async def command_error(interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "You need the Manage Server permission to use this command."
    else:
        error_id = secrets.token_hex(4)
        logger.error(
            "Application command failed error_id=%s command=%s user=%s",
            error_id,
            interaction.command.name if interaction.command else "unknown",
            interaction.user.id,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = f"The command could not be completed. Reference: {error_id}"
    if not interaction.response.is_done():
        await interaction.response.send_message(embed=response_embed("Command unavailable", message, False), ephemeral=True)


def main():
    try:
        bot.run(TOKEN, log_handler=None)
    except KeyboardInterrupt:
        print("Bot stopped.")
    finally:
        database.close()


if __name__ == "__main__":
    main()
