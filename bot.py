"""
Ника — Discord AI Voice Bot
Бот сидит в голосовом канале и отвечает голосом на сообщения вида:
«Ника, как ты относишься к ...?»
"""

import os
import io
import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv
import edge_tts
from openai import AsyncOpenAI

# ==================== НАСТРОЙКИ ====================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
# openrouter/free — автоматически выбирает бесплатную модель
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "openrouter/free")

# Голос edge-tts (русские женские голоса)
# Варианты: ru-RU-SvetlanaNeural, ru-RU-DariyaNeural
TTS_VOICE = os.getenv("TTS_VOICE", "ru-RU-SvetlanaNeural")

# Триггер-слово (можно несколько)
TRIGGERS = ["ника", "nika"]

# Системный промпт персонажа
SYSTEM_PROMPT = """Ты — Ника, весёлая и немного дерзкая голосовая помощница Discord-сервера.
Отвечай коротко (1–3 предложения), живо, по-дружески, с лёгким сарказмом когда уместно.
Говори на русском. Не используй markdown, эмодзи и длинные списки — ты говоришь голосом.
Если вопрос грубый или провокационный — можешь ответить в том же духе, но без перехода на личности в реальной жизни.
"""

# ==================== ЛОГИ ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("nika")

# ==================== КЛИЕНТЫ ====================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# OpenRouter / OpenAI-совместимый клиент
openai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_BASE_URL,
    default_headers={
        "HTTP-Referer": "https://github.com/etozheNiksych/nika-bot",
        "X-Title": "Nika Discord Bot",
    },
)

# Состояние: guild_id -> text_channel_id (куда слушать)
bound_channels: dict[int, int] = {}
# Очередь воспроизведения, чтобы не перебивать себя
play_locks: dict[int, asyncio.Lock] = {}


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def is_trigger(content: str) -> bool:
    """Проверяет, начинается ли сообщение с триггера."""
    lower = content.lower().strip()
    for t in TRIGGERS:
        if lower.startswith(t):
            return True
    return False


def extract_question(content: str) -> str:
    """Убирает триггер из начала сообщения."""
    lower = content.lower().strip()
    for t in TRIGGERS:
        if lower.startswith(t):
            rest = content[len(t):].lstrip(" ,.:!?-—")
            return rest.strip()
    return content.strip()


async def generate_reply(question: str, author_name: str) -> str:
    """Генерирует ответ через LLM."""
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY не задан!")
        return "У меня нет ключа нейросети. Админ, проверь настройки."

    try:
        log.info(f"LLM запрос | model={OPENAI_MODEL} | base={OPENAI_BASE_URL}")
        response = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{author_name} спрашивает: {question}"},
            ],
            max_tokens=200,
            temperature=0.85,
        )
        text = response.choices[0].message.content
        if not text or not text.strip():
            log.error("LLM вернул пустой ответ")
            return "Хм, я что-то задумалась и ничего не придумала. Спроси ещё раз."
        return text.strip()
    except Exception as e:
        log.error(f"LLM error: {type(e).__name__}: {e}")
        return "Ой, подожди немного, у меня мозги немного зависли. Попробуй ещё раз чуть позже."


async def synthesize_speech(text: str) -> bytes:
    """Синтезирует речь через edge-tts и возвращает mp3-байты."""
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    audio_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data += chunk["data"]
    return audio_data


async def play_audio(voice_client: discord.VoiceClient, audio_bytes: bytes, guild_id: int):
    """Воспроизводит mp3 из байтов в голосовом канале."""
    lock = play_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        while voice_client.is_playing():
            await asyncio.sleep(0.3)

        source = discord.FFmpegPCMAudio(
            io.BytesIO(audio_bytes),
            pipe=True,
            options="-vn"
        )
        voice_client.play(source)

        while voice_client.is_playing():
            await asyncio.sleep(0.3)


# ==================== СОБЫТИЯ ====================

@bot.event
async def on_ready():
    log.info(f"Бот запущен как {bot.user} (ID: {bot.user.id})")
    log.info(f"LLM: model={OPENAI_MODEL} | base={OPENAI_BASE_URL}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="Ника + вопрос"
        )
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await bot.process_commands(message)

    bound_id = bound_channels.get(message.guild.id)
    if bound_id is None or message.channel.id != bound_id:
        return

    if not is_trigger(message.content):
        return

    question = extract_question(message.content)
    if not question:
        await message.channel.send(
            "Ника включена. Напиши «Ника» и вопрос — отвечу голосом. "
            "Микрофоны не слушаю."
        )
        return

    voice_client = message.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await message.channel.send(
            "Я не в голосовом канале. Сначала используй `!join`."
        )
        return

    async with message.channel.typing():
        reply = await generate_reply(question, message.author.display_name)
        log.info(f"[{message.guild.name}] {message.author}: {question} → {reply}")

        try:
            audio = await synthesize_speech(reply)
        except Exception as e:
            log.error(f"TTS error: {e}")
            await message.channel.send("Не получилось озвучить ответ 😢")
            return

        await play_audio(voice_client, audio, message.guild.id)


# ==================== КОМАНДЫ ====================

@bot.command(name="join")
async def join(ctx: commands.Context):
    """Бот заходит в твой голосовой канал и привязывается к текущему текстовому."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Ты должен быть в голосовом канале.")
        return

    channel = ctx.author.voice.channel

    if ctx.voice_client:
        await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()

    bound_channels[ctx.guild.id] = ctx.channel.id

    await ctx.send(
        f"✅ Зашла в **{channel.name}**\n"
        f"Слушаю этот чат (`#{ctx.channel.name}`).\n"
        f"Пиши **Ника** + вопрос — отвечу голосом."
    )


@bot.command(name="leave")
async def leave(ctx: commands.Context):
    """Бот выходит из голосового канала."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        bound_channels.pop(ctx.guild.id, None)
        await ctx.send("Вышла. Пока! 👋")
    else:
        await ctx.send("Я и так не в войсе.")


@bot.command(name="bind")
async def bind(ctx: commands.Context):
    """Привязать бота к текущему текстовому каналу (если уже в войсе)."""
    if not ctx.voice_client:
        await ctx.send("Сначала зайди в войс командой `!join`.")
        return
    bound_channels[ctx.guild.id] = ctx.channel.id
    await ctx.send(f"Теперь слушаю чат `#{ctx.channel.name}`.")


@bot.command(name="status")
async def status(ctx: commands.Context):
    """Показать текущее состояние бота."""
    vc = ctx.voice_client
    bound = bound_channels.get(ctx.guild.id)
    bound_name = "не привязан"
    if bound:
        ch = ctx.guild.get_channel(bound)
        bound_name = f"#{ch.name}" if ch else f"ID {bound}"

    embed = discord.Embed(title="Статус Ника", color=0xF5A623)
    embed.add_field(
        name="Голосовой канал",
        value=vc.channel.name if vc and vc.channel else "не подключена",
        inline=False
    )
    embed.add_field(name="Слушает чат", value=bound_name, inline=False)
    embed.add_field(name="Голос TTS", value=TTS_VOICE, inline=False)
    embed.add_field(name="Модель LLM", value=OPENAI_MODEL, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="say")
async def say(ctx: commands.Context, *, text: str):
    """Принудительно озвучить любой текст (для теста)."""
    if not ctx.voice_client:
        await ctx.send("Я не в войсе. Сначала `!join`.")
        return
    try:
        audio = await synthesize_speech(text)
        await play_audio(ctx.voice_client, audio, ctx.guild.id)
        await ctx.message.add_reaction("🔊")
    except Exception as e:
        await ctx.send(f"Ошибка TTS: {e}")


# ==================== ЗАПУСК ====================

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ Нет DISCORD_TOKEN в .env")
    if not OPENAI_API_KEY:
        log.warning("⚠️ OPENAI_API_KEY не задан — нейросеть работать не будет")

    bot.run(DISCORD_TOKEN)
