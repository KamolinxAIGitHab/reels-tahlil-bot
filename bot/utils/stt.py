import logging
import os
import re
import subprocess
from typing import Optional

from openai import OpenAI
from bot.config import settings
from bot.utils import cost

client = OpenAI(api_key=settings.openai_api_key, timeout=90.0, max_retries=2)

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


# gpt-4o-transcribe kabi boshqa modelni sinash uchun: STT_MODEL muhit o'zgaruvchisi
# Ўзбек нутқи учун синовда gpt-4o-mini-transcribe gpt-4o-transcribe ва whisper-1 дан аниқроқ чиқди
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-mini-transcribe")
_PROMPT_MAX_CHARS = 600

# O'zbekcha nutq til berilmaganda ba'zan boshqa tur (masalan, ozarbayjon)
# imlosida yoziladi. Neytral ko'rsatma modelni o'zbek lotin imlosiga yo'naltiradi,
# lekin rus/ingliz nutqni majburan o'zgartirmaydi. STT_HINT=0 bilan o'chiriladi.
_LANG_HINT = "Nutq o'zbek, rus yoki ingliz tilida bo'lishi mumkin. O'zbekcha bo'lsa, o'zbek lotin imlosida yozing: o', g', sh, ch."
_LANG_HINT_MARK = "lotin imlosida"
# Синовда кўрсатмасиз вариант энг барқарор чиққани учун кўрсатма ўчирилган; STT_HINT=1 билан ёқилади
_USE_HINT = os.getenv("STT_HINT", "0") == "1"


def transcribe_audio(file_path: str, language: Optional[str] = None, prompt: Optional[str] = None) -> str:
    """
    Transcribes the audio from the given file path using OpenAI Whisper.

    language — aniq til kodi (masalan "ru"); None bo'lsa, til avtomatik
    aniqlanadi. prompt — odatda Reels caption'i: undagi brend va nomlar
    Whisper'ga imlo bo'yicha yordam beradi.
    """
    duration = None
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

    def _call(lang: Optional[str], want_language: bool, use_temperature: bool = True):
        params = {"model": STT_MODEL}
        if use_temperature:
            params["temperature"] = 0
        if lang:
            params["language"] = lang
        # Prompt'ning oxirgi qismi muhimroq: ko'rsatma oxirida turadi
        hint = _LANG_HINT if _USE_HINT else ""
        if prompt or hint:
            room = _PROMPT_MAX_CHARS - (len(hint) + 1 if hint else 0)
            base = (prompt or "").strip()[-room:]
            params["prompt"] = (base + "\n" + hint).strip() if hint else base
        if want_language:
            params["response_format"] = "verbose_json"
        with open(file_path, "rb") as audio_file:
            return client.audio.transcriptions.create(file=audio_file, **params)

    # Til aniqlash faqat whisper-1 va til berilmagan holatda (verbose_json
    # faqat whisper-1'da mavjud)
    # Eslatma: Whisper API "uz" tilini language parametri sifatida QABUL
    # QILMAYDI (400 unsupported_language), shuning uchun o'zbekcha nutq
    # avtomatik aniqlash bilan transkripsiya qilinadi. Whisper uni ba'zan
    # qozoqcha/qirg'izcha deb belgilaydi — matn baribir GPT orqali
    # o'zbekchaga tarjima qilinadi.
    detect = STT_MODEL == "whisper-1" and not language
    try:
        response = _call(language, detect)
    except Exception as e:
        # Баъзи моделлар temperature параметрини қабул қилмаслиги мумкин: шунда уни олиб ташлаб қайтамиз
        if "temperature" not in str(e).lower():
            raise
        logging.warning(f"STT temperature параметрисиз қайта уриниш: {e}")
        response = _call(language, detect, use_temperature=False)

    cost.record_audio(duration, STT_MODEL)

    if detect:
        detected = (getattr(response, "language", None) or "").lower()
        logging.info(f"WHISPER TIL: {detected or 'noma`lum'}")

    text = (response.text or "").strip()
    # Musiqa/shovqin ustidan Whisper ba'zan faqat emoji yoki belgilar
    # to'qiydi — harf deyarli bo'lmasa, nutq yo'q deb hisoblanadi.
    if _LANG_HINT_MARK in text.lower():
        raise UnclearAudioError("Nutq topilmadi (model ko'rsatma matnini qaytardi)")
    if sum(ch.isalpha() for ch in text) < 3:
        raise UnclearAudioError("Nutq topilmadi (faqat belgilar/emoji)")
    sentences = [x.strip().lower() for x in re.split(r"[.!?…\n]+", text) if x.strip()]
    if len(sentences) >= 4 and len(set(sentences)) / len(sentences) < 0.5:
        raise UnclearAudioError("Transkripsiya takrorlanib ketgan (Whisper gallyutsinatsiyasi)")
    # Битта жумла ичида ҳам бир бўлак ўнлаб марта такрорланса («qəqəqə…»), бу галлюцинация
    if re.search(r"(.{1,8}?)\1{9,}", text):
        raise UnclearAudioError("Transkripsiya takrorlanib ketgan (uzun takror)")
    return text
