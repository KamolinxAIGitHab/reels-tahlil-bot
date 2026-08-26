import yt_dlp
import os
import uuid
import asyncio
import base64
import subprocess
import http.cookiejar
import instaloader

INSTAGRAM_USER    = os.getenv("INSTAGRAM_USERNAME")
INSTAGRAM_PASS    = os.getenv("INSTAGRAM_PASSWORD")
INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES")

_LOCAL_COOKIES = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "cookies.txt")
)

def _resolve_cookies_file() -> str | None:
    # 1. INSTAGRAM_COOKIES env var — base64 decode qilib /tmp ga yoz
    if INSTAGRAM_COOKIES:
        tmp_path = "/tmp/cookies.txt"
        try:
            decoded = base64.b64decode(INSTAGRAM_COOKIES)
        except Exception:
            decoded = INSTAGRAM_COOKIES.encode()
        with open(tmp_path, "wb") as f:
            f.write(decoded)
        return tmp_path

    # 2. Loyiha ildizidagi cookies fayli (lokal ishlatish uchun)
    if os.path.exists(_LOCAL_COOKIES):
        return _LOCAL_COOKIES

    return None

_COOKIES_FILE = _resolve_cookies_file()

def _build_ydl_opts(tmp_dir: str) -> dict:
    opts = {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": "mp4/bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }
    if _COOKIES_FILE:
        opts["cookiefile"] = _COOKIES_FILE
    elif INSTAGRAM_USER and INSTAGRAM_PASS:
        opts["username"] = INSTAGRAM_USER
        opts["password"] = INSTAGRAM_PASS
    return opts

def _load_cookies_into(L: "instaloader.Instaloader") -> None:
    """cookies.txt (Netscape format) dagi instagram cookie'larini
    instaloader sessiyasiga yuklaydi."""
    if not _COOKIES_FILE:
        return
    try:
        jar = http.cookiejar.MozillaCookieJar(_COOKIES_FILE)
        jar.load()
        cookie_dict = {c.name: c.value for c in jar if "instagram" in c.domain}
        if cookie_dict:
            L.context._session.cookies.update(cookie_dict)
            headers = {
                'X-IG-App-ID': '936619743392459',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': 'https://www.instagram.com/',
            }
            if 'csrftoken' in cookie_dict:
                headers['X-CSRFToken'] = cookie_dict['csrftoken']
            L.context._session.headers.update(headers)

            # instaloader'ning is_logged_in xossasi faqat context.username'ga
            # qaraydi (cookie'larga emas!) — shuning uchun Profile.get_posts()
            # bizni "anonim" deb hisoblab, qattiq cheklangan endpoint'ni
            # tanlaydi. Haqiqiy login holatini tekshirib, username'ni
            # qo'lda o'rnatamiz — shu orqali get_posts() avtorizatsiyalangan
            # (ancha kengroq ruxsatli) yo'lni tanlaydi.
            username = L.context.test_login()
            if username:
                L.context.username = username
    except Exception:
        pass

def _new_instaloader(**extra_opts) -> "instaloader.Instaloader":
    """Barcha Instagram funksiyalari uchun umumiy Instaloader instance.
    Ichki retry/backoff o'chirilgan (sleep=False, max_connection_attempts=1)
    va qat'iy request_timeout qo'yilgan — Instagram javob bermay qolganda
    funksiya minutlab osilib qolmasligi uchun."""
    opts = dict(
        quiet=True,
        sleep=False,
        max_connection_attempts=1,
        request_timeout=15.0,
    )
    opts.update(extra_opts)
    L = instaloader.Instaloader(**opts)
    _load_cookies_into(L)
    return L

async def download_reels_audio(url: str):
    """Instagram Reels videosini yt-dlp orqali yuklaydi, undan ffmpeg bilan
    audio yo'lakchasini ajratib oladi (Whisper API'ning 25MB chegarasidan
    oshib ketmaslik uchun) va (audio_path, caption) qaytaradi. Caption
    instaloader orqali alohida, best-effort tarzda olinadi — topilmasa
    yoki timeout bo'lsa, bo'sh satr qaytariladi."""
    import re

    output_dir = "downloads"
    os.makedirs(output_dir, exist_ok=True)
    tmp_dir = os.path.join(output_dir, str(uuid.uuid4()))
    os.makedirs(tmp_dir, exist_ok=True)

    loop = asyncio.get_event_loop()

    def _get_caption():
        try:
            L = _new_instaloader()
            match = re.search(r'/reel/([A-Za-z0-9_-]+)', url)
            if match:
                shortcode = match.group(1)
                post = instaloader.Post.from_shortcode(L.context, shortcode)
                return post.caption or ""
        except Exception:
            return ""

    def _download():
        with yt_dlp.YoutubeDL(_build_ydl_opts(tmp_dir)) as ydl:
            ydl.download([url])

        video_path = None
        for f in os.listdir(tmp_dir):
            if f.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
                video_path = os.path.join(tmp_dir, f)
                break
        if not video_path:
            raise FileNotFoundError("Video topilmadi!")

        # Whisper API 25MB (26214400 bayt) chegarasiga ega — to'liq videoni
        # (video+audio) emas, faqat siqilgan audio yo'lakchasini yuboramiz,
        # bu hajmni ~10-20 barobar kamaytiradi va 413 xatosining oldini oladi.
        audio_path = os.path.join(tmp_dir, "audio.mp3")
        subprocess.run(
            ["ffmpeg", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", audio_path],
            capture_output=True,
            timeout=60,
        )
        os.remove(video_path)

        if not os.path.exists(audio_path):
            raise FileNotFoundError("Audio ajratib olinmadi!")

        return audio_path

    audio_path = await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=90)
    try:
        caption = await asyncio.wait_for(loop.run_in_executor(None, _get_caption), timeout=20)
    except asyncio.TimeoutError:
        caption = ""

    return audio_path, caption


class VideoTooLongError(Exception):
    """YouTube video belgilangan maksimal davomiylikdan (odatda 60
    soniya) uzun bo'lganda ko'tariladi."""
    def __init__(self, duration: int):
        self.duration = duration
        super().__init__(f"Video juda uzun: {duration} soniya")


class AudioExtractionFailedError(Exception):
    """YouTube'dan yuklab olingan video/audio yaroqsiz (chala yoki
    buzilgan stream) bo'lib chiqqanda, barcha player_client
    fallback'lari sinab ko'rilgandan keyin ham ko'tariladi."""


# YouTube'ning "Sign in to confirm you're not a bot" tekshiruvini
# chetlab o'tish uchun bir nechta player_client'lar ketma-ket sinaladi —
# "android" ba'zi videolar uchun chala/uzilgan stream qaytarishi mumkin
# (yt-dlp/YouTube'ning bilinadigan nomuvofiqligi), shunda navbatdagisiga
# o'tiladi.
_YOUTUBE_PLAYER_CLIENTS = ("android", "web_creator", "mweb")


def _build_youtube_ydl_opts(tmp_dir: str, player_client: str) -> dict:
    # Instagram cookiefile/login ma'lumotlari YouTube uchun kerak emas
    # va tegishli emas — shuning uchun _build_ydl_opts qayta ishlatilmaydi.
    return {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": "mp4/bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "extractor_args": {"youtube": {"player_client": [player_client]}},
    }


async def download_youtube_shorts(url: str, max_duration: int = 60):
    """YouTube Shorts (yoki max_duration soniyagacha bo'lgan youtu.be/
    watch videosi)ni yt-dlp orqali yuklaydi, Instagram Reels bilan bir
    xil pipeline'da ffmpeg bilan audio yo'lakchasini ajratib oladi va
    (audio_path, caption) qaytaradi. Video max_duration'dan uzun bo'lsa,
    yuklab olishdan oldin (faqat metadata so'ralib) VideoTooLongError
    ko'taradi. Ajratilgan audio yaroqsiz (chala/buzilgan) chiqsa,
    Whisper'ga yuborilmasdan, boshqa player_client bilan qayta uriniladi;
    barchasi muvaffaqiyatsiz bo'lsa AudioExtractionFailedError ko'taradi."""
    from bot.utils.stt import _probe_audio

    output_dir = "downloads"
    os.makedirs(output_dir, exist_ok=True)
    tmp_dir = os.path.join(output_dir, str(uuid.uuid4()))
    os.makedirs(tmp_dir, exist_ok=True)

    loop = asyncio.get_event_loop()

    def _download():
        info = None
        last_probe_error = None
        for player_client in _YOUTUBE_PLAYER_CLIENTS:
            try:
                probe_opts = {
                    "quiet": True, "no_warnings": True, "skip_download": True,
                    "extractor_args": {"youtube": {"player_client": [player_client]}},
                }
                with yt_dlp.YoutubeDL(probe_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                break
            except Exception as e:
                last_probe_error = e
                continue

        if info is None:
            raise AudioExtractionFailedError(
                f"Video ma'lumotini olib bo'lmadi (barcha client'lar rad etdi): {last_probe_error}"
            )

        duration = info.get("duration") or 0
        if duration > max_duration:
            raise VideoTooLongError(int(duration))

        caption = info.get("description") or info.get("title") or ""

        for player_client in _YOUTUBE_PLAYER_CLIENTS:
            try:
                with yt_dlp.YoutubeDL(_build_youtube_ydl_opts(tmp_dir, player_client)) as ydl:
                    ydl.download([url])
            except Exception:
                continue

            video_path = None
            for f in os.listdir(tmp_dir):
                if f.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
                    video_path = os.path.join(tmp_dir, f)
                    break
            if not video_path:
                continue

            audio_path = os.path.join(tmp_dir, "audio.mp3")
            subprocess.run(
                ["ffmpeg", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", audio_path],
                capture_output=True,
                timeout=60,
            )
            os.remove(video_path)

            if not os.path.exists(audio_path):
                continue

            probed_duration, _ = _probe_audio(audio_path)
            if probed_duration is None:
                os.remove(audio_path)
                continue

            return audio_path, caption

        raise AudioExtractionFailedError(
            f"Barcha player_client fallback'lari ({', '.join(_YOUTUBE_PLAYER_CLIENTS)}) yaroqli audio bermadi"
        )

    return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=90)

async def download_instagram_image(url: str):
    """Instagram post (/p/...) rasmlarini yuklaydi — bitta rasm yoki karusel
    (GraphSidecar) bo'lishi mumkin. (images: list[str], caption: str) qaytaradi."""
    import re
    import urllib.request

    output_dir = "downloads"
    os.makedirs(output_dir, exist_ok=True)
    tmp_dir = os.path.join(output_dir, str(uuid.uuid4()))
    os.makedirs(tmp_dir, exist_ok=True)

    loop = asyncio.get_event_loop()

    def _download():
        L = _new_instaloader(
            download_pictures=True,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            dirname_pattern=tmp_dir,
        )

        match = re.search(r'/p/([A-Za-z0-9_-]+)', url)
        if not match:
            raise ValueError("Instagram post URL notogri")
        shortcode = match.group(1)

        post = instaloader.Post.from_shortcode(L.context, shortcode)
        caption = post.caption or ""
        images = []

        if post.typename == "GraphSidecar":
            for i, node in enumerate(post.get_sidecar_nodes()):
                if not node.is_video:
                    img_path = os.path.join(tmp_dir, f"slide_{i}.jpg")
                    urllib.request.urlretrieve(node.display_url, img_path)
                    images.append(img_path)
        elif post.typename == "GraphImage":
            img_path = os.path.join(tmp_dir, f"{shortcode}.jpg")
            urllib.request.urlretrieve(post.url, img_path)
            images.append(img_path)

        if not images:
            raise ValueError("Rasmlar topilmadi")

        return images, caption

    return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=45)

async def download_account_posts(username: str) -> list:
    """
    Instagram akkountidan ohirgi 6 postni yuklaydi.
    Har post uchun caption, typename, likes, comments, date, url qaytaradi.
    """
    import itertools
    import time

    loop = asyncio.get_event_loop()

    def _fetch():
        L = _new_instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
        )

        # web_profile_info endpoint'i 429'ni tez-tez qaytaradi (hatto
        # autentifikatsiyalangan sessiyada ham) — sleep=False tufayli
        # instaloader'ning ichki retry'i o'chirilgan, shuning uchun
        # bu yerda qo'lda backoff bilan qayta urinamiz.
        profile = None
        last_exc = None
        for delay in (0, 5, 15):
            if delay:
                time.sleep(delay)
            try:
                profile = instaloader.Profile.from_username(L.context, username)
                break
            except Exception as e:
                last_exc = e
                msg = str(e).lower()
                if not any(k in msg for k in ("429", "too many", "wait a few minutes")):
                    raise
        if profile is None:
            raise last_exc

        posts = []

        for post in itertools.islice(profile.get_posts(), 6):
            posts.append({
                "caption": post.caption or "",
                "typename": post.typename,
                "likes": post.likes,
                "comments": post.comments,
                "date": str(post.date_utc)[:10],
                "url": f"https://www.instagram.com/p/{post.shortcode}/",
            })
            time.sleep(2)  # har post orasida 2 soniya kutish

        return posts, profile.biography or ""

    return await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=90)
