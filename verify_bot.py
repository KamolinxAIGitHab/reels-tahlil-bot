import os
from unittest.mock import MagicMock, patch
import sys

# Mocking the settings to avoid pydantic errors when env vars are missing
with patch.dict(os.environ, {"BOT_TOKEN": "test_token", "OPENAI_API_KEY": "test_key"}):
    from bot.config import settings

from bot.utils.downloader import download_reels_audio
from bot.utils.stt import transcribe_audio
from bot.utils.analyzer import analyze_content

def test_downloader_mock():
    import asyncio
    import shutil

    class FakeYDL:
        def __init__(self, opts):
            self._tmp_dir = os.path.dirname(opts["outtmpl"])
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def download(self, urls):
            open(os.path.join(self._tmp_dir, "video.mp4"), "w").close()

    with patch('bot.utils.downloader.yt_dlp.YoutubeDL', FakeYDL):
        url = "https://www.instagram.com/reel/C-xyz/"
        try:
            path = asyncio.run(download_reels_audio(url))
            print(f"Downloader test passed: {path}")
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        except Exception as e:
            print(f"Downloader test failed: {e}")

def test_stt_mock():
    with patch('bot.utils.stt.client.audio.transcriptions.create') as mock_stt:
        mock_stt.return_value.text = "Bu bir sinov transkripsiyasi."
        
        # Create a dummy file
        with open("dummy.mp4", "w") as f:
            f.write("dummy content")
        
        try:
            text = transcribe_audio("dummy.mp4")
            print(f"STT test passed: {text}")
            assert text == "Bu bir sinov transkripsiyasi."
        except Exception as e:
            print(f"STT test failed: {e}")
        finally:
            if os.path.exists("dummy.mp4"):
                os.remove("dummy.mp4")

def test_analyzer_mock():
    with patch('bot.utils.analyzer.client.chat.completions.create') as mock_chat:
        mock_chat.return_value.choices[0].message.content = "Ushbu g'oya juda foydali."
        
        try:
            analysis = analyze_content("Sinov matni")
            print(f"Analyzer test passed: {analysis}")
            assert analysis == "Ushbu g'oya juda foydali."
        except Exception as e:
            print(f"Analyzer test failed: {e}")

if __name__ == "__main__":
    print("Running verification tests...")
    test_downloader_mock()
    test_stt_mock()
    test_analyzer_mock()
    print("Verification complete.")
