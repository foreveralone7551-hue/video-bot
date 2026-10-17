"""
Telegram-бот: качает видео / фото / звук по ссылке — без водяных знаков.
TikTok • Instagram Reels • X (Twitter) • Pinterest • VK • Twitch Clips • SoundCloud
Работает в личке, группах и каналах.
"""
import asyncio
import json
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
FB_UA = "facebookexternalhit/1.1"  # Instagram охотнее отдаёт страницы «роботу Facebook»
TIKWM_API = "https://www.tikwm.com/api/"

# ffmpeg: системный, а если его нет — встроенный из пакета imageio-ffmpeg
FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    try:
        import imageio_ffmpeg

        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        FFMPEG = None

SEM = asyncio.Semaphore(2)  # не больше двух загрузок одновременно (бесплатный сервер слабый)

GREETING = (
    "👋 <b>Привет!</b>\n\n"
    "Чтобы скачать видео, фото или звук — отправь мне ссылку с:\n\n"
    "🎵 TikTok\n"
    "📸 Instagram Reels\n"
    "🐦 X (Twitter)\n"
    "📌 Pinterest\n"
    "💙 VK Видео и Клипы\n"
    "🎮 Twitch Clips\n"
    "🎧 SoundCloud\n\n"
    "✨ Без водяных знаков\n"
    "⚡ Быстро и бесплатно\n"
    "💬 Работаю в личке, группах и каналах\n\n"
    "Жду ссылку 👇"
)

TOO_BIG = "Файл слишком длинный или тяжёлый — лимит Telegram для ботов 50 МБ 📦"
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov"}
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | {".gif"}


class UserError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


# ───────────────────────── ссылки ─────────────────────────

_DOMAINS = (
    r"tiktok\.com|twitch\.tv|soundcloud\.com|snd\.sc"
    r"|twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com|fixvx\.com"
    r"|instagram\.com"
    r"|pinterest\.[a-z]{2,3}(?:\.[a-z]{2})?|pin\.it"
    r"|vk\.com|vk\.ru|vkvideo\.ru"
)
URL_RE = re.compile(
    r"(?<![\w@.\-])(?:https?://)?(?:[\w\-]+\.)*(?:" + _DOMAINS + r")(?:/[^\s<>\"']*)?",
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
    """tiktok | x | instagram | pinterest | vk | twitch | soundcloud | None"""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    path = u.path.lower()
    query = u.query.lower()

    def is_host(*names):
        return any(host == n or host.endswith("." + n) for n in names)

    if is_host("tiktok.com"):
        if host.startswith(("vm.", "vt.")) or any(
            s in path for s in ("/video/", "/photo/", "/t/", "/v/")
        ):
            return "tiktok"
        return None

    if is_host("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com"):
        return "x" if "/status/" in path else None

    if is_host("instagram.com"):
        if any(s in path for s in ("/reel/", "/reels/", "/p/", "/tv/", "/share/")):
            return "instagram"
        return None

    if host == "pin.it" or is_host("pinterest.com") or re.search(r"(^|\.)pinterest\.[a-z.]+$", host):
        return "pinterest" if host == "pin.it" or "/pin/" in path else None

    if is_host("vk.com", "vk.ru", "vkvideo.ru"):
        if any(s in path for s in ("/video", "/clip")) or "z=video" in query or "z=clip" in query:
            return "vk"
        return None

    if host == "clips.twitch.tv" or (is_host("twitch.tv") and "/clip/" in path):
        return "twitch"

    if host == "snd.sc" or is_host("soundcloud.com"):
        # профиль без трека (soundcloud.com/artist) не берём
        if host == "soundcloud.com" and path.strip("/").count("/") < 1:
            return None
        return "soundcloud"

    return None


# ───────────────────────── скачивание (yt-dlp) ─────────────────────────


def download_sync(url, kind, workdir, small=False, ua=None):
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
    if ua:
        opts["http_headers"] = {"User-Agent": ua}

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


async def download_with_retry(url, kind, workdir, ua=None):
    r = await asyncio.to_thread(download_sync, url, kind, workdir, False, ua)
    if r["size"] > MAX_BYTES:
        if kind == "soundcloud":
            raise UserError(TOO_BIG)
        # слишком тяжело — пробуем качество пониже
        for p in Path(workdir).iterdir():
            p.unlink(missing_ok=True)
        r = await asyncio.to_thread(download_sync, url, kind, workdir, True, ua)
        if r["size"] > MAX_BYTES:
            raise UserError(TOO_BIG)
    return r


# ───────────────────────── общие помощники ─────────────────────────


async def stream_download(client, url):
    """Качает файл в память. Возвращает bytes или None, если файл больше лимита Telegram."""
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        cl = r.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BYTES:
            return None
        buf = bytearray()
        async for chunk in r.aiter_bytes(65536):
            buf.extend(chunk)
            if len(buf) > MAX_BYTES:
                return None
        return bytes(buf)


async def send_video_bytes(msg, data, width=None, height=None, duration=None, gif=False):
    if gif:
        await msg.reply_animation(
            animation=data,
            filename="animation.mp4",
            width=width or None,
            height=height or None,
            duration=duration or None,
        )
    else:
        await msg.reply_video(
            video=data,
            filename="video.mp4",
            width=width or None,
            height=height or None,
            duration=duration or None,
            supports_streaming=True,
        )


async def send_photo_blobs(msg, blobs):
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


async def resolve_redirect(url):
    """Раскрывает короткие ссылки (pin.it и т.п.)."""
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers={"User-Agent": UA}
        ) as c:
            r = await c.get(url)
            return str(r.url)
    except httpx.HTTPError:
        return url


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
            await send_photo_blobs(msg, blobs)
            return

        # видео: сначала HD, если тяжёлое — обычное
        for key in ("hdplay", "play"):
            video_url = data.get(key)
            if not video_url:
                continue
            blob = await stream_download(client, _abs(video_url))
            if blob:
                await send_video_bytes(msg, blob, duration=data.get("duration"))
                return
        raise UserError(TOO_BIG)


# ───────────────────────── X (Twitter): через fxtwitter ─────────────────────────
# yt-dlp с серверов у Twitter часто не проходит, поэтому сначала открытый API fxtwitter.


async def send_via_fxtwitter(msg, url):
    """True — отправили; False — не вышло, надо пробовать yt-dlp."""
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return False
    parts = [p for p in urlparse(url).path.split("/") if p]
    name = parts[0] if parts and parts[0].lower() not in ("i", "status") else "i"
    api = f"https://api.fxtwitter.com/{name}/status/{m.group(1)}"

    async with httpx.AsyncClient(
        timeout=40, follow_redirects=True, headers={"User-Agent": UA}
    ) as client:
        try:
            r = await client.get(api)
            r.raise_for_status()
            media = ((r.json() or {}).get("tweet") or {}).get("media") or {}
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.warning("fxtwitter не ответил (%s) — пробую yt-dlp", e)
            return False

        videos = media.get("videos") or []
        photos = media.get("photos") or []
        if not videos and not photos:
            raise UserError("В этом посте нет видео или фото 🤷")

        try:
            if videos:
                v = videos[0]
                variants = [
                    x
                    for x in (v.get("variants") or [])
                    if x.get("url") and "mp4" in str(x.get("content_type", "")).lower()
                ]
                variants.sort(key=lambda x: x.get("bitrate") or 0, reverse=True)
                urls = [x["url"] for x in variants]
                if v.get("url") and v["url"] not in urls:
                    urls.append(v["url"])
                for vu in urls[:4]:  # от лучшего качества к худшему, пока не влезет в 50 МБ
                    blob = await stream_download(client, vu)
                    if blob:
                        await send_video_bytes(
                            msg,
                            blob,
                            width=v.get("width"),
                            height=v.get("height"),
                            duration=int(v.get("duration") or 0),
                            gif=str(v.get("type", "")).lower() == "gif",
                        )
                        return True
                raise UserError(TOO_BIG)

            blobs = []
            for p in photos[:30]:
                if p.get("url"):
                    blob = await stream_download(client, p["url"])
                    if blob:
                        blobs.append(blob)
            if not blobs:
                raise UserError("Не получилось скачать фото из поста 😕")
            await send_photo_blobs(msg, blobs)
            return True
        except httpx.HTTPError as e:
            log.warning("Скачивание с X не вышло (%s) — пробую yt-dlp", e)
            return False


# ───────────────────────── Instagram: запасной вариант (embed) ─────────────────────────


async def send_via_instagram_embed(msg, url):
    """True — отправили; False — не вышло."""
    m = re.search(r"instagram\.com/(?:[\w.\-]+/)?(?:reels?|p|tv)/([\w\-]+)", url)
    if not m:
        return False
    embed = f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/"
    async with httpx.AsyncClient(
        timeout=40, follow_redirects=True, headers={"User-Agent": FB_UA}
    ) as client:
        try:
            r = await client.get(embed)
            r.raise_for_status()
            mm = re.search(r'"video_url"\s*:\s*"([^"]+)"', r.text)
            if not mm:
                return False
            video_url = json.loads('"' + mm.group(1) + '"')
            blob = await stream_download(client, video_url)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Instagram embed не сработал (%s)", e)
            return False
        if blob is None:
            raise UserError(TOO_BIG)
        await send_video_bytes(msg, blob)
        return True


# ───────────────────────── отправка в Telegram ─────────────────────────


async def send_result(msg, r, kind):
    path = r["path"]
    suffix = path.suffix.lower()
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
    elif suffix == ".mp4":
        with path.open("rb") as f:
            await msg.reply_video(
                video=f,
                duration=r["duration"],
                width=r["width"] if r["width"] and r["height"] else None,
                height=r["height"] if r["width"] and r["height"] else None,
                supports_streaming=True,
            )
    elif suffix in IMAGE_EXT:
        with path.open("rb") as f:
            await msg.reply_photo(photo=f)
    elif suffix == ".gif":
        with path.open("rb") as f:
            await msg.reply_animation(animation=f)
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
    if "no video" in t:
        return "В этом посте нет видео (только фото) — такое пока не умею 📷"
    if any(x in t for x in ("login required", "log in", "sign in", "rate-limit", "registered users")):
        return "Эта площадка не отдала видео без входа в аккаунт 🔒 Попробуй позже или другую ссылку."
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

    # X (Twitter): сначала fxtwitter, при неудаче — yt-dlp ниже
    if kind == "x" and await send_via_fxtwitter(msg, url):
        return

    # короткие ссылки pin.it → полный адрес
    if kind == "pinterest" and (urlparse(url).hostname or "").lower() == "pin.it":
        url = await resolve_redirect(url)

    try:
        await msg.chat.send_action(
            ChatAction.UPLOAD_VOICE if kind == "soundcloud" else ChatAction.UPLOAD_VIDEO
        )
    except TelegramError:
        pass

    workdir = tempfile.mkdtemp(prefix="dl_")
    try:
        try:
            if kind == "instagram":
                result, last = None, None
                for ua in (FB_UA, None):  # сначала как «робот Facebook», потом обычно
                    try:
                        result = await download_with_retry(url, kind, workdir, ua)
                        break
                    except UserError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        last = e
                if result is None:
                    raise last
            else:
                result = await download_with_retry(url, kind, workdir)
        except UserError:
            raise
        except Exception as e:  # noqa: BLE001
            if kind == "tiktok":
                log.warning("yt-dlp не справился с TikTok (%s) — пробую запасной вариант", e)
                await send_via_tikwm(msg, url)
                return
            if kind == "instagram":
                log.warning("yt-dlp не справился с Instagram (%s) — пробую embed", e)
                if await send_via_instagram_embed(msg, url):
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
        # Проверяем, что именно написал пользователь
        if msg.text and msg.text.startswith('/help'):
            help_text = (
                "📖 <b>Инструкция по использованию бота</b>\n\n"
                "Бот работает очень просто — ему нужна только ссылка!\n\n"
                "1️⃣ <b>Найдите видео</b>\n"
                "Откройте соцсеть и найдите нужный пост.\n\n"
                "2️⃣ <b>Скопируйте ссылку</b>\n"
                "Нажмите «Поделиться» под видео и выберите «Скопировать ссылку».\n\n"
                "3️⃣ <b>Отправьте боту</b>\n"
                "Вернитесь сюда, вставьте ссылку и отправьте её.\n\n"
                "4️⃣ <b>Получите файл</b> ⚡\n"
                "Подождите пару секунд, бот пришлет видео или аудио без водяных знаков.\n\n"
                "💡 <i>Бот также работает в группах и каналах.</i>\n"
                "Жду вашу первую ссылку! 👇"
            )
            await msg.reply_text(help_text, parse_mode=ParseMode.HTML)
        else:
            # Если это обычный /start, отправляем старое приветствие
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
