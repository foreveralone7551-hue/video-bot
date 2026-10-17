"""
Telegram-бот: качает видео / фото / звук по ссылке — без водяных знаков.
TikTok • YouTube Shorts • Twitch Clips • SoundCloud
Работает в личке, группах и каналах.
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from telegram import InputMediaPhoto, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp.utils import match_filter_func

# ───────────────────────── настройки ─────────────────────────

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx пишет в лог полные URL запросов, а в них токен бота — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.getenv("PORT", "10000"))
PROXY = os.getenv("PROXY") or None  # необязательно, например http://user:pass@host:port
MAX_BYTES = 49 * 1024 * 1024  # лимит Telegram для ботов — 50 МБ
MAX_LINKS_PER_MESSAGE = 3
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
TIKWM_API = "https://www.tikwm.com/api/"

# ffmpeg: системный, а если его нет — встроенный из пакета imageio-ffmpeg
FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    try:
        import imageio_ffmpeg

        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        FFMPEG = None

# необязательные cookies для YouTube (содержимое cookies.txt в переменной YT_COOKIES)
COOKIES_FILE = None
if os.getenv("YT_COOKIES"):
    COOKIES_FILE = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(COOKIES_FILE, "w", encoding="utf-8") as _f:
        _f.write(os.environ["YT_COOKIES"])

SEM = asyncio.Semaphore(2)  # не больше двух загрузок одновременно (бесплатный сервер слабый)

GREETING = (
    "👋 <b>Привет!</b>\n\n"
    "Чтобы скачать видео, фото или звук с <b>TikTok</b> 🎵, <b>YouTube Shorts</b> ▶️, "
    "<b>Twitch Clips</b> 🎮 и <b>SoundCloud</b> 🎧 — просто отправь мне ссылку.\n\n"
    "✨ Без водяных знаков\n"
    "⚡ Быстро и бесплатно\n"
    "💬 Работаю в личке, группах и каналах\n\n"
    "Жду ссылку 👇"
)

TOO_BIG = "Файл слишком длинный или тяжёлый — лимит Telegram для ботов 50 МБ 📦"
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".opus", ".ogg"}


class UserError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


# ───────────────────────── ссылки ─────────────────────────

URL_RE = re.compile(
    r"(?<![\w@.\-])(?:https?://)?(?:[\w\-]+\.)*"
    r"(?:tiktok\.com|youtube\.com|youtu\.be|twitch\.tv|soundcloud\.com|snd\.sc)"
    r"(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)


def extract_urls(msg):
    text = msg.text or msg.caption or ""
    urls = []
    for m in URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:!?)]}»…")
        if not u.lower().startswith("http"):
            u = "https://" + u
        urls.append(u)
    # ссылки, спрятанные за текстом («нажми сюда»)
    for e in (msg.entities or msg.caption_entities or []):
        if e.type == "text_link" and e.url:
            urls.append(e.url)
    return urls


def classify(url):
    """Возвращает 'tiktok' | 'youtube' | 'twitch' | 'soundcloud' или None."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    path = u.path.lower()

    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        if host.startswith(("vm.", "vt.")) or any(
            s in path for s in ("/video/", "/photo/", "/t/", "/v/")
        ):
            return "tiktok"
        return None

    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        if host == "youtu.be" or path.startswith(("/shorts/", "/watch", "/embed/")):
            return "youtube"
        return None

    if host == "clips.twitch.tv" or (
        (host == "twitch.tv" or host.endswith(".twitch.tv")) and "/clip/" in path
    ):
        return "twitch"

    if host == "snd.sc" or host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        # профиль без трека (soundcloud.com/artist) не берём
        if host == "soundcloud.com" and path.strip("/").count("/") < 1:
            return None
        return "soundcloud"

    return None


# ───────────────────────── скачивание (yt-dlp) ─────────────────────────


def download_sync(url, kind, workdir, small=False):
    """Блокирующая загрузка через yt-dlp. Запускается в отдельном потоке."""
    is_audio = kind == "soundcloud"
    height = 480 if small else 1080

    opts = {
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_BYTES,
        "merge_output_format": "mp4",
        "match_filter": match_filter_func(
            "!is_live & duration <=? %d" % (1800 if is_audio else 900)
        ),
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    if PROXY:
        opts["proxy"] = PROXY
    if kind == "youtube" and COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE

    if is_audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        # приоритет: H.264 mp4 (играет везде) и БЕЗ формата с пометкой watermarked
        nowm = "[format_note!*=?watermarked]"
        h = f"[height<=?{height}]"
        opts["format"] = (
            f"bv*[vcodec~='^(avc|h264)'][ext=mp4]{nowm}{h}+ba[ext=m4a]"
            f"/b[vcodec~='^(avc|h264)'][ext=mp4]{nowm}{h}"
            f"/b[ext=mp4]{nowm}{h}"
            f"/bv*{h}+ba/b{h}"
        )

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise UserError("Ничего не нашёл по этой ссылке 🤷")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise UserError("Ничего не нашёл по этой ссылке 🤷")
        info = entries[0]

    files = [
        p for p in Path(workdir).iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXT
    ]
    if not files:
        raise UserError(TOO_BIG)
    path = max(files, key=lambda p: p.stat().st_size)

    return {
        "path": path,
        "size": path.stat().st_size,
        "title": info.get("track") or info.get("title") or "",
        "uploader": info.get("artist") or info.get("uploader") or "",
        "duration": int(info.get("duration") or 0) or None,
        "width": info.get("width"),
        "height": info.get("height"),
    }


async def download_with_retry(url, kind, workdir):
    r = await asyncio.to_thread(download_sync, url, kind, workdir, False)
    if r["size"] > MAX_BYTES:
        if kind == "soundcloud":
            raise UserError(TOO_BIG)
        # слишком тяжело — пробуем качество пониже
        for p in Path(workdir).iterdir():
            p.unlink(missing_ok=True)
        r = await asyncio.to_thread(download_sync, url, kind, workdir, True)
        if r["size"] > MAX_BYTES:
            raise UserError(TOO_BIG)
    return r


# ───────────────────────── TikTok: запасной вариант (tikwm) ─────────────────────────
# Нужен для фото-постов (слайд-шоу) и когда yt-dlp не справился.


def _abs(u):
    return u if u.startswith("http") else "https://www.tikwm.com" + u


async def send_via_tikwm(msg, url):
    async with httpx.AsyncClient(
        timeout=40, follow_redirects=True, headers={"User-Agent": UA}
    ) as client:
        data = None
        for attempt in range(2):
            resp = await client.post(TIKWM_API, data={"url": url, "hd": 1})
            j = resp.json()
            if isinstance(j, dict) and j.get("code") == 0 and j.get("data"):
                data = j["data"]
                break
            if attempt == 0:
                await asyncio.sleep(1.5)  # у бесплатного API лимит ~1 запрос/сек
        if not data:
            raise UserError("Не получилось скачать с TikTok. Ссылка точно рабочая? 🤔")

        # фото-пост
        images = data.get("images") or []
        if images:
            blobs = []
            for img in images[:30]:
                r = await client.get(_abs(img))
                r.raise_for_status()
                blobs.append(r.content)
            for i in range(0, len(blobs), 10):
                chunk = blobs[i : i + 10]
                if len(chunk) == 1:
                    await msg.reply_photo(photo=chunk[0], filename="photo.jpg")
                else:
                    await msg.reply_media_group(
                        media=[
                            InputMediaPhoto(b, filename=f"photo{i + n}.jpg")
                            for n, b in enumerate(chunk)
                        ]
                    )
            return

        # видео: сначала HD, если тяжёлое — обычное
        for key in ("hdplay", "play"):
            video_url = data.get(key)
            if not video_url:
                continue
            r = await client.get(_abs(video_url))
            r.raise_for_status()
            if len(r.content) <= MAX_BYTES:
                await msg.reply_video(
                    video=r.content,
                    filename="video.mp4",
                    duration=data.get("duration") or None,
                    supports_streaming=True,
                )
                return
        raise UserError(TOO_BIG)


# ───────────────────────── отправка в Telegram ─────────────────────────


async def send_result(msg, r, kind):
    path = r["path"]
    if kind == "soundcloud":
        name = f"{r['uploader']} - {r['title']}".strip(" -") or "track"
        name = re.sub(r'[\\/:*?"<>|]', "_", name)[:100] + ".mp3"
        with path.open("rb") as f:
            await msg.reply_audio(
                audio=f,
                filename=name,
                title=r["title"] or None,
                performer=r["uploader"] or None,
                duration=r["duration"],
            )
    elif path.suffix.lower() == ".mp4":
        with path.open("rb") as f:
            await msg.reply_video(
                video=f,
                duration=r["duration"],
                width=r["width"] if r["width"] and r["height"] else None,
                height=r["height"] if r["width"] and r["height"] else None,
                supports_streaming=True,
            )
    else:
        with path.open("rb") as f:
            await msg.reply_document(document=f)


async def safe_reply(msg, text):
    try:
        await msg.reply_text(text)
    except TelegramError as e:
        log.warning("Не смог ответить: %s", e)


def friendly_error(e):
    t = str(e).lower()
    if "confirm you" in t and "bot" in t:
        return (
            "YouTube не пускает сервер бота 😔 Shorts сейчас скачать не получится. "
            "TikTok, Twitch и SoundCloud работают."
        )
    if any(x in t for x in ("private", "unavailable", "not found", "404", "removed", "deleted")):
        return "Видео недоступно: оно приватное или удалено 🔒"
    return None


async def process(msg, url, kind):
    if kind == "soundcloud" and "/sets/" in url.lower():
        raise UserError("Плейлисты не умею — пришли ссылку на конкретный трек 🎧")

    # TikTok фото-пост — сразу запасной путь (yt-dlp их не умеет)
    if kind == "tiktok" and "/photo/" in urlparse(url).path.lower():
        await send_via_tikwm(msg, url)
        return

    try:
        await msg.chat.send_action(
            ChatAction.UPLOAD_VOICE if kind == "soundcloud" else ChatAction.UPLOAD_VIDEO
        )
    except TelegramError:
        pass

    workdir = tempfile.mkdtemp(prefix="dl_")
    try:
        try:
            result = await download_with_retry(url, kind, workdir)
        except UserError:
            raise
        except Exception as e:  # noqa: BLE001
            if kind == "tiktok":
                log.warning("yt-dlp не справился с TikTok (%s) — пробую запасной вариант", e)
                await send_via_tikwm(msg, url)
                return
            raise
        await send_result(msg, result, kind)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def handle_job(msg, url, kind):
    async with SEM:
        try:
            await process(msg, url, kind)
        except UserError as e:
            await safe_reply(msg, f"😕 {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка при обработке %s", url)
            await safe_reply(
                msg,
                "😕 " + (friendly_error(e) or "Не получилось скачать. Попробуй другую ссылку или чуть позже."),
            )


# ───────────────────────── обработчики Telegram ─────────────────────────


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg:
        await msg.reply_text(GREETING, parse_mode=ParseMode.HTML)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    jobs, seen = [], set()
    for u in extract_urls(msg):
        kind = classify(u)
        if kind and u not in seen:
            seen.add(u)
            jobs.append((u, kind))
    if not jobs:
        return  # обычное сообщение без нужных ссылок — молчим

    # реакция 👀
    try:
        await msg.set_reaction("👀")
    except TelegramError as e:
        log.info("Реакцию поставить не вышло: %s", e)

    for url, kind in jobs[:MAX_LINKS_PER_MESSAGE]:
        await handle_job(msg, url, kind)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Необработанная ошибка: %s", context.error)


# ───────────────────────── веб-сервер для хостинга ─────────────────────────


class Health(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *args):
        pass


def run_health_server():
    ThreadingHTTPServer(("0.0.0.0", PORT), Health).serve_forever()


def keep_alive():
    """Раз в 10 минут стучится на свой публичный адрес, чтобы Render не усыплял сервис."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        time.sleep(600)
        try:
            httpx.get(url, timeout=20)
        except Exception:  # noqa: BLE001
            pass


# ───────────────────────── запуск ─────────────────────────


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(180)  # загрузка видео может идти долго
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler(["start", "help"], on_start))
    app.add_handler(
        MessageHandler(
            (filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST)
            & (filters.TEXT | filters.CAPTION)
            & ~filters.COMMAND,
            on_message,
        )
    )
    app.add_error_handler(on_error)

    log.info("Бот запущен. ffmpeg: %s", FFMPEG or "не найден")
    app.run_polling(allowed_updates=["message", "channel_post"])


if __name__ == "__main__":
    main()
