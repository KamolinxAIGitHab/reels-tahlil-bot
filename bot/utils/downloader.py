import yt_dlp
import os
import uuid
import time
import shutil
import asyncio
import base64
import subprocess
import http.cookiejar
import logging
import re
import instaloader
from googleapiclient.discovery import build
from bot.config import settings, YOUTUBE_API_KEY

INSTAGRAM_USER    = settings.instagram_username
INSTAGRAM_PASS    = os.getenv("INSTAGRAM_PASSWORD")
INSTAGRAM_COOKIES = settings.instagram_cookies

_LOCAL_COOKIES = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "cookies.txt")
)


class CookieExpiredError(Exception):
    """Instagram cookie eskirgan yoki yaroqsiz bo'lganda (LoginRequired
    yoki 401 javobi) ko'tariladi — cookies.txt'ni yangilash kerakligini
    bildiradi (qarang: COOKIE_YANGILASH.md)."""


class PrivateContentError(Exception):
    """Instagram kontenti xususiy (private) profildan bo'lgani va unga
    kirish cheklangani sababli cookie'siz ham, cookie bilan ham
    yuklanmaganda ko'tariladi."""


class VideoPostError(Exception):
    """Instagram post (/p/...) rasm emas, video ekanida ko'tariladi —
    chaqiruvchi uni Reels yuklash yo'liga o'tkazishi kerak."""


# Instagram cookie yaroqsiz bo'lganini bildiruvchi aniq belgilar. Eslatma:
# yalang'och "401" qidirilmaydi — u shortcode yoki URL ichida tasodifan
# uchrashi mumkin (masalan "...Db401x...").
_COOKIE_ERROR_MARKERS = (
    "http error 401",
    "401: unauthorized",
    "login required",
    "login_required",
    "please log in",
    "redirected to the login page",
    "checkpoint_required",
    "challenge_required",
)

# Haqiqiy so'rovlar limiti belgilari. Yalang'och "429" va "limit" so'zi
# ataylab olib tashlangan — ular boshqa xatolarni ham "limit" deb ko'rsatardi.
_RATE_LIMIT_MARKERS = (
    "http error 429",
    "too many requests",
    "rate-limit",
    "rate limit",
    "ratelimit",
    "wait a few minutes",
)

# Cookie yaroqsiz deb topilgach, shu vaqt davomida Instagram'ga umuman
# so'rov yuborilmaydi — o'lik cookie bilan qayta-qayta so'rov yuborish
# akkauntni xavf ostiga qo'yadi (yangi cookie qo'yilganda bot qayta
# ishga tushadi va holat tozalanadi).
COOKIE_COOLDOWN_SECONDS = 30 * 60
_cookie_dead_until = 0.0

# Instagram'ga barcha so'rovlar navbat bilan yuboriladi (bir vaqtda bittadan),
# ketma-ket so'rovlar orasida kamida shuncha soniya kutiladi — server IP'dan
# bir zumda ko'p so'rov yuborish 429 cheklovini tezlashtiradi.
INSTAGRAM_MIN_GAP_SECONDS = 20
_instagram_lock = None
_instagram_last_end = 0.0


class _InstagramGate:
    async def __aenter__(self):
        global _instagram_lock
        if _instagram_lock is None:
            _instagram_lock = asyncio.Lock()
        await _instagram_lock.acquire()
        wait = INSTAGRAM_MIN_GAP_SECONDS - (time.monotonic() - _instagram_last_end)
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aexit__(self, *exc):
        global _instagram_last_end
        _instagram_last_end = time.monotonic()
        _instagram_lock.release()


def cookie_in_cooldown() -> bool:
    """Cookie yaqinda yaroqsiz deb topilgan va hali kutish vaqti
    tugamaganmi, shuni qaytaradi."""
    return time.monotonic() < _cookie_dead_until


def _mark_cookie_dead() -> None:
    global _cookie_dead_until
    _cookie_dead_until = time.monotonic() + COOKIE_COOLDOWN_SECONDS
    logging.error(
        f"Instagram cookie yaroqsiz deb belgilandi — {COOKIE_COOLDOWN_SECONDS // 60} "
        f"daqiqa davomida cookie ishlatilmaydi"
    )


# Cookie'ni "o'lik" deb belgilash uchun bitta havoladagi xato yetarli emas:
# "login required" kabi xatolar ba'zi havolalarda (yoshga cheklangan kontent va h.k.)
# cookie yaroqli bo'lsa ham chiqadi. Shuning uchun bunday xato 10 daqiqa ichida
# kamida 2 ta turli havolada takrorlanishi kerak. Hisob darajasidagi aniq belgilar
# (401, checkpoint, challenge) darhol hisobga olinadi.
COOKIE_FAIL_WINDOW_SECONDS = 10 * 60
COOKIE_FAIL_DISTINCT_URLS = 2
_STRONG_COOKIE_MARKERS = (
    "http error 401",
    "401: unauthorized",
    "checkpoint_required",
    "challenge_required",
)
_cookie_failures: dict[str, float] = {}


def _should_mark_cookie_dead(url: str, error: Exception) -> bool:
    msg = str(error).lower()
    if any(k in msg for k in _STRONG_COOKIE_MARKERS):
        return True
    now = time.monotonic()
    for key in [k for k, t in _cookie_failures.items() if now - t > COOKIE_FAIL_WINDOW_SECONDS]:
        _cookie_failures.pop(key, None)
    _cookie_failures[(url or "").split("?")[0]] = now
    return len(_cookie_failures) >= COOKIE_FAIL_DISTINCT_URLS


# Reels/video post uchun eng uzun davomiylik: undan uzun video yuklanmaydi
# (OpenAI STT xarajati va 25 MB chegarasi uchun). REEL_MAX_SECONDS bilan o'zgartiriladi.
MAX_REEL_SECONDS = int(os.environ.get("REEL_MAX_SECONDS", "300"))


class ReelTooLongError(Exception):
    """Video belgilangan davomiylikdan uzun."""

    def __init__(self, duration):
        self.duration = duration
        super().__init__(f"Video uzunligi {duration} soniya, chegara {MAX_REEL_SECONDS}")


def _is_cookie_expired(e: Exception) -> bool:
    """Berilgan xato Instagram cookie yaroqsiz bo'lganini bildiradimi
    (LoginRequired, HTTP 401, login sahifasiga yo'naltirish va h.k.)."""
    if isinstance(e, instaloader.LoginRequiredException):
        return True
    msg = str(e).lower()
    return any(k in msg for k in _COOKIE_ERROR_MARKERS)


def is_rate_limited(e: Exception) -> bool:
    """Berilgan xato Instagram'ning haqiqiy so'rovlar limitini
    bildiradimi, shuni tekshiradi."""
    msg = str(e).lower()
    return any(k in msg for k in _RATE_LIMIT_MARKERS)


def _is_private_content(e: Exception) -> bool:
    """Berilgan xato Instagram kontenti xususiy (private) profildan
    ekanini bildiradimi, shuni tekshiradi."""
    msg = str(e).lower()
    return any(k in msg for k in ("private", "restricted video", "not available"))

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

def _build_ydl_opts(tmp_dir: str, use_cookies: bool = True) -> dict:
    opts = {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": "mp4/bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }
    if not use_cookies:
        return opts
    if _COOKIES_FILE:
        # Har so'rov uchun alohida nusxa: yt-dlp cookie faylni qayta yozadi,
        # parallel so'rovlar umumiy faylni buzmasligi uchun.
        local_copy = os.path.join(tmp_dir, "cookies.txt")
        try:
            shutil.copyfile(_COOKIES_FILE, local_copy)
            opts["cookiefile"] = local_copy
        except OSError:
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

# Muallif ma'lumoti: yuklash paytida yt-dlp javobidan olinadi va chaqiruvchi pop_reel_author()
# bilan oladi (download_*() qaytarish formati o'zgarmasligi uchun). Faqat tekshirilgan
# qiymatlar saqlanadi: Instagram nomi qat'iy andoza bilan, YouTube kanali faqat youtube.com manzili.
_AUTHORS: dict[str, tuple[str, str]] = {}
_IG_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_YT_CHANNEL_RE = re.compile(r"^https://(?:www\.)?youtube\.com/[A-Za-z0-9_@./%\-]{1,120}$")


def _remember_author(url: str, display: str, link: str) -> None:
    if len(_AUTHORS) >= 200:
        _AUTHORS.pop(next(iter(_AUTHORS)), None)
    _AUTHORS[url] = (display, link)


def _author_from_instagram(url: str, info) -> None:
    uid = info.get("uploader_id") if isinstance(info, dict) else None
    # Faqat raqamlardan iborat qiymat — foydalanuvchi nomi emas, ichki ID bo'lishi mumkin: havola noto'g'ri chiqadi.
    if isinstance(uid, str) and _IG_HANDLE_RE.match(uid) and not uid.isdigit():
        _remember_author(url, "@" + uid, f"https://www.instagram.com/{uid}/")


def _author_from_youtube(url: str, info) -> None:
    if not isinstance(info, dict):
        return
    link = info.get("channel_url") or info.get("uploader_url")
    name = info.get("channel") or info.get("uploader") or ""
    if isinstance(link, str) and _YT_CHANNEL_RE.match(link):
        display = str(name).strip()[:60] or link.rsplit("/", 1)[-1]
        _remember_author(url, display, link)


def pop_reel_author(url: str):
    """Oxirgi yuklashda aniqlangan muallif: (ko'rinadigan nom, havola) yoki None."""
    return _AUTHORS.pop(url, None)


async def download_reels_audio(url: str):
    """Instagram Reels videosini yt-dlp orqali yuklaydi, undan ffmpeg bilan
    audio yo'lakchasini ajratib oladi (Whisper API'ning 25MB chegarasidan
    oshib ketmaslik uchun) va (audio_path, caption, used_cookie) qaytaradi.

    So'rovlar soni kamaytirilgan: cookie bilan boshlanadi (anonim so'rovlar
    server IP'sidan doim limitga uchraydi), caption esa alohida so'rovsiz,
    yt-dlp'ning o'z javobidan ('description') olinadi. Cookie yaroqsiz bo'lsa,
    bir marta anonim urinib ko'riladi va cookie 30 daqiqaga 'o'lik' deb
    belgilanadi — shu vaqt ichida faqat anonim urinish bo'ladi.
    used_cookie — cookie ishlatilgan so'rovni bildiradi (statistika uchun).
    Caption topilmasa, bo'sh satr qaytariladi."""
    output_dir = "downloads"
    os.makedirs(output_dir, exist_ok=True)
    tmp_dir = os.path.join(output_dir, str(uuid.uuid4()))
    os.makedirs(tmp_dir, exist_ok=True)

    loop = asyncio.get_event_loop()

    def _extract(use_cookies: bool) -> str:
        """Videoni yuklaydi va caption'ni qaytaradi."""
        too_long = []

        def _duration_filter(info, *, incomplete=False):
            duration = info.get("duration")
            if duration and duration > MAX_REEL_SECONDS:
                too_long.append(duration)
                return "video juda uzun"
            return None

        opts = _build_ydl_opts(tmp_dir, use_cookies=use_cookies)
        opts["match_filter"] = _duration_filter
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if too_long:
            raise ReelTooLongError(too_long[0])
        _author_from_instagram(url, info)
        if isinstance(info, dict):
            return (info.get("description") or "").strip()
        return ""

    def _download():
        has_cookie_source = bool(_COOKIES_FILE or (INSTAGRAM_USER and INSTAGRAM_PASS))

        if has_cookie_source and cookie_in_cooldown():
            # Cookie yaqinda yaroqsiz deb topilgan — Instagram'ga so'rov yubormaymiz
            raise CookieExpiredError("Instagram cookie yaroqsiz (cooldown)")

        def _extract_with_retry() -> str:
            try:
                return _extract(has_cookie_source)
            except Exception as e:
                if is_rate_limited(e) and not _is_cookie_expired(e):
                    logging.warning("Instagram 429 — 25 soniya kutib, bir marta qayta uriniladi")
                    time.sleep(25)
                    return _extract(has_cookie_source)
                raise

        used_cookie = has_cookie_source
        try:
            caption = _extract_with_retry()
            _cookie_failures.clear()
        except Exception as e:
            if _is_cookie_expired(e):
                logging.error(f"Instagram cookie xatosi (video yuklashda): {e}")
                if has_cookie_source and _should_mark_cookie_dead(url, e):
                    _mark_cookie_dead()
                raise CookieExpiredError("Instagram cookie eskirgan (video yuklashda)") from e
            if _is_private_content(e):
                raise PrivateContentError("Instagram kontenti xususiy (private)") from e
            raise

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

        return audio_path, caption, used_cookie

    try:
        async with _InstagramGate():
            return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=150)
    except BaseException:
        # Xato bo'lsa, yarim yuklangan fayllar diskda qolib ketmasin
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


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


def get_youtube_subtitles(video_id: str) -> str | None:
    """YouTube Data API v3 orqali video subtitr mavjudligini tekshiradi.

    ESLATMA: captions.download() endpoint'i Google tomonidan faqat
    OAuth2 orqali ruxsat etiladi — oddiy API kalit bilan har doim
    401 "Login Required" xatosi qaytaradi (sinovda tasdiqlangan).
    Shuning uchun bu funksiya hozircha subtitr matnini yuklab olmaydi
    va topilgan-topilmaganidan qat'i nazar har doim None qaytaradi —
    chaqiruvchi tomon bu holatda yt-dlp+Whisper pipeline'iga xabarsiz
    o'tadi. Kelajakda OAuth2 qo'shilganda captions().download()
    chaqiruvi shu joyga qaytariladi."""
    if not YOUTUBE_API_KEY:
        return None

    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        response = youtube.captions().list(part="snippet", videoId=video_id).execute()
        items = response.get("items", [])
        if items:
            logging.info(
                f"YouTube subtitr mavjud (video_id={video_id}), lekin OAuth2 "
                f"yo'qligi sababli yuklab olinmaydi — yt-dlp'ga o'tiladi"
            )
        return None
    except Exception as e:
        logging.error(f"YouTube subtitr tekshirishda xato (video_id={video_id}): {e}")
        return None


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
        _author_from_youtube(url, info)

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
        if cookie_in_cooldown():
            # Post oqimi cookie'siz ishlamaydi — o'lik cookie bilan so'rov yubormaymiz
            raise CookieExpiredError("Instagram cookie yaroqsiz (post, cooldown)")

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

        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            caption = post.caption or ""
            images = []
            has_video = False

            if post.typename == "GraphVideo":
                # /p/ havolasi aslida video — Reels yo'liga o'tkaziladi
                raise VideoPostError(url)
            elif post.typename == "GraphSidecar":
                for i, node in enumerate(post.get_sidecar_nodes()):
                    if node.is_video:
                        has_video = True
                        continue
                    img_path = os.path.join(tmp_dir, f"slide_{i}.jpg")
                    urllib.request.urlretrieve(node.display_url, img_path)
                    images.append(img_path)
            elif post.typename == "GraphImage":
                img_path = os.path.join(tmp_dir, f"{shortcode}.jpg")
                urllib.request.urlretrieve(post.url, img_path)
                images.append(img_path)
        except VideoPostError:
            raise
        except Exception as e:
            if _is_cookie_expired(e):
                logging.error(f"Instagram cookie xatosi (post o'qishda): {e}")
                if _should_mark_cookie_dead(url, e):
                    _mark_cookie_dead()
                raise CookieExpiredError("Instagram cookie eskirgan (post o'qishda)") from e
            raise

        if not images:
            if has_video:
                raise VideoPostError(url)
            raise ValueError("Rasmlar topilmadi")

        return images, caption

    try:
        async with _InstagramGate():
            return await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=45)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

async def download_account_posts(username: str) -> list:
    """
    Instagram akkountidan ohirgi 3 postni yuklaydi.
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
        for delay in (0, 60, 180):
            if delay:
                time.sleep(delay)
            try:
                profile = instaloader.Profile.from_username(L.context, username)
                break
            except Exception as e:
                if _is_cookie_expired(e):
                    raise CookieExpiredError("Instagram cookie eskirgan (profil o'qishda)") from e
                last_exc = e
                msg = str(e).lower()
                if not any(k in msg for k in (
                    "429", "too many", "wait a few minutes",
                    "read timed out", "connection"
                )):
                    raise
        if profile is None:
            raise last_exc

        posts = []

        try:
            for post in itertools.islice(profile.get_posts(), 3):
                posts.append({
                    "caption": post.caption or "",
                    "typename": post.typename,
                    "likes": post.likes,
                    "comments": post.comments,
                    "date": str(post.date_utc)[:10],
                    "url": f"https://www.instagram.com/p/{post.shortcode}/",
                })
                time.sleep(2)  # har post orasida 2 soniya kutish
        except Exception as e:
            if _is_cookie_expired(e):
                raise CookieExpiredError("Instagram cookie eskirgan (postlarni o'qishda)") from e
            raise

        return posts, profile.biography or ""

    return await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=300)
