import yt_dlp
import os
import uuid
import asyncio
import base64
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
            L.context._session.headers.update({
                'X-IG-App-ID': '936619743392459',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': 'https://www.instagram.com/',
            })
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
    """Instagram Reels videosini yt-dlp orqali yuklaydi va (video_path, caption)
    qaytaradi. Caption instaloader orqali alohida, best-effort tarzda olinadi —
    topilmasa yoki timeout bo'lsa, bo'sh satr qaytariladi."""
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
        for f in os.listdir(tmp_dir):
            if f.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
                return os.path.join(tmp_dir, f)
        raise FileNotFoundError("Video topilmadi!")

    video_path = await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=90)
    try:
        caption = await asyncio.wait_for(loop.run_in_executor(None, _get_caption), timeout=20)
    except asyncio.TimeoutError:
        caption = ""

    return video_path, caption

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
