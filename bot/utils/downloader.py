import yt_dlp
import os
import uuid
import asyncio
import base64

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

async def download_reels_audio(url: str) -> str:
    output_dir = "downloads"
    os.makedirs(output_dir, exist_ok=True)

    tmp_dir = os.path.join(output_dir, str(uuid.uuid4()))
    os.makedirs(tmp_dir, exist_ok=True)

    loop = asyncio.get_event_loop()

    def _download():
        with yt_dlp.YoutubeDL(_build_ydl_opts(tmp_dir)) as ydl:
            ydl.download([url])

        for f in os.listdir(tmp_dir):
            if f.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
                return os.path.join(tmp_dir, f)

        raise FileNotFoundError("Video topilmadi! yt-dlp yuklay olmadi.")

    return await loop.run_in_executor(None, _download)

async def download_instagram_image(url: str):
    import instaloader
    import re

    output_dir = "downloads"
    os.makedirs(output_dir, exist_ok=True)
    tmp_dir = os.path.join(output_dir, str(uuid.uuid4()))
    os.makedirs(tmp_dir, exist_ok=True)

    loop = asyncio.get_event_loop()

    def _download():
        L = instaloader.Instaloader(
            download_pictures=True,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            quiet=True,
            dirname_pattern=tmp_dir,
        )

        cookie_jar = {}
        if _COOKIES_FILE:
            try:
                import http.cookiejar
                jar = http.cookiejar.MozillaCookieJar(_COOKIES_FILE)
                jar.load()
                for cookie in jar:
                    if "instagram" in cookie.domain:
                        cookie_jar[cookie.name] = cookie.value
                if cookie_jar:
                    L.context._session.cookies.update(cookie_jar)
            except Exception:
                pass

        match = re.search(r'/p/([A-Za-z0-9_-]+)', url)
        if not match:
            raise ValueError("Instagram post URL notogri")
        shortcode = match.group(1)

        post = instaloader.Post.from_shortcode(L.context, shortcode)
        caption = post.caption or ""
        images = []

        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                if not node.is_video:
                    img_path = os.path.join(tmp_dir, f"{node.shortcode}.jpg")
                    L.download_pic(img_path, node.display_url, post.date_utc)
                    images.append(img_path)
        elif post.typename == "GraphImage":
            img_path = os.path.join(tmp_dir, f"{shortcode}.jpg")
            L.download_pic(img_path, post.url, post.date_utc)
            images.append(img_path)

        if not images:
            raise ValueError("Rasmlar topilmadi")

        return images, caption

    return await loop.run_in_executor(None, _download)
