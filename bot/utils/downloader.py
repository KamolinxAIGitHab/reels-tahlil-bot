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
