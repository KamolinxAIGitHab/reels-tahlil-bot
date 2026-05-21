import instaloader
import os
import uuid
import re
import asyncio

L = instaloader.Instaloader(
    download_video_thumbnails=False,
    download_comments=False,
    save_metadata=False,
    post_metadata_txt_pattern=""
)

INSTAGRAM_USER = os.getenv("INSTAGRAM_USERNAME")
INSTAGRAM_PASS = os.getenv("INSTAGRAM_PASSWORD")

try:
    L.load_session_from_file(
        INSTAGRAM_USER,
        'session-reelsbot2026'
    )
except:
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
        post = instaloader.Post.from_shortcode(
            L.context, shortcode
        )
        tmp_dir = os.path.join(output_dir, str(uuid.uuid4()))
        os.makedirs(tmp_dir, exist_ok=True)
        L.download_post(post, target=tmp_dir)

        # Barcha fayllarni ko'rish
        all_files = os.listdir(tmp_dir)
        print(f"DEBUG - Yuklangan fayllar: {all_files}")

        # mp4 qidirish
        for f in all_files:
            if f.endswith(".mp4"):
                return os.path.join(tmp_dir, f)

        # Boshqa video formatlar
        for f in all_files:
            if f.endswith((".mov", ".avi", ".mkv", ".webm")):
                return os.path.join(tmp_dir, f)

        # Xato - fayllar ro'yxati bilan
        raise FileNotFoundError(
            f"Video topilmadi! Mavjud fayllar: {all_files}"
        )

    return await loop.run_in_executor(None, _download)
