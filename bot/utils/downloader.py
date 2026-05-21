import instaloader
import os
import uuid
import re

# Bot Instagram akkauntiga login (bir marta)
L = instaloader.Instaloader(
    download_video_thumbnails=False,
    download_comments=False,
    save_metadata=False,
    post_metadata_txt_pattern=""
)

# Environment variable dan login
INSTAGRAM_USER = os.getenv("INSTAGRAM_USERNAME")
INSTAGRAM_PASS = os.getenv("INSTAGRAM_PASSWORD")

if INSTAGRAM_USER and INSTAGRAM_PASS:
    L.login(INSTAGRAM_USER, INSTAGRAM_PASS)

def download_reels_audio(url: str) -> str:
    """
    Instagram Reel ni yuklab, mp4 faylni qaytaradi.
    OpenAI Whisper mp4 ni to'g'ridan-to'g'ri qabul qiladi.
    """
    output_dir = "downloads"
    os.makedirs(output_dir, exist_ok=True)

    # Shortcode ni URLdan olish
    # https://www.instagram.com/reel/ABC123/...
    match = re.search(r'/reel/([A-Za-z0-9_-]+)', url)
    if not match:
        raise ValueError("Instagram Reel URL noto'g'ri!")
    
    shortcode = match.group(1)
    
    # Post ma'lumotlarini olish
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    
    # Vaqtinchalik papkaga yuklash
    tmp_dir = os.path.join(output_dir, str(uuid.uuid4()))
    os.makedirs(tmp_dir, exist_ok=True)
    
    L.download_post(post, target=tmp_dir)
    
    # mp4 faylni topish
    for f in os.listdir(tmp_dir):
        if f.endswith(".mp4"):
            return os.path.join(tmp_dir, f)
    
    raise FileNotFoundError("Video fayl yuklanmadi!")
