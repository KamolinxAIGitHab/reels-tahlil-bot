import instaloader
import os
import uuid
import re
import asyncio

L = instaloader.Instaloader(
    download_video_thumbnails=False,
    download_comments=False,
    save_metadata=False,
    post_metadata_txt_pattern="",
    download_videos=True,
    download_geotags=False,
    compress_json=False,
)

INSTAGRAM_USER = os.getenv("INSTAGRAM_USERNAME")
INSTAGRAM_PASS = os.getenv("INSTAGRAM_PASSWORD")

try:
    L.load_session_from_file(
        INSTAGRAM_USER,
        'session-reelsbot2026'
    )
except Exception as e:
    print(f"Session xatosi: {e}")
    L.login(INSTAGRAM_USER, INSTAGRAM_PASS)
    L.save_session_to_file('session-reelsbot2026')

async def download_reels_audio(url: str) -> str:
    output_dir = "downloads"
    os.makedirs(output_dir, exist_ok=True)

    match = re.search(r'/reel/([A-Za-z0-9_-]+)', url)
    if not match:
        raise ValueError("Instagram Reel URL noto'g'ri!")

    shortcode = match.group(1)
    loop = asyncio.get_event_loop()

    def _download():
        post = instalo
