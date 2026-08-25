import logging
import re
import subprocess
from typing import Optional

from openai import OpenAI
from bot.config import settings

client = OpenAI(api_key=settings.openai_api_key)

MIN_DURATION_SECONDS = 1.5
MIN_MEAN_VOLUME_DB = -40.0


class UnclearAudioError(Exception):
    """Audio juda qisqa yoki juda past ovozli — aniq nutq topilmadi."""


def _probe_audio(file_path: str) -> tuple[Optional[float], Optional[float]]:
    result = subprocess.run(
        ["ffmpeg", "-i", file_path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    stderr = result.stderr

    duration = None
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
    if duration_match:
        h, m, s = duration_match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)

    mean_volume = None
    mean_volume_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    if mean_volume_match:
        mean_volume = float(mean_volume_match.group(1))

    return duration, mean_volume


def transcribe_audio(file_path: str, language: str = "uz") -> str:
    """
    Transcribes the audio from the given file path using OpenAI Whisper.
    """
    try:
        duration, mean_volume = _probe_audio(file_path)
        logging.info(f"AUDIO STATS: duration={duration} mean_volume={mean_volume}dB")

        if duration is not None and duration < MIN_DURATION_SECONDS:
            raise UnclearAudioError(f"Audio juda qisqa: {duration:.2f}s")
        if mean_volume is not None and mean_volume < MIN_MEAN_VOLUME_DB:
            raise UnclearAudioError(f"Audio juda past ovozli: {mean_volume:.1f}dB")
    except UnclearAudioError:
        raise
    except Exception as e:
        logging.warning(f"Audio tekshiruvi muvaffaqiyatsiz, davom etilmoqda: {e}")

    with open(file_path, "rb") as audio_file:
        transcription_params = {
            "model": "whisper-1",
            "file": audio_file,
            "temperature": 0,
        }
        if language:
            transcription_params["language"] = language

        response = client.audio.transcriptions.create(**transcription_params)
    return response.text
