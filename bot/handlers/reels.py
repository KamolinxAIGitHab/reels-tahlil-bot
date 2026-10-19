from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
import asyncio
import functools
from collections import deque
import html
from datetime import datetime, timedelta, timezone
import os
import re
import shutil
import subprocess
import tempfile
import logging
import time
from urllib.parse import urlparse, parse_qs
from openai import RateLimitError, AuthenticationError, BadRequestError
from bot.utils.downloader import (
    download_reels_audio, download_instagram_image, download_account_posts, download_account_via_sc, AccountFetchError,
    download_youtube_shorts, get_youtube_subtitles, VideoTooLongError, AudioExtractionFailedError,
    CookieExpiredError, PrivateContentError, VideoPostError, is_rate_limited,
    ReelTooLongError, MAX_REEL_SECONDS, pop_reel_author, NoAudioTrackError, pop_scrapecreators_used,
)
from bot.utils import stats
from bot.utils import cost as cost_utils
from bot.utils.stt import transcribe_audio, UnclearAudioError, STT_MODEL
from bot.utils.analyzer import analyze_content, analyze_image_content, analyze_caption_only, analyze_account, PROMPT_VERSION

router = Router()

# Админ Telegram ID: фақат Railway Variables'даги ADMIN_USER_ID дан олинади.
# Ўрнатилмаган ёки нотўғри бўлса — 0 (админ имкониятлари ўчирилади).
try:
    ADMIN_USER_ID = int((os.getenv("ADMIN_USER_ID", "") or "").strip() or 0)
except ValueError:
    ADMIN_USER_ID = 0
if not ADMIN_USER_ID:
    logging.warning("ADMIN_USER_ID ўрнатилмаган: админ имкониятлари ўчирилган")

# Фойдаланувчи чекловлари (харажатни назорат қилиш). Қийматлар Railway Variables орқали ўзгартирилади.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default

USER_DAILY_LIMIT = _env_int("USER_DAILY_LIMIT", 15)      # бир кунда битта фойдаланувчи учун янги таҳлиллар сони; 0 = чекловсиз
MAX_PARALLEL_JOBS = max(1, _env_int("MAX_PARALLEL_JOBS", 3))  # бир вақтда ишловчи оғир ишлар сони
_busy_users: set = set()
_job_sem = asyncio.Semaphore(MAX_PARALLEL_JOBS)

_LIMIT_TEXTS = {
    "limit": {
        "lang_kirill": "⚠️ Бугунги чегара тўлди (кунига {n} тагача янги таҳлил). Илтимос, эртага давом этинг.",
        "lang_lotin": "⚠️ Bugungi chegara to'ldi (kuniga {n} tagacha yangi tahlil). Iltimos, ertaga davom eting.",
        "lang_rus": "⚠️ Дневной лимит исчерпан (до {n} новых анализов в сутки). Пожалуйста, продолжите завтра.",
    },
    "busy": {
        "lang_kirill": "⏳ Олдинги сўровингиз ҳали ишланяпти. Тугагач, янгисини юборинг.",
        "lang_lotin": "⏳ Oldingi so'rovingiz hali ishlanyapti. Tugagach, yangisini yuboring.",
        "lang_rus": "⏳ Предыдущий запрос ещё обрабатывается. Отправьте новый, когда он завершится.",
    },
    "queue": {
        "lang_kirill": "⏳ Ботда ҳозир иш кўп. Сўровингиз навбатда, илтимос кутинг.",
        "lang_lotin": "⏳ Botda hozir ish ko'p. So'rovingiz navbatda, iltimos kuting.",
        "lang_rus": "⏳ Сейчас много запросов. Ваш запрос в очереди, пожалуйста, подождите.",
    },
}


async def _daily_limit_hit(message: Message, lang: str) -> bool:
    """Кунлик чегара тўлган бўлса, фойдаланувчига хабар юборади ва True қайтаради. Админга чегара йўқ."""
    uid = message.from_user.id
    if USER_DAILY_LIMIT <= 0 or uid == ADMIN_USER_ID:
        return False
    if stats.user_analyses_today(uid) < USER_DAILY_LIMIT:
        return False
    logging.info(f"KUNLIK CHEGARA: user_id={uid} limit={USER_DAILY_LIMIT}")
    await message.answer(_LIMIT_TEXTS["limit"].get(lang, _LIMIT_TEXTS["limit"]["lang_kirill"]).format(n=USER_DAILY_LIMIT))
    return True


def _one_job_per_user(func):
    """Оғир ишлов ҳандлерини ўраб олади: бир фойдаланувчидан бир вақтда биттадан ортиқ сўров ишламайди,
    умумий параллел ишлар MAX_PARALLEL_JOBS билан чекланади (Railway хотира ва дискини ҳимоя қилади)."""
    @functools.wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        uid = message.from_user.id
        lang = user_language.get(uid, "lang_kirill")
        if uid in _busy_users:
            await message.answer(_LIMIT_TEXTS["busy"].get(lang, _LIMIT_TEXTS["busy"]["lang_kirill"]))
            return
        _busy_users.add(uid)
        try:
            if _job_sem.locked():
                await message.answer(_LIMIT_TEXTS["queue"].get(lang, _LIMIT_TEXTS["queue"]["lang_kirill"]))
            async with _job_sem:
                return await func(message, *args, **kwargs)
        finally:
            _busy_users.discard(uid)
    return wrapper


class _PersistentLanguages(dict):
    """Foydalanuvchi tili: xotirada (tezkor) va stats.db'da (bot qayta ishga
    tushganda ham saqlanadi). Avval xotiraga, topilmasa bazaga qaraydi."""

    def get(self, key, default=None):
        value = dict.get(self, key)
        if value is None:
            value = stats.get_user_language(key)
            if value is not None:
                dict.__setitem__(self, key, value)
        return value if value is not None else default

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        stats.set_user_language(key, value)


user_language = _PersistentLanguages()

RESULT_HEADERS = {
    "lang_kirill": "Таҳлил натижаси",
    "lang_lotin": "Tahlil natijasi",
    "lang_rus": "Результат анализа",
}

# Instagram ҳаволаси очилмаганда фойдаланувчига муқобил йўл кўрсатилади:
# видеони файл сифатида юбориш (Instagram'га боғлиқ эмас)
VIDEO_FILE_HINT = {
    "lang_kirill": (
        "\n\n💡 Видеони телефонга сақлаб (ёки экранни ёзиб) шу ерга файл сифатида "
        "юборсангиз, бот уни Instagram'сиз таҳлил қилади."
    ),
    "lang_lotin": (
        "\n\n💡 Videoni telefonga saqlab (yoki ekranni yozib) shu yerga fayl sifatida "
        "yuborsangiz, bot uni Instagram'siz tahlil qiladi."
    ),
    "lang_rus": (
        "\n\n💡 Сохраните видео на телефон (или запишите экран) и отправьте сюда файлом: "
        "бот проанализирует его без Instagram."
    ),
}

# Видео файл (юклаб юборилган) чекловлари
MAX_TG_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Telegram Bot API орқали юклаш чегараси
MAX_VIDEO_FILE_SECONDS = 180  # узун видеолар харажатни оширмаслиги учун

VIDEO_FILE_MESSAGES = {
    "status": {
        "lang_kirill": "⏳ Видео файл қайта ишланяпти...",
        "lang_lotin": "⏳ Video fayl qayta ishlanmoqda...",
        "lang_rus": "⏳ Видеофайл обрабатывается...",
    },
    "too_big": {
        "lang_kirill": "⚠️ Файл ҳажми 20 МБ дан катта. Қисқароқ видео юборинг (Reels учун 90 сониягача).",
        "lang_lotin": "⚠️ Fayl hajmi 20 MB dan katta. Qisqaroq video yuboring (Reels uchun 90 soniyagacha).",
        "lang_rus": "⚠️ Размер файла больше 20 МБ. Отправьте видео покороче (для Reels до 90 секунд).",
    },
    "too_long": {
        "lang_kirill": "⚠️ Видео 3 дақиқадан узун. Қисқароқ видео юборинг.",
        "lang_lotin": "⚠️ Video 3 daqiqadan uzun. Qisqaroq video yuboring.",
        "lang_rus": "⚠️ Видео длиннее 3 минут. Отправьте видео покороче.",
    },
    "no_audio": {
        "lang_kirill": "❌ Видеода овоз топилмади ёки файлни ўқиб бўлмади. Бошқа файл юборинг.",
        "lang_lotin": "❌ Videoda ovoz topilmadi yoki faylni o'qib bo'lmadi. Boshqa fayl yuboring.",
        "lang_rus": "❌ В видео нет звука или файл не удалось прочитать. Отправьте другой файл.",
    },
    "error": {
        "lang_kirill": "❌ Хато юз берди. Қайта уриниб кўринг.",
        "lang_lotin": "❌ Xatolik yuz berdi. Qayta urinib ko'ring.",
        "lang_rus": "❌ Произошла ошибка. Попробуйте снова.",
    },
}

COOKIE_EXPIRED_USER_MESSAGES = {
    "lang_kirill": "⚠️ Техник носозлик. Тез орада тузатилади.",
    "lang_lotin": "⚠️ Texnik nosozlik. Tez orada tuzatiladi.",
    "lang_rus": "⚠️ Техническая неполадка. Скоро будет исправлено.",
}

COOKIE_EXPIRED_ADMIN_MESSAGE = (
    "⚠️ Instagram cookie эскирди! COOKIE_YANGILASH.md бўйича янгиланг."
)

# Админга бир хил огоҳлантириш шу вақт оралиғида фақат бир марта юборилади
COOKIE_ALERT_INTERVAL_SECONDS = 60 * 60
_last_cookie_alert = 0.0

# Кетма-кет бир нечта ҳар хил ҳаволада HTTP 400 чиқса, сабаб ҳавола эмас,
# бот ҳисоби ёки cookie бўлиши мумкин. Бу ҳолда админга огоҳлантириш юборилади.
HTTP400_WINDOW_SECONDS = 15 * 60
HTTP400_ACCOUNT_THRESHOLD = 3
_recent_http400: deque = deque()
HTTP400_ADMIN_MESSAGE = (
    "⚠️ Instagram охирги 15 дақиқада 3 та ва ундан кўп ҳар хил ҳаволада 400 хатоси берди. "
    "Бот ҳисобида хавфсизлик текшируви (challenge) бор ёки cookie яроқсиз бўлиши мумкин. "
    "Ҳисобни ва cookie'ни текшириб, зарур бўлса янгиланг."
)


def register_http400(url: str) -> bool:
    """HTTP 400 ҳодисасини қайд этади. Охирги 15 дақиқада камида 3 та ҳар хил
    ҳаволада 400 чиққан бўлса, True қайтаради (ҳисоб ёки cookie муаммоси)."""
    now = time.monotonic()
    m = _SHORTCODE_PATTERN.search(url or "")
    key = m.group(1) if m else (url or "")
    while _recent_http400 and now - _recent_http400[0][0] > HTTP400_WINDOW_SECONDS:
        _recent_http400.popleft()
    _recent_http400.append((now, key))
    return len({k for _, k in _recent_http400}) >= HTTP400_ACCOUNT_THRESHOLD


_LABEL_RE = re.compile(r"^((?:✅|⚠️|⚠|❌|💡|🎯|🚀|📝|🔍)\ufe0f?[ \t]*[^:\n<>]{2,60}:)", re.MULTILINE)


_TRANSCRIPT_RE = re.compile(r"^📝[^\n]*:[ \t]*$", re.MULTILINE)
_ANALYSIS_MARK_RE = re.compile(r"^🔍[ \t]*(?:Таҳлил|Tahlil|Анализ):[ \t]*\n?", re.MULTILINE)
_ORIGINAL_CAPTION_LABELS = {
    "lang_kirill": "Изоҳнинг асл нусхаси",
    "lang_lotin": "Izohning asl nusxasi",
    "lang_rus": "Оригинал подписи",
}
_QUOTE_OPEN = "<blockquote expandable>"
_QUOTE_CLOSE = "</blockquote>"


def _rich(analysis: str, original_caption: str = "", lang: str = "lang_kirill") -> str:
    """Tahlil matnini Telegram HTML uchun tayyorlaydi: matn escape qilinadi, bo'lim
    sarlavhalari (✅/⚠️/❌/💡/🎯/🚀 ... :) qalin qilinadi. Model javobida transkripsiya
    tahlildan oldin keladi; bu yerda tahlil tepaga chiqariladi, transkripsiya esa pastda
    yig'iladigan blokka (expandable blockquote) solinadi. Bo'limlar topilmasa — oddiy ko'rinish."""
    t = _TRANSCRIPT_RE.search(analysis)
    m = _ANALYSIS_MARK_RE.search(analysis, t.end()) if t else None
    if not (t and m):
        return _LABEL_RE.sub(r"<b>\1</b>", html.escape(analysis))

    head = analysis[:t.start()]
    transcript_title = analysis[t.start():t.end()].strip()
    transcript_body = analysis[t.end():m.start()].strip()
    body = analysis[m.end():]
    if not transcript_body or not body.strip():
        return _LABEL_RE.sub(r"<b>\1</b>", html.escape(analysis))

    # Izoh (caption) tarjima qilinadi; asl nusxasi yig'iladigan blok oxirida qoladi
    # (tarjimada xato bo'lsa tekshirib ko'rish uchun). Tarjima aynan asl bilan bir xil bo'lsa — qo'shilmaydi.
    original = (original_caption or "").strip()
    if original and original not in transcript_body:
        label = _ORIGINAL_CAPTION_LABELS.get(lang, _ORIGINAL_CAPTION_LABELS["lang_kirill"])
        transcript_body += f"\n\n— {label} —\n{original}"

    out = _LABEL_RE.sub(r"<b>\1</b>", html.escape(head + body.lstrip("\n"))).rstrip()
    out += f"\n\n<b>{html.escape(transcript_title)}</b>\n{_QUOTE_OPEN}{html.escape(transcript_body)}{_QUOTE_CLOSE}"
    return out


def _split_message(text: str, limit: int = 4000) -> list:
    """Uzun matnni qator chegarasidan bo'lib, so'z/qator o'rtasida uzilmasligini ta'minlaydi.
    Yig'iladigan blok (blockquote) bo'laklar orasida bo'linsa, har bir bo'lakda tegi yopiladi
    va keyingisida qayta ochiladi (aks holda Telegram HTML'ni o'qiy olmaydi)."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n ")
    parts.append(text)

    reopen = False
    for i, part in enumerate(parts):
        if reopen:
            part = _QUOTE_OPEN + part
        reopen = part.count(_QUOTE_OPEN) > part.count(_QUOTE_CLOSE)
        if reopen:
            part += _QUOTE_CLOSE
        parts[i] = part
    return parts


_SOURCE_TEXTS = {
    "lang_kirill": ("Муаллиф", "Муаллиф ёзган ҳаволалар (текширилмаган, босиб нусха олинг)"),
    "lang_lotin": ("Muallif", "Muallif yozgan havolalar (tekshirilmagan, bosib nusxa oling)"),
    "lang_rus": ("Автор", "Ссылки от автора (не проверены, нажмите, чтобы скопировать)"),
}
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'()]+", re.IGNORECASE)
MAX_CAPTION_LINKS = 5


def author_get(url: str):
    """Keshdan muallif: (nom, havola) yoki None."""
    key = _cache_key(url, "")
    if not key:
        return None
    row = stats.rc_get("author", key[0], "", "1", CACHE_TTL_DAYS)
    if not row or "\n" not in row[0]:
        return None
    display, link = row[0].split("\n", 1)
    return display, link


def author_put(url: str, author) -> None:
    key = _cache_key(url, "")
    if key and author:
        stats.rc_put("author", key[0], "", "1", f"{author[0]}\n{author[1]}", "", CACHE_DB_MAX_ROWS)


def _caption_links(caption: str, own_url: str = "") -> list:
    """Izohdagi (caption) tashqi havolalar: takrorlarsiz, eng ko'pi MAX_CAPTION_LINKS ta.
    Reels'ning o'z havolasi tashlab ketiladi."""
    own = _cache_key(own_url, "")
    found = []
    for raw in _URL_IN_TEXT_RE.findall(caption or ""):
        link = raw.rstrip(".,;:!?»”…")
        if not link or len(link) > 200 or link in found:
            continue
        m = _SHORTCODE_PATTERN.search(link)
        if m and own and m.group(1) == own[0]:
            continue
        found.append(link)
        if len(found) >= MAX_CAPTION_LINKS:
            break
    return found


def _author_line(lang: str, author) -> str:
    """Natija tepasidagi muallif qatori (Telegram HTML)."""
    if not author:
        return ""
    label = _SOURCE_TEXTS.get(lang, _SOURCE_TEXTS["lang_kirill"])[0]
    return f'👤 {label}: <a href="{html.escape(author[1], quote=True)}">{html.escape(author[0])}</a>\n\n'


def _links_block(lang: str, caption: str, own_url: str = "") -> str:
    """Natija pastidagi izoh havolalari bloki. Havolalar <code> ichida: bosilmaydi (faqat nusxa olinadi)."""
    links = _caption_links(caption, own_url)
    if not links:
        return ""
    title = _SOURCE_TEXTS.get(lang, _SOURCE_TEXTS["lang_kirill"])[1]
    return f"\n\n🔗 <b>{html.escape(title)}</b>\n" + "\n".join(f"<code>{html.escape(u)}</code>" for u in links)


async def _send_long(status_msg, message, text: str, parse_mode=None) -> None:
    """Natijani yuboradi: birinchi bo'lak status xabarini almashtiradi, qolganlari alohida xabarlar
    bo'lib ketma-ket yuboriladi (har biri Telegram chegarasidan oshmaydi)."""
    parts = _split_message(text)
    await status_msg.edit_text(parts[0], parse_mode=parse_mode)
    for part in parts[1:]:
        await message.answer(part, parse_mode=parse_mode)


def _head_tail(text: str, limit: int = 4000):
    parts = _split_message(text, limit)
    return parts[0], "\n".join(parts[1:])


async def notify_cookie_expired(
    message: Message, lang: str, source_type: str, error: Exception | None = None,
    admin_text: str | None = None,
) -> None:
    """Instagram cookie eskirganda statistikaga 'cookie_expired' yozadi va
    adminga ogohlantirish yuboradi. Ogohlantirish soatiga bir martadan
    ko'p yuborilmaydi (foydalanuvchi xabari esa har safar alohida)."""
    global _last_cookie_alert
    logging.error(f"Instagram cookie eskirgan ({source_type}): user_id={message.from_user.id}")
    stats.log_analysis(message.from_user.id, source_type, lang, "error", "cookie_expired")

    now = time.monotonic()
    if _last_cookie_alert and now - _last_cookie_alert < COOKIE_ALERT_INTERVAL_SECONDS:
        return
    _last_cookie_alert = now

    cause = getattr(error, "__cause__", None) or error
    text = admin_text or COOKIE_EXPIRED_ADMIN_MESSAGE
    if cause:
        text += f"\n\nСабаб: {str(cause)[:300]}"
    try:
        await message.bot.send_message(ADMIN_USER_ID, text)
    except Exception as e:
        logging.error(f"Adminga cookie ogohlantirishini yuborib bo'lmadi: {e}")


# Bir xil Reels/post uchun natija qayta Instagram va OpenAI'ga so'rov yubormasdan
# beriladi. Kesh ikki qavatli: tezkor xotira (24 soat) va doimiy baza (stats.db, standart 30 kun,
# bot qayta ishga tushsa ham saqlanadi). Natija kaliti PROMPT_VERSION'ga, transkripsiya
# kaliti STT_MODEL'ga bog'langan: ular o'zgarsa, eski yozuv ishlatilmaydi.
RESULT_CACHE_TTL_SECONDS = 24 * 60 * 60
RESULT_CACHE_MAX_ITEMS = 300
CACHE_TTL_DAYS = cost_utils.env_float("CACHE_TTL_DAYS", 30.0)
CACHE_DB_MAX_ROWS = 5000
_SHORTCODE_PATTERN = re.compile(r"instagram\.com/(?:reels?|p|tv)/([A-Za-z0-9_\-]+)", re.IGNORECASE)
_result_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _cache_key(url: str, lang: str) -> tuple[str, str] | None:
    match = _SHORTCODE_PATTERN.search(url or "")
    return (match.group(1), lang) if match else None


def cache_get(url: str, lang: str) -> str | None:
    key = _cache_key(url, lang)
    item = _result_cache.get(key) if key else None
    if item:
        saved_at, text = item
        if time.monotonic() - saved_at <= RESULT_CACHE_TTL_SECONDS:
            return text
        _result_cache.pop(key, None)
    if not key:
        return None
    row = stats.rc_get("result", key[0], lang, PROMPT_VERSION, CACHE_TTL_DAYS)
    if not row:
        return None
    _result_cache[key] = (time.monotonic(), row[0])
    return row[0]


def cache_put(url: str, lang: str, text: str) -> None:
    key = _cache_key(url, lang)
    if not key:
        return
    if len(_result_cache) >= RESULT_CACHE_MAX_ITEMS:
        oldest = min(_result_cache, key=lambda k: _result_cache[k][0])
        _result_cache.pop(oldest, None)
    _result_cache[key] = (time.monotonic(), text)
    stats.rc_put("result", key[0], lang, PROMPT_VERSION, text, "", CACHE_DB_MAX_ROWS)


# Audio transkripsiyasi tildan mustaqil saqlanadi: bir xil Reels turli tilda
# so'ralsa, Instagram va OpenAI STT'ga qayta murojaat qilinmaydi va barcha
# tilda bir xil matn ishlatiladi (STT natijasi har chaqiruvda biroz farq qiladi).
_transcript_cache: dict[str, tuple[float, str, str]] = {}


def transcript_get(url: str):
    key = _cache_key(url, "")
    item = _transcript_cache.get(key[0]) if key else None
    if item:
        saved_at, text, caption = item
        if time.monotonic() - saved_at <= RESULT_CACHE_TTL_SECONDS:
            return text, caption
        _transcript_cache.pop(key[0], None)
    if not key:
        return None
    row = stats.rc_get("transcript", key[0], "", STT_MODEL, CACHE_TTL_DAYS)
    if not row:
        return None
    _transcript_cache[key[0]] = (time.monotonic(), row[0], row[1])
    return row[0], row[1]


def transcript_put(url: str, text: str, caption) -> None:
    key = _cache_key(url, "")
    if not key:
        return
    if len(_transcript_cache) >= RESULT_CACHE_MAX_ITEMS:
        oldest = min(_transcript_cache, key=lambda k: _transcript_cache[k][0])
        _transcript_cache.pop(oldest, None)
    _transcript_cache[key[0]] = (time.monotonic(), text, caption or "")
    stats.rc_put("transcript", key[0], "", STT_MODEL, text, caption or "", CACHE_DB_MAX_ROWS)

OPENAI_CREDIT_MESSAGES = {
    "lang_kirill": (
        "⚠️ Хизмат вақтинчалик тўхтатилди.\n"
        "OpenAI кредити тугади. Тез орада тикланади.\n"
        "Илтимос, 10-15 дақиқадан сўнг уриниб кўринг."
    ),
    "lang_lotin": (
        "⚠️ Xizmat vaqtincha to'xtatildi.\n"
        "OpenAI krediti tugadi. Tez orada tiklanadi.\n"
        "Iltimos, 10-15 daqiqadan so'ng urinib ko'ring."
    ),
    "lang_rus": (
        "⚠️ Сервис временно недоступен.\n"
        "Кредит OpenAI исчерпан. Скоро будет восстановлен.\n"
        "Пожалуйста, попробуйте через 10-15 минут."
    ),
}

OPENAI_AUTH_MESSAGES = {
    "lang_kirill": "❌ API калит муаммоси. Илтимос, администратор билан боғланинг.",
    "lang_lotin": "❌ API kalit muammosi. Iltimos, administrator bilan bog'laning.",
    "lang_rus": "❌ Проблема с API ключом. Пожалуйста, свяжитесь с администратором.",
}

SHORT_VOICE_SECONDS = 5
SHORT_VOICE_NOTES = {
    "lang_kirill": "⚠️ Овоз жуда қисқа (5 сониядан кам), натижа аниқ бўлмаслиги мумкин.",
    "lang_lotin": "⚠️ Ovoz juda qisqa (5 soniyadan kam), natija aniq bo'lmasligi mumkin.",
    "lang_rus": "⚠️ Голосовое сообщение слишком короткое (менее 5 секунд), результат может быть неточным.",
}

MODERATION_MESSAGES = {
    "lang_kirill": "⚠️ Ушбу контент таҳлил қилинмади. Мазмун хизмат шартларига мос келмайди.",
    "lang_lotin": "⚠️ Ushbu kontent tahlil qilinmadi. Mazmun xizmat shartlariga mos kelmaydi.",
    "lang_rus": (
        "⚠️ Контент не был проанализирован. Содержание не "
        "соответствует условиям сервиса."
    ),
}

PRIVATE_CONTENT_MESSAGES = {
    "lang_kirill": (
        "🔒 Бу контент хусусий — кириш чекланган.\n"
        "Фақат оммавий контентни таҳлил қила оламиз."
    ),
    "lang_lotin": (
        "🔒 Bu kontent xususiy — kirish cheklangan.\n"
        "Faqat ommaviy kontentni tahlil qila olamiz."
    ),
    "lang_rus": (
        "🔒 Этот контент приватный — доступ ограничен.\n"
        "Мы можем анализировать только публичный контент."
    ),
}

YOUTUBE_STATUS_MESSAGES = {
    "lang_kirill": "🎬 YouTube Shorts таҳлил қилиняпти...",
    "lang_lotin": "🎬 YouTube Shorts tahlil qilinmoqda...",
    "lang_rus": "🎬 YouTube Shorts анализируется...",
}

YOUTUBE_DISABLED_MESSAGES = {
    "lang_kirill": "🔴 YouTube Shorts таҳлили вақтинча ишламайди. Тез орада тикланади.",
    "lang_lotin": "🔴 YouTube Shorts tahlili vaqtincha ishlamaydi. Tez orada tiklanadi.",
    "lang_rus": "🔴 Анализ YouTube Shorts временно недоступен. Скоро восстановим.",
}

YOUTUBE_TOO_LONG_MESSAGES = {
    "lang_kirill": (
        "⚠️ Фақат YouTube Shorts (60 сек) қабул қилинади. "
        "Узун видеолар таҳлил қилинмайди."
    ),
    "lang_lotin": "⚠️ Faqat YouTube Shorts (60 sek) qabul qilinadi.",
    "lang_rus": "⚠️ Принимаются только YouTube Shorts (до 60 сек).",
}

_REFUSAL_PATTERNS = (
    "i can't assist", "i cannot assist", "i can't help", "i cannot help",
    "i'm sorry, but i can't", "i'm sorry, i can't", "i am unable to assist",
    "i am unable to help", "i won't be able to help", "i'm not able to help",
    "against our content policy", "against openai's usage policies",
    "violates our usage policies", "i can't provide", "i cannot provide",
    # Ruscha rad javoblar
    "не могу помочь", "не могу выполнить", "не могу проанализировать",
    "не могу анализировать", "не могу предоставить",
    # O'zbekcha (kirill) rad javoblar
    "ёрдам бера олмайман", "таҳлил қила олмайман", "бажара олмайман",
    "ўзбек тилида эмас",
    # O'zbekcha (lotin) rad javoblar
    "yordam bera olmayman", "tahlil qila olmayman", "bajara olmayman",
    "o'zbek tilida emas",
)


def is_moderation_refusal(text: str | None) -> bool:
    """GPT-4o javobi moderatsiya sababli rad etish (refusal) matni
    ekanligini aniqlaydi — model so'rovni bajarishdan bosh tortganda
    odatda ingliz tilida qisqa 'can't assist' uslubidagi javob beradi,
    tizim promptidagi til talabidan qat'i nazar."""
    if not text:
        return False
    lowered = text.lower()
    if any(p in lowered for p in _REFUSAL_PATTERNS):
        return True
    # Oddiy tahlilda doim "💡" (amaliy qiymat) qatori bo'ladi;
    # u yo'q va javob qisqa bo'lsa — bu rad javobi.
    return "💡" not in text and len(text.strip()) < 300


async def handle_openai_error(
    status_msg, lang: str, e: Exception, context: str,
    user_id: int | None = None, source_type: str | None = None,
) -> bool:
    """OpenAI'ning kredit tugashi (RateLimitError, shu jumladan
    insufficient_quota/429), autentifikatsiya (AuthenticationError) va
    moderatsiya (BadRequestError/content_filter) xatolarini 3 tilda
    foydalanuvchiga ko'rsatadi va statistika bazasiga yozadi. Xato
    OpenAI'ga tegishli bo'lmasa False qaytaradi — chaqiruvchi umumiy
    xato ishlovini davom ettiradi."""
    if isinstance(e, AuthenticationError):
        logging.error(f"OpenAI autentifikatsiya xatosi ({context}): {e}")
        if user_id is not None:
            stats.log_analysis(user_id, source_type, lang, "error", "openai_auth", str(e))
        await status_msg.edit_text(OPENAI_AUTH_MESSAGES.get(lang, OPENAI_AUTH_MESSAGES["lang_kirill"]))
        return True
    if isinstance(e, RateLimitError):
        logging.error(f"OpenAI kredit/rate-limit xatosi ({context}): {e}")
        if user_id is not None:
            stats.log_analysis(user_id, source_type, lang, "error", "openai_credit", str(e))
        await status_msg.edit_text(OPENAI_CREDIT_MESSAGES.get(lang, OPENAI_CREDIT_MESSAGES["lang_kirill"]))
        return True
    if isinstance(e, BadRequestError):
        message = str(e).lower()
        code = str(getattr(e, "code", "") or "").lower()
        if "content_filter" in message or "content_filter" in code or "moderation" in message:
            logging.error(f"OpenAI moderatsiya xatosi ({context}): {e}")
            if user_id is not None:
                stats.log_analysis(user_id, source_type, lang, "error", "moderation", str(e))
            await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
            return True
    return False

REEL_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/reels?/[A-Za-z0-9_\-]+/?(?:\?[A-Za-z0-9=&%_\-]*)?",
    re.IGNORECASE,
)
ALLOWED_HOSTS = {"instagram.com", "www.instagram.com"}


def extract_reel_url(text: str | None) -> str | None:
    """Xabardan faqat Instagram Reel URL qismini ajratib oladi va
    host'ini qat'iy tekshiradi. Yaroqsiz bo'lsa None qaytaradi."""
    match = REEL_URL_PATTERN.search(text or "")
    if not match:
        return None

    candidate = match.group(0)
    if not candidate.lower().startswith(("http://", "https://")):
        candidate = "https://" + candidate

    hostname = (urlparse(candidate).hostname or "").lower()
    if hostname not in ALLOWED_HOSTS:
        return None

    return candidate

def extract_post_url(text: str) -> str | None:
    """Xabardan Instagram post (/p/...) URL'ini ajratib oladi."""
    pattern = r'https?://(?:www\.)?instagram\.com/p/[A-Za-z0-9_-]+/?'
    match = re.search(pattern, text)
    return match.group(0) if match else None

def extract_account_url(text: str) -> str | None:
    """Xabardan Instagram profil URL'ini ajratib, username'ni qaytaradi.
    /p/, /reel/ kabi maxsus yo'llarni username sifatida qabul qilmaydi."""
    pattern = r'https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?(?:\?.*)?$'
    match = re.search(pattern, text)
    if match:
        username = match.group(1)
        if username not in ('p', 'reel', 'reels', 'stories', 'explore', 'tv'):
            return username
    return None

YOUTUBE_URL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.\-])(?:https?://)?(?:www\.|m\.)?"
    r"(?:youtube\.com/shorts/[A-Za-z0-9_\-]+"
    r"|youtube\.com/watch\?v=[A-Za-z0-9_\-]+"
    r"|youtu\.be/[A-Za-z0-9_\-]+)"
    r"(?:[?&][^\s<>\"']*)?",
    re.IGNORECASE,
)
YOUTUBE_ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
_URL_TRAILING_PUNCT = ".,;:!?)]}'\""


def extract_youtube_url(text: str | None) -> str | None:
    """Xabardan YouTube (Shorts, youtu.be yoki oddiy watch) URL'ini
    ajratib oladi va host'ini qat'iy tekshiradi. Query-qism (masalan
    ?si=... ulashish tokeni) istalgan belgidan iborat bo'lishi mumkin —
    faqat probel/qavs/qoshtirnoqda to'xtaydi. Gap oxiridagi tinish
    belgilari (nuqta, vergul va hokazo) kesib tashlanadi. Uzun/qisqa
    ekanligi bu yerda emas, yuklab olishdan oldin davomiylik bo'yicha
    tekshiriladi (download_youtube_shorts)."""
    match = YOUTUBE_URL_PATTERN.search(text or "")
    if not match:
        return None

    candidate = match.group(0).rstrip(_URL_TRAILING_PUNCT)
    if not candidate.lower().startswith(("http://", "https://")):
        candidate = "https://" + candidate

    hostname = (urlparse(candidate).hostname or "").lower()
    if hostname not in YOUTUBE_ALLOWED_HOSTS:
        return None

    return candidate


def is_youtube_shorts_path(url: str) -> bool:
    """URL youtube.com/shorts/... shaklidami, shuni tekshiradi (faqat
    log/status xabari uchun — qabul qilish shartiga ta'sir qilmaydi)."""
    return "/shorts/" in urlparse(url).path.lower()


def extract_youtube_video_id(url: str) -> str | None:
    """URL'dan YouTube video ID'sini ajratib oladi — shorts/XXXXX,
    watch?v=XXXXX yoki youtu.be/XXXXX shakllarining barchasini qo'llab-
    quvvatlaydi. Ajratib bo'lmasa None qaytaradi."""
    parsed = urlparse(url)
    path = parsed.path

    if "/shorts/" in path:
        return path.split("/shorts/")[1].split("/")[0] or None

    if (parsed.hostname or "").lower() == "youtu.be":
        return path.lstrip("/").split("/")[0] or None

    if path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        return video_id or None

    return None


NO_SUBTITLE_MESSAGES = {
    "lang_kirill": (
        "Бу видеода субтитр мавжуд эмас. Видеони юклаб, "
        "овозли хабар сифатида юборинг — таҳлил қиламиз."
    ),
    "lang_lotin": (
        "Bu videoda subtitr yo'q. Videoni yuklab, "
        "ovozli xabar sifatida yuboring."
    ),
    "lang_rus": (
        "У этого видео нет субтитров. Скачайте видео и "
        "отправьте голосовым сообщением."
    ),
}

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Assalomu alaykum! / Салом! / Привет!\n\n"
        "🤖 <b>ReelsTahlil Bot</b>\n\n"
        "📌 Tahlil qila olaman / Таҳлил қила оламан / Могу анализировать:\n"
        "🎬 Instagram Reels (video)\n"
        "🖼 Instagram rasm / расм постлари\n"
        "🎠 Instagram karusel / карусель постлари\n"
        "📝 Instagram matn / матн постлари\n"
        "🎙 Telegram ovozli xabar / овозли хабар\n"
        "🎞 Video fayl / Видео файл (Reels ochilmasa / если Reels не открывается)\n\n"
        "🌐 Tilni tanlang / Тилни танланг / Выберите язык:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇿 Ўзбекча (Кирилл)", callback_data="lang_kirill"),
                InlineKeyboardButton(text="🇺🇿 O'zbekcha (Lotin)", callback_data="lang_lotin"),
            ],
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_rus"),
            ]
        ])
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")
    if lang == "lang_rus":
        text = (
            "📖 <b>Как пользоваться ботом:</b>\n\n"
            "1️⃣ Откройте Instagram\n"
            "2️⃣ Найдите Reels или пост\n"
            "3️⃣ Нажмите 'Поделиться' → 'Скопировать ссылку'\n"
            "4️⃣ Отправьте ссылку сюда\n\n"
            "✅ Бот автоматически:\n"
            "• Скачает видео/фото\n"
            "• Транскрибирует аудио\n"
            "• Проверит факты\n"
            "• Даст оценку контента\n"
            "• Отправьте голосовое — бот проанализирует\n"
            "• Reels не открывается? Отправьте видео или файл (до 20 МБ, до 3 минут)\n"
            "• 🔴 Анализ аккаунтов временно недоступен\n"
            "• 🔴 YouTube Shorts — временно не работает\n\n"
            "⚙️ Команды:\n"
            "/start — начать заново\n"
            "/help — эта справка"
        )
    elif lang == "lang_lotin":
        text = (
            "📖 <b>Botdan qanday foydalanish:</b>\n\n"
            "1️⃣ Instagramni oching\n"
            "2️⃣ Reels yoki post toping\n"
            "3️⃣ 'Ulashish' → 'Havolani nusxalash' bosing\n"
            "4️⃣ Havolani shu yerga yuboring\n\n"
            "✅ Bot avtomatik:\n"
            "• Video/rasm yuklab oladi\n"
            "• Audioni matnga o'giradi\n"
            "• Faktlarni tekshiradi\n"
            "• Kontent bahosini beradi\n"
            "• Ovozli xabar yuboring — bot tahlil qiladi\n"
            "• Reels ochilmasa, video yoki fayl yuboring (20 MB gacha, 3 daqiqagacha)\n"
            "• 🔴 Akkount tahlili vaqtincha ishlamaydi\n"
            "• 🔴 YouTube Shorts — vaqtincha ishlamaydi\n\n"
            "⚙️ Buyruqlar:\n"
            "/start — qaytadan boshlash\n"
            "/help — ushbu yordam"
        )
    else:
        text = (
            "📖 <b>Ботдан қандай фойдаланиш:</b>\n\n"
            "1️⃣ Instagramni очинг\n"
            "2️⃣ Reels ёки постни топинг\n"
            "3️⃣ 'Улашиш' → 'Ҳаволани нусхалаш' босинг\n"
            "4️⃣ Ҳаволани шу ерга юборинг\n\n"
            "✅ Бот автоматик:\n"
            "• Видео/расм юклаб олади\n"
            "• Аудиони матнга ўгиради\n"
            "• Фактларни текширади\n"
            "• Контент баҳосини беради\n"
            "• Овозли хабар юборинг — бот таҳлил қилади\n"
            "• Reels очилмаса, видео ёки файл юборинг (20 МБ гача, 3 дақиқагача)\n"
            "• 🔴 Аккаунт таҳлили вақтинча ишламайди\n"
            "• 🔴 YouTube Shorts — вақтинча ишламайди\n\n"
            "⚙️ Буйруқлар:\n"
            "/start — қайтадан бошлаш\n"
            "/help — ушбу ёрдам"
        )
    await message.answer(text, parse_mode="HTML")

_COST_LABELS = {
    "stt": "Овоз → матн (STT)",
    "analyze_content": "Матн таҳлили",
    "try_block": "«Синаб кўриш» блоки",
    "value_fix": "Амалий қиймат қайта текшируви",
    "audio_garbled": "Овоз сифати текшируви",
    "analyze_image": "Расм таҳлили",
    "analyze_caption": "Изоҳ таҳлили",
    "analyze_account": "Аккаунт таҳлили",
}


@router.message(Command("xarajat"))
async def cmd_xarajat(message: Message):
    """OpenAI xarajati hisoboti. Faqat admin uchun; boshqalarga javob berilmaydi."""
    if message.from_user.id != ADMIN_USER_ID:
        return
    c = stats.get_cost_summary()

    def usd(v: float) -> str:
        if v >= 0.1:
            return f"${v:.2f}"
        return f"${v:.3f}" if v >= 0.01 else f"${v:.4f}"

    lines = [
        "💰 OpenAI харажати (тахминий):",
        f"📅 Бугун: {usd(c['today_usd'])} ({c['today_calls']} та чақирув)",
        f"📅 Бу ҳафта: {usd(c['week_usd'])}",
        f"📅 Бу ой: {usd(c['month_usd'])} ({c['month_calls']} та чақирув)",
        f"📅 Ҳисоб бошланишидан бери: {usd(c['all_usd'])}",
    ]
    if c["avg_per_request"] is not None:
        lines.append(f"📊 Битта сўровга ўртача (бу ой): {usd(c['avg_per_request'])} ({c['month_requests']} та муваффақиятли)")
    if c["forecast_month_usd"] is not None:
        lines.append(f"📈 Ой охиригача тахмин: {usd(c['forecast_month_usd'])}")
    else:
        lines.append("📈 Ой охиригача тахмин: камида 1 кунлик маълумот йиғилгач кўрсатилади")
    limit_parts = []
    if cost_utils.LIMIT_DAY > 0:
        limit_parts.append(f"кун {usd(cost_utils.LIMIT_DAY)} (ишлатилган {c['today_usd'] / cost_utils.LIMIT_DAY * 100:.0f}%)")
    if cost_utils.LIMIT_MONTH > 0:
        limit_parts.append(f"ой {usd(cost_utils.LIMIT_MONTH)} (ишлатилган {c['month_usd'] / cost_utils.LIMIT_MONTH * 100:.0f}%)")
    if limit_parts:
        lines.append("🎯 Огоҳлантириш лимити: " + ", ".join(limit_parts))
    if c["month_cache_hits"]:
        saved = ""
        if c["avg_per_request"] is not None:
            saved = f", тахминий тежам ≈ {usd(c['month_cache_hits'] * c['avg_per_request'])}"
        lines.append(f"♻️ Кэшдан берилган такрорий сўровлар (бу ой): {c['month_cache_hits']} та{saved}")
    alerts = stats.recent_alerts(4)
    if alerts:
        from datetime import datetime, timedelta
        lines.append("🔔 Охирги огоҳлантиришлар:")
        for akey, asent, acreated in alerts:
            kind, period, pct = akey.split(":")[0], akey.split(":")[1], akey.split(":")[-1]
            try:
                when = (datetime.fromisoformat(acreated) + timedelta(hours=5)).strftime("%d.%m %H:%M")
            except ValueError:
                when = acreated[:16]
            state = {0: "навбатда ⏳", 1: "юборилган ✅", 2: "жим ёзилган (юборилмаган)"}.get(asent, str(asent))
            lines.append(f"  • {'кун' if kind == 'day' else 'ой'} {pct}% ({period}): {state}, {when}")
    if c["by_label"]:
        lines.append("\n🔎 Бу ой, тури бўйича:")
        for label, calls, total in c["by_label"]:
            lines.append(f"  • {_COST_LABELS.get(label, label)}: {usd(total)} ({calls} та)")
    if c["tracking_since"]:
        lines.append(f"\n🗓 Ҳисоб бошланган сана (UTC): {c['tracking_since'][:10]}")
    lines.append(
        "\n⚠️ Нархлар тахминий: расмий рақамни platform.openai.com → Usage саҳифасидан текширинг. "
        "Нарх ёки коэффициентни Railway Variables орқали ўзгартириш мумкин."
    )
    await message.answer("\n".join(lines))


_STATS_TEXTS = {
    "lang_kirill": {
        "title_user": "📊 Сизнинг статистикангиз", "title_admin": "📊 Умумий статистика (админ)",
        "today": "Бугун", "week": "Ҳафта", "month": "Ой", "total": "Жами",
        "rate": "Бугунги муваффақият", "types": "📌 Таҳлил турлари", "langs": "🌐 Тиллар",
        "hourly": "🕒 Бугунги фаоллик", "tech": "⚙️ Админ (техник)", "empty": "Ҳали сўров йўқ.",
        "src": {"instagram_reels": "🎬 Reels", "instagram_post": "📸 Пост", "instagram_account": "👤 Аккаунт",
                "youtube_shorts": "🎥 Shorts", "voice": "🎤 Овоз", "video_file": "🎞 Видео файл"},
        "lng": {"lang_kirill": "🇺🇿 Кирилл", "lang_lotin": "🔤 Лотин", "lang_rus": "🇷🇺 Рус"},
        "err": {"instagram_limit": "Instagram чеклов", "cookie_expired": "Instagram cookie", "bad_link": "Ҳавола очилмади",
                "openai_credit": "OpenAI кредит", "moderation": "Модерация", "boshqa": "Бошқа"},
        "fallback": "Захира режим (gpt-4o-mini)", "cookie": "Cookie билан юклаш", "sc": "Захира йўл (ScrapeCreators)",
    },
    "lang_lotin": {
        "title_user": "📊 Sizning statistikangiz", "title_admin": "📊 Umumiy statistika (admin)",
        "today": "Bugun", "week": "Hafta", "month": "Oy", "total": "Jami",
        "rate": "Bugungi muvaffaqiyat", "types": "📌 Tahlil turlari", "langs": "🌐 Tillar",
        "hourly": "🕒 Bugungi faollik", "tech": "⚙️ Admin (texnik)", "empty": "Hali so'rov yo'q.",
        "src": {"instagram_reels": "🎬 Reels", "instagram_post": "📸 Post", "instagram_account": "👤 Akkaunt",
                "youtube_shorts": "🎥 Shorts", "voice": "🎤 Ovoz", "video_file": "🎞 Video fayl"},
        "lng": {"lang_kirill": "🇺🇿 Kirill", "lang_lotin": "🔤 Lotin", "lang_rus": "🇷🇺 Rus"},
        "err": {"instagram_limit": "Instagram cheklov", "cookie_expired": "Instagram cookie", "bad_link": "Havola ochilmadi",
                "openai_credit": "OpenAI kredit", "moderation": "Moderatsiya", "boshqa": "Boshqa"},
        "fallback": "Fallback (gpt-4o-mini)", "cookie": "Cookie bilan yuklash", "sc": "Zaxira yo'l (ScrapeCreators)",
    },
    "lang_rus": {
        "title_user": "📊 Ваша статистика", "title_admin": "📊 Общая статистика (админ)",
        "today": "Сегодня", "week": "Неделя", "month": "Месяц", "total": "Всего",
        "rate": "Успешность сегодня", "types": "📌 Типы анализа", "langs": "🌐 Языки",
        "hourly": "🕒 Активность сегодня", "tech": "⚙️ Админ (техническое)", "empty": "Запросов пока нет.",
        "src": {"instagram_reels": "🎬 Reels", "instagram_post": "📸 Пост", "instagram_account": "👤 Аккаунт",
                "youtube_shorts": "🎥 Shorts", "voice": "🎤 Голос", "video_file": "🎞 Видеофайл"},
        "lng": {"lang_kirill": "🇺🇿 Кириллица", "lang_lotin": "🔤 Латиница", "lang_rus": "🇷🇺 Русский"},
        "err": {"instagram_limit": "Ограничение Instagram", "cookie_expired": "Cookie Instagram", "bad_link": "Ссылка не открылась",
                "openai_credit": "Кредит OpenAI", "moderation": "Модерация", "boshqa": "Другое"},
        "fallback": "Резервный режим (gpt-4o-mini)", "cookie": "Загрузка с cookie", "sc": "Резервный путь (ScrapeCreators)",
    },
}


def _progress_bar(percent: int, width: int = 10) -> str:
    filled = max(0, min(width, round(width * percent / 100)))
    return "🟩" * filled + "⬜" * (width - filled)


def build_stats_text(s: dict, lang: str, is_admin: bool) -> str:
    """/stats matnini (Telegram HTML) tuzadi. Oddiy foydalanuvchiga faqat o'z ko'rsatkichlari,
    texnik bo'lim faqat adminga."""
    t = _STATS_TEXTS.get(lang, _STATS_TEXTS["lang_kirill"])
    lines = [f"<b>{t['title_admin'] if is_admin else t['title_user']}</b>", ""]

    if not s["all_total"]:
        lines.append(t["empty"])
        return "\n".join(lines)

    today = f"{s['today_total']}"
    if s["today_total"]:
        today += f"  ✅{s['today_success']}"
        if s["today_error"]:
            today += f" ❌{s['today_error']}"
    rows = [
        (t["today"], today), (t["week"], str(s["week_total"])),
        (t["month"], str(s["month_total"])), (t["total"], str(s["all_total"])),
    ]
    lines.append("<pre>" + "\n".join(f"{name:<9}{val}" for name, val in rows) + "</pre>")

    if s["today_total"]:
        pct = round(100 * s["today_success"] / s["today_total"])
        lines.append(f"{t['rate']}: {_progress_bar(pct)} {pct}%")

    src = " · ".join(f"{t['src'][k]} {v}" for k, v in s["by_source"].items() if v and k in t["src"])
    if src:
        lines += ["", t["types"], src]
    lng = " · ".join(f"{t['lng'][k]} {v}" for k, v in s["by_lang"].items() if v and k in t["lng"])
    if lng:
        lines += ["", t["langs"], lng]

    if s["hourly"]:
        peak = max(s["hourly"].values())
        bars = "\n".join(
            f"{h}:00 {'█' * max(1, round(10 * c / peak))} {c}" for h, c in sorted(s["hourly"].items())
        )
        lines += ["", t["hourly"], f"<pre>{bars}</pre>"]

    if is_admin:
        et = s["today_error_types"]
        tech = [f"{t['err'][k]}: {v}" for k, v in et.items() if v and k in t["err"]]
        tech.append(f"{t['fallback']}: {s['fallback_used_count']}")
        tech.append(f"{t['cookie']}: {s['cookie_fallback_count']}")
        tech.append(f"{t['sc']}: {s['scrapecreators_count']}")
        lines += ["", t["tech"], "\n".join(tech)]

    return "\n".join(lines)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")
    # Admin — umumiy statistika; boshqa foydalanuvchi faqat o'zinikini ko'radi.
    is_admin = message.from_user.id == ADMIN_USER_ID
    s = stats.get_stats(None if is_admin else message.from_user.id)
    await message.answer(build_stats_text(s, lang, is_admin), parse_mode="HTML")

_ERR_URL_RE = re.compile(r"https?://\S+")
_ERR_LONGNUM_RE = re.compile(r"\d{6,}")


def build_errors_text(rows: list) -> str:
    """Охирги хатолар матни (админ учун). Ҳаволалар ва узун рақамлар яширилади."""
    if not rows:
        return "Хатолар топилмади."
    out = ["<b>Охирги хатолар</b> (Тошкент вақти)", ""]
    for r in rows:
        ts = r.get("timestamp") or ""
        try:
            dt = datetime.fromisoformat(ts).astimezone(timezone(timedelta(hours=5)))
            when = dt.strftime("%d.%m %H:%M")
        except Exception:
            when = ts[:16]
        detail = (r.get("error_detail") or "").replace("\n", " ")
        detail = _ERR_URL_RE.sub("[ҳавола]", detail)
        detail = _ERR_LONGNUM_RE.sub("…", detail)[:120]
        line = f"{when} · {r.get('source_type') or '-'} · {r.get('error_type') or '-'}"
        if detail:
            line += f" · {detail}"
        out.append(html.escape(line))
    return "\n".join(out)


@router.message(Command("xatolar"))
async def cmd_xatolar(message: Message):
    # Фақат админ учун; бошқа фойдалувчига жавоб берилмайди
    if message.from_user.id != ADMIN_USER_ID:
        return
    rows = stats.recent_errors(15)
    await message.answer(build_errors_text(rows), parse_mode="HTML")


@router.callback_query(F.data.in_({"lang_kirill", "lang_lotin", "lang_rus"}))
async def set_language(callback: CallbackQuery):
    lang = callback.data
    user_language[callback.from_user.id] = lang

    confirmations = {
        "lang_kirill": (
            "✅ Тил танланди: Ўзбекча (Кирилл)\n"
            "Қуйидагиларни юборишингиз мумкин:\n"
            "🎬 Instagram Reels ҳаволаси\n"
            "📸 Instagram пост ҳаволаси\n"
            "🔴 YouTube Shorts — вақтинча ишламайди\n"
            "🎤 Овозли хабар\n"
            "/help — батафсил кўрсатма"
        ),
        "lang_lotin": (
            "✅ Til tanlandi: O'zbekcha (Lotin)\n"
            "Quyidagilarni yuborishingiz mumkin:\n"
            "🎬 Instagram Reels havolasi\n"
            "📸 Instagram post havolasi\n"
            "🔴 YouTube Shorts — vaqtincha ishlamaydi\n"
            "🎤 Ovozli xabar\n"
            "/help — batafsil ko'rsatma"
        ),
        "lang_rus": (
            "✅ Язык выбран: Русский\n"
            "Вы можете отправить:\n"
            "🎬 Ссылку на Instagram Reels\n"
            "📸 Ссылку на Instagram пост\n"
            "🔴 YouTube Shorts — временно не работает\n"
            "🎤 Голосовое сообщение\n"
            "/help — подробная инструкция"
        ),
    }
    await callback.message.edit_text(confirmations[lang])
    await callback.answer()

async def _account_unavailable(message: Message, lang: str):
    # Оддий фойдаланувчилар учун: аккаунт таҳлили ҳозирча ёпиқ (фақат маъмур синовда).
    stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "instagram_limit", "akkount tahlili vaqtincha o'chirilgan")
    if lang == "lang_rus":
        await message.answer(
            "🔴 Анализ аккаунта Instagram временно недоступен.\n"
            "Вы можете использовать другие функции:\n"
            "🎬 Instagram Reels\n"
            "📸 Instagram пост\n"
            "🔴 YouTube Shorts — временно не работает\n"
            "🎤 Голосовое сообщение"
        )
    elif lang == "lang_lotin":
        await message.answer(
            "🔴 Instagram akkaunt tahlili vaqtincha ishlamaydi.\n"
            "Boshqa xizmatlardan foydalanishingiz mumkin:\n"
            "🎬 Instagram Reels\n"
            "📸 Instagram post\n"
            "🔴 YouTube Shorts — vaqtincha ishlamaydi\n"
            "🎤 Ovozli xabar"
        )
    else:
        await message.answer(
            "🔴 Instagram аккаунт таҳлили вақтинча ишламайди.\n"
            "Бошқа хизматлардан фойдаланишингиз мумкин:\n"
            "🎬 Instagram Reels\n"
            "📸 Instagram пост\n"
            "🔴 YouTube Shorts — вақтинча ишламайди\n"
            "🎤 Овозли хабар"
        )


@router.message(F.text.func(lambda text: extract_account_url(text) is not None and extract_post_url(text) is None and extract_reel_url(text) is None))
@_one_job_per_user
async def handle_account(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")
    # Синов босқичи: аккаунт таҳлили фақат маъмур учун ScrapeCreators орқали ишлайди
    # (Profile + Posts ≈ 2 кредит). Бошқалар «вақтинча ишламайди» хабарини кўради.
    if message.from_user.id != ADMIN_USER_ID:
        await _account_unavailable(message, lang)
        return
    username = extract_account_url(message.text or "")
    status_msg = await message.answer("⏳ Аккаунт таҳлил қилиняпти...")
    try:
        posts, biography = await download_account_via_sc(username)
        if not posts:
            stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "download_error", "posts bo'sh")
            await status_msg.edit_text("❌ Аккаунтда постлар топилмади.")
            return
        await status_msg.edit_text("🔍 Мазмун таҳлил қилиняпти...")
        result = await analyze_account(posts, biography, username, lang)
        if is_moderation_refusal(result):
            stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "moderation", "refusal-heuristic")
            await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
            return
        result_text = f"👤 <b>@{html.escape(username)} аккаунт таҳлили:</b>\n\n{html.escape(result)}"
        if len(result_text) > 4000:
            await status_msg.edit_text(result_text[:4000], parse_mode="HTML")
            await message.answer(result_text[4000:], parse_mode="HTML")
        else:
            await status_msg.edit_text(result_text, parse_mode="HTML")
        stats.log_analysis(message.from_user.id, "instagram_account", lang, "success")
    except AccountFetchError as e:
        logging.warning(f"Аккаунт маълумотини олиб бўлмади: {e}")
        stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "download_error", str(e))
        await status_msg.edit_text(f"❌ Аккаунтни олиб бўлмади: {e}")
    except asyncio.TimeoutError:
        stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "download_error", "timeout")
        await status_msg.edit_text("⏳ Жавоб кечикди. Кейинроқ уриниб кўринг.")
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_account", message.from_user.id, "instagram_account"):
            return
        logging.error(f"Аккаунт таҳлили хатоси: user_id={message.from_user.id} error={e}")
        stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "other", str(e))
        await status_msg.edit_text("❌ Хато юз берди.")

@router.message(F.text.func(lambda text: extract_post_url(text) is not None))
@_one_job_per_user
async def handle_post(message: Message):
    text = message.text or ""
    url = extract_post_url(text)
    lang = user_language.get(message.from_user.id, "lang_kirill")
    if cache_get(url, lang):
        # Кешда фақат Reels оқимининг натижалари бор (видео постлар ҳам) —
        # Instagram'га сўровсиз шу ердан берилади
        await process_reel(message, url)
        return
    if await _daily_limit_hit(message, lang):
        return
    status_msg = await message.answer("⏳ Пост таҳлил қилиняпти...")
    images = None
    try:
        if lang == "lang_rus":
            await status_msg.edit_text("⬇️ Фото загружается...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⬇️ Rasm yuklanmoqda...")
        else:
            await status_msg.edit_text("⬇️ Расм юкланяпти...")

        images, caption = await download_instagram_image(url)

        if lang == "lang_rus":
            await status_msg.edit_text("🔍 Контент анализируется...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🔍 Mazmun tahlil qilinmoqda...")
        else:
            await status_msg.edit_text("🔍 Мазмун таҳлил қилиняпти...")

        if not images and not caption:
            stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "other", "mazmun topilmadi")
            if lang == "lang_rus":
                await status_msg.edit_text("❌ В посте не найдено содержимого для анализа.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("❌ Postda tahlil qilishga mazmun topilmadi.")
            else:
                await status_msg.edit_text("❌ Постда таҳлил қилишга мазмун топилмади.")
            return

        fallback_used = False
        try:
            if images:
                result = await analyze_image_content(images, caption, lang)
            else:
                result = await analyze_caption_only(caption, lang)
            refused = is_moderation_refusal(result)
        except BadRequestError as e:
            err_msg = str(e).lower()
            err_code = str(getattr(e, "code", "") or "").lower()
            if "content_filter" not in err_msg and "content_filter" not in err_code and "moderation" not in err_msg:
                raise
            result = None
            refused = True

        if refused:
            logging.warning(
                f"GPT-4o moderatsiya rad etishi (handle_post): user_id={message.from_user.id} "
                f"url={url}, gpt-4o-mini fallback urinilmoqda"
            )
            try:
                if images:
                    result = await analyze_image_content(images, caption, lang, "gpt-4o-mini")
                else:
                    result = await analyze_caption_only(caption, lang, "gpt-4o-mini")
                fallback_refused = is_moderation_refusal(result)
            except BadRequestError:
                fallback_refused = True

            if fallback_refused:
                logging.warning(f"gpt-4o-mini fallback ham rad etdi (handle_post): user_id={message.from_user.id} url={url}")
                stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "moderation", "refusal-heuristic+fallback")
                await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
                return
            else:
                fallback_used = True
                logging.info(f"gpt-4o-mini fallback muvaffaqiyatli (handle_post): user_id={message.from_user.id} url={url}")

        await _send_long(status_msg, message, result, None)

        stats.log_analysis(message.from_user.id, "instagram_post", lang, "success", fallback_used=1 if fallback_used else 0)
    except asyncio.TimeoutError:
        stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "download_error", "timeout")
        if lang == "lang_rus":
            await status_msg.edit_text("⏳ Instagram слишком долго не отвечает. Попробуйте позже.")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⏳ Instagram javob berishda kechikmoqda. Keyinroq urinib ko'ring.")
        else:
            await status_msg.edit_text("⏳ Instagram жавоб беришда кечикмоқда. Кейинроқ уриниб кўринг.")
    except VideoPostError:
        logging.info(f"/p/ havola video ekan, Reels yo'liga o'tkazildi: {url}")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await process_reel(message, url)
    except CookieExpiredError as e:
        await notify_cookie_expired(message, lang, "instagram_post", e)
        await status_msg.edit_text(COOKIE_EXPIRED_USER_MESSAGES.get(lang, COOKIE_EXPIRED_USER_MESSAGES["lang_kirill"]))
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_post", message.from_user.id, "instagram_post"):
            return
        logging.error(f"Post tahlili xatosi: user_id={message.from_user.id} url={url} error={e}")
        err = str(e).lower()
        if any(k in err for k in ("private", "restricted video", "not available")):
            stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "private_content", str(e))
            await status_msg.edit_text(PRIVATE_CONTENT_MESSAGES.get(lang, PRIVATE_CONTENT_MESSAGES["lang_kirill"]))
        elif is_rate_limited(e):
            stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "instagram_limit", str(e))
            if lang == "lang_rus":
                await status_msg.edit_text("⏳ Instagram временно ограничил запросы. Повторите через 10-15 минут.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("⏳ Instagram vaqtinchalik cheklov qo'ydi. 10-15 daqiqadan so'ng qayta urining.")
            else:
                await status_msg.edit_text("⏳ Instagram вақтинчалик чеклов қўйди. 10-15 дақиқадан сўнг қайта уриниб кўринг.")
        else:
            stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "other", str(e))
            if lang == "lang_rus":
                await status_msg.edit_text("❌ Произошла ошибка. Попробуйте другую ссылку.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("❌ Xatolik yuz berdi. Iltimos, boshqa havola yuboring.")
            else:
                await status_msg.edit_text("❌ Хато юз берди. Илтимос, бошқа ҳавола юборинг.")
    finally:
        if images:
            tmp_dir = os.path.dirname(images[0])
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

@router.message(F.voice)
@_one_job_per_user
async def handle_voice(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")
    if await _daily_limit_hit(message, lang):
        return

    if lang == "lang_rus":
        status_msg = await message.answer("⏳ Голосовое сообщение обрабатывается...")
    elif lang == "lang_lotin":
        status_msg = await message.answer("⏳ Ovozli xabar qayta ishlanmoqda...")
    else:
        status_msg = await message.answer("⏳ Овозли хабар қайта ишланяпти...")

    tmp_dir = None
    try:
        # Ovozli faylni yuklab olish
        voice = message.voice
        file = await message.bot.get_file(voice.file_id)

        import tempfile
        tmp_dir = tempfile.mkdtemp()
        ogg_path = os.path.join(tmp_dir, "voice.ogg")
        mp3_path = os.path.join(tmp_dir, "voice.mp3")

        # Faylni saqlash
        await message.bot.download_file(file.file_path, ogg_path)

        # ogg -> mp3 konvertatsiya
        if lang == "lang_rus":
            await status_msg.edit_text("🎙 Аудио преобразуется в текст...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🎙 Audio matnga aylantirilmoqda...")
        else:
            await status_msg.edit_text("🎙 Аудио матнга айлантирилаяпти...")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["ffmpeg", "-i", ogg_path, "-ar", "16000", "-ac", "1", mp3_path],
                capture_output=True,
                timeout=60,
            ),
        )

        # Whisper orqali transkripsiya
        from bot.utils.stt import transcribe_audio
        whisper_lang = None  # til avtomatik aniqlanadi; GPT keyin kerakli tilga tarjima qiladi
        text = await loop.run_in_executor(None, transcribe_audio, mp3_path, whisper_lang)

        if not text or not text.strip():
            stats.log_analysis(message.from_user.id, "voice", lang, "error", "other", "nutq aniqlanmadi")
            if lang == "lang_rus":
                await status_msg.edit_text("❌ Не удалось распознать речь. Попробуйте снова.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("❌ Nutq aniqlanmadi. Qayta urinib ko'ring.")
            else:
                await status_msg.edit_text("❌ Нутқ аниқланмади. Қайта уриниб кўринг.")
            return

        # Tahlil
        if lang == "lang_rus":
            await status_msg.edit_text("🔍 Контент анализируется...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🔍 Mazmun tahlil qilinmoqda...")
        else:
            await status_msg.edit_text("🔍 Мазмун таҳлил қилиняпти...")

        from bot.utils.analyzer import analyze_content
        fallback_used = False
        try:
            analysis = await loop.run_in_executor(None, analyze_content, text, lang)
            refused = is_moderation_refusal(analysis)
        except BadRequestError as e:
            err_msg = str(e).lower()
            err_code = str(getattr(e, "code", "") or "").lower()
            if "content_filter" not in err_msg and "content_filter" not in err_code and "moderation" not in err_msg:
                raise
            analysis = None
            refused = True

        if refused:
            logging.warning(
                f"GPT-4o moderatsiya rad etishi (handle_voice): user_id={message.from_user.id}, "
                f"gpt-4o-mini fallback urinilmoqda"
            )
            try:
                analysis = await loop.run_in_executor(None, analyze_content, text, lang, "gpt-4o-mini")
                fallback_refused = is_moderation_refusal(analysis)
            except BadRequestError:
                fallback_refused = True

            if fallback_refused:
                logging.warning(f"gpt-4o-mini fallback ham rad etdi (handle_voice): user_id={message.from_user.id}")
                stats.log_analysis(message.from_user.id, "voice", lang, "error", "moderation", "refusal-heuristic+fallback")
                await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
                return
            else:
                fallback_used = True
                logging.info(f"gpt-4o-mini fallback muvaffaqiyatli (handle_voice): user_id={message.from_user.id}")

        result_text = f"🔍 <b>{RESULT_HEADERS.get(lang, RESULT_HEADERS['lang_kirill'])}:</b>\n\n{_rich(analysis)}"

        # Telegram берган давомийлик: 5 сониядан қисқа овозда аниқлик паст бўлиши мумкин
        if voice.duration is not None and voice.duration < SHORT_VOICE_SECONDS:
            result_text += "\n\n" + SHORT_VOICE_NOTES.get(lang, SHORT_VOICE_NOTES["lang_kirill"])

        await _send_long(status_msg, message, result_text, "HTML")

        stats.log_analysis(message.from_user.id, "voice", lang, "success", fallback_used=1 if fallback_used else 0)

    except UnclearAudioError:
        stats.log_analysis(message.from_user.id, "voice", lang, "error", "other", "audio sifati past")
        if lang == "lang_rus":
            await status_msg.edit_text(
                "🔇 Качество аудио низкое.\n\n"
                "Голос слишком тихий или сообщение слишком короткое (менее 1.5 секунды).\n\n"
                "Пожалуйста, говорите чётче и отправьте снова."
            )
        elif lang == "lang_lotin":
            await status_msg.edit_text(
                "🔇 Audio sifati past.\n\n"
                "• Ovoz juda past yoki\n"
                "• Xabar juda qisqa (1.5 soniyadan kam)\n\n"
                "Iltimos, aniqroq gapirib qayta yuboring."
            )
        else:
            await status_msg.edit_text(
                "🔇 Аудио сифати паст.\n\n"
                "• Овоз жуда паст ёки\n"
                "• Хабар жуда қисқа (1.5 сониядан кам)\n\n"
                "Илтимос, аниқроқ гапириб қайта юборинг."
            )
    except subprocess.TimeoutExpired:
        stats.log_analysis(message.from_user.id, "voice", lang, "error", "download_error", "ffmpeg timeout")
        if lang == "lang_rus":
            await status_msg.edit_text("⏳ Обработка аудио заняла слишком много времени. Попробуйте снова.")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⏳ Audio qayta ishlash juda uzoq davom etdi. Qayta urinib ko'ring.")
        else:
            await status_msg.edit_text("⏳ Аудио қайта ишлаш жуда узоқ давом этди. Қайта уриниб кўринг.")
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_voice", message.from_user.id, "voice"):
            return
        logging.error(f"Ovozli xabar tahlili xatosi: user_id={message.from_user.id} error={e}")
        err = str(e).lower()
        if any(k in err for k in ("429", "too many", "rate", "wait a few minutes")):
            stats.log_analysis(message.from_user.id, "voice", lang, "error", "openai_credit", str(e))
            if lang == "lang_rus":
                await status_msg.edit_text("⏳ Слишком много запросов. Повторите через 10-15 минут.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("⏳ So'rovlar juda ko'p. 10-15 daqiqadan so'ng qayta urining.")
            else:
                await status_msg.edit_text("⏳ Сўровлар жуда кўп. 10-15 дақиқадан сўнг қайта уриниб кўринг.")
        else:
            stats.log_analysis(message.from_user.id, "voice", lang, "error", "other", str(e))
            if lang == "lang_rus":
                await status_msg.edit_text("❌ Произошла ошибка. Попробуйте снова.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("❌ Xatolik yuz berdi. Qayta urinib ko'ring.")
            else:
                await status_msg.edit_text("❌ Хато юз берди. Қайта уриниб кўринг.")
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

def _video_source(message: Message):
    """Хабарда видео файл бўлса (file_id, ҳажми, давомийлиги) қайтаради, акс ҳолда None.
    Қўллаб-қувватланади: оддий видео, думалоқ видео (video_note) ва видео турдаги ҳужжат."""
    if message.video:
        v = message.video
        return v.file_id, v.file_size, v.duration
    if message.video_note:
        v = message.video_note
        return v.file_id, v.file_size, v.duration
    doc = message.document
    if doc and (doc.mime_type or "").startswith("video/"):
        return doc.file_id, doc.file_size, None
    return None


async def _analyze_text_with_fallback(text: str, lang: str, user_id: int, context: str):
    """analyze_content'ни чақиради; модерация рад этса gpt-4o-mini билан қайта уринади.
    Қайтаради: (таҳлил матни, fallback_used) ёки иккала модел рад этса (None, False)."""
    loop = asyncio.get_event_loop()
    try:
        analysis = await loop.run_in_executor(None, analyze_content, text, lang)
        refused = is_moderation_refusal(analysis)
    except BadRequestError as e:
        err_msg = str(e).lower()
        err_code = str(getattr(e, "code", "") or "").lower()
        if "content_filter" not in err_msg and "content_filter" not in err_code and "moderation" not in err_msg:
            raise
        analysis = None
        refused = True

    if not refused:
        return analysis, False

    logging.warning(f"GPT-4o moderatsiya rad etishi ({context}): user_id={user_id}, gpt-4o-mini fallback urinilmoqda")
    try:
        analysis = await loop.run_in_executor(None, analyze_content, text, lang, "gpt-4o-mini")
        fallback_refused = is_moderation_refusal(analysis)
    except BadRequestError:
        fallback_refused = True

    if fallback_refused:
        logging.warning(f"gpt-4o-mini fallback ham rad etdi ({context}): user_id={user_id}")
        return None, False
    logging.info(f"gpt-4o-mini fallback muvaffaqiyatli ({context}): user_id={user_id}")
    return analysis, True


@router.message(F.func(lambda message: _video_source(message) is not None))
@_one_job_per_user
async def handle_video_file(message: Message):
    """Фойдаланувчи юборган видео файлни таҳлил қилади (Instagram'га боғлиқ эмас):
    файл юкланади → аудио ажратилади → STT → таҳлил. Овозли хабар ҳандлери (handle_voice)
    билан бир хил занжир."""
    user_id = message.from_user.id
    lang = user_language.get(user_id, "lang_kirill")

    def msg(key: str) -> str:
        return VIDEO_FILE_MESSAGES[key].get(lang, VIDEO_FILE_MESSAGES[key]["lang_kirill"])

    if await _daily_limit_hit(message, lang):
        return

    file_id, file_size, duration = _video_source(message)

    if file_size and file_size > MAX_TG_DOWNLOAD_BYTES:
        stats.log_analysis(user_id, "video_file", lang, "error", "other", f"fayl katta: {file_size}")
        await message.answer(msg("too_big"))
        return
    if duration and duration > MAX_VIDEO_FILE_SECONDS:
        stats.log_analysis(user_id, "video_file", lang, "error", "other", f"video uzun: {duration}s")
        await message.answer(msg("too_long"))
        return

    status_msg = await message.answer(msg("status"))
    tmp_dir = None
    try:
        file = await message.bot.get_file(file_id)
        tmp_dir = tempfile.mkdtemp()
        src_path = os.path.join(tmp_dir, "input_video")
        mp3_path = os.path.join(tmp_dir, "audio.mp3")
        await message.bot.download_file(file.file_path, src_path)

        # Видеодан овозни ажратиш. -t: давомийлиги номаълум файлда (ҳужжат) ҳам
        # фақат биринчи MAX_VIDEO_FILE_SECONDS сония олинади.
        loop = asyncio.get_event_loop()
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["ffmpeg", "-y", "-i", src_path, "-vn", "-t", str(MAX_VIDEO_FILE_SECONDS),
                 "-ar", "16000", "-ac", "1", mp3_path],
                capture_output=True,
                timeout=120,
            ),
        )
        if proc.returncode != 0 or not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
            stats.log_analysis(user_id, "video_file", lang, "error", "other", "ovoz ajratilmadi")
            await status_msg.edit_text(msg("no_audio"))
            return

        text = await loop.run_in_executor(None, transcribe_audio, mp3_path, None)
        if not text or not text.strip():
            stats.log_analysis(user_id, "video_file", lang, "error", "other", "nutq aniqlanmadi")
            await status_msg.edit_text(msg("no_audio"))
            return

        analysis, fallback_used = await _analyze_text_with_fallback(text, lang, user_id, "handle_video_file")
        if analysis is None:
            stats.log_analysis(user_id, "video_file", lang, "error", "moderation", "refusal-heuristic+fallback")
            await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
            return

        result_text = f"🔍 <b>{RESULT_HEADERS.get(lang, RESULT_HEADERS['lang_kirill'])}:</b>\n\n{_rich(analysis)}"
        await _send_long(status_msg, message, result_text, "HTML")

        stats.log_analysis(user_id, "video_file", lang, "success", fallback_used=1 if fallback_used else 0)

    except TelegramBadRequest as e:
        if "too big" in str(e).lower():
            stats.log_analysis(user_id, "video_file", lang, "error", "other", "fayl katta (Telegram)")
            await status_msg.edit_text(msg("too_big"))
        else:
            logging.error(f"Video fayl Telegram xatosi: user_id={user_id} error={e}")
            stats.log_analysis(user_id, "video_file", lang, "error", "other", str(e))
            await status_msg.edit_text(msg("error"))
    except UnclearAudioError:
        stats.log_analysis(user_id, "video_file", lang, "error", "other", "audio sifati past")
        await status_msg.edit_text(msg("no_audio"))
    except subprocess.TimeoutExpired:
        stats.log_analysis(user_id, "video_file", lang, "error", "download_error", "ffmpeg timeout")
        await status_msg.edit_text(msg("no_audio"))
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_video_file", user_id, "video_file"):
            return
        logging.error(f"Video fayl tahlili xatosi: user_id={user_id} error={e}")
        stats.log_analysis(user_id, "video_file", lang, "error", "other", str(e))
        await status_msg.edit_text(msg("error"))
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


@router.message(F.text.func(lambda text: extract_reel_url(text) is not None))
@_one_job_per_user
async def handle_reel(message: Message):
    url = extract_reel_url(message.text)
    await process_reel(message, url)


NO_AUDIO_TRACK_MESSAGES = {
    "lang_kirill": "⚠️ Бу видеода овоз йўқ (аудио қатлам топилмади).\n\nОвозли бошқа Reels ҳаволасини юборинг.",
    "lang_lotin": "⚠️ Bu videoda ovoz yo'q (audio qatlam topilmadi).\n\nOvozli boshqa Reels havolasini yuboring.",
    "lang_rus": "⚠️ В этом видео нет звука (аудиодорожка не найдена).\n\nОтправьте ссылку на другой Reels со звуком.",
}

REEL_TOO_LONG_MESSAGES = {
    "lang_kirill": "⚠️ Видео жуда узун ({minutes} дақиқадан ортиқ). Қисқароқ Reels ҳаволасини юборинг.",
    "lang_lotin": "⚠️ Video juda uzun ({minutes} daqiqadan ortiq). Qisqaroq Reels havolasini yuboring.",
    "lang_rus": "⚠️ Видео слишком длинное (больше {minutes} минут). Отправьте ссылку на более короткий Reels.",
}


async def process_reel(message: Message, url: str):
    """Reels (yoki video bo'lib chiqqan /p/ post) havolasini yuklab,
    transkripsiya qiladi va tahlil natijasini yuboradi."""
    lang = user_language.get(message.from_user.id, "lang_kirill")
    logging.info(f"REELS SO'ROV: user_id={message.from_user.id} url={url}")

    cached = cache_get(url, lang)
    if cached:
        logging.info(f"KESH: user_id={message.from_user.id} url={url}")
        for part in _split_message(cached):
            await message.answer(part, parse_mode="HTML")
        stats.log_analysis(message.from_user.id, "instagram_reels", lang, "success", error_type="cache_hit")
        return

    if await _daily_limit_hit(message, lang):
        return

    if lang == "lang_rus":
        status_msg = await message.answer("⏳ Видео загружается...")
    elif lang == "lang_lotin":
        status_msg = await message.answer("⏳ Video yuklab olinmoqda...")
    else:
        status_msg = await message.answer("⏳ Видео юклаб олинмоқда...")
    file_path = None
    tmp_dir = None
    loop = asyncio.get_running_loop()

    try:
        if lang == "lang_rus":
            await status_msg.edit_text("⬇️ Видео загружается...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⬇️ Video yuklanmoqda...")
        else:
            await status_msg.edit_text("⬇️ Видео юкланяпти...")

        cached_tr = transcript_get(url)
        if cached_tr:
            text, reel_caption = cached_tr
            used_cookie_fallback = False
            used_sc = False
            author = author_get(url)
            logging.info(f"TRANSKRIPSIYA KESHI: user_id={message.from_user.id} url={url}")
        else:
            file_path, reel_caption, used_cookie_fallback = await download_reels_audio(url)
            author = pop_reel_author(url)
            used_sc = pop_scrapecreators_used(url)
            author_put(url, author)
            tmp_dir = os.path.dirname(file_path)

            if lang == "lang_rus":
                await status_msg.edit_text("🎙 Аудио преобразуется в текст...")
            elif lang == "lang_lotin":
                await status_msg.edit_text("🎙 Audio matnga aylantirilmoqda...")
            else:
                await status_msg.edit_text("🎙 Аудио матнга айлантирилаяпти...")

            try:
                whisper_lang = None  # til avtomatik aniqlanadi; GPT keyin kerakli tilga tarjima qiladi
                text = await loop.run_in_executor(None, transcribe_audio, file_path, whisper_lang, reel_caption)
            except UnclearAudioError as e:
                logging.warning(f"Aniq bo'lmagan audio (user_id={message.from_user.id}, url={url}): {e}")
                stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "other", "audio sifati past")
                unclear_messages = {
                    "lang_kirill": (
                        "⚠️ Видеода аниқ товуш ёки нутқ топилмади "
                        "(жуда қисқа ёки жуда паст овозли).\n\n"
                        "Илтимос, бошқа Reels ҳаволасини юборинг."
                    ),
                    "lang_lotin": (
                        "⚠️ Videoda aniq tovush yoki nutq topilmadi "
                        "(juda qisqa yoki juda past ovozli).\n\n"
                        "Iltimos, boshqa Reels havolasini yuboring."
                    ),
                    "lang_rus": (
                        "⚠️ В видео не найден чёткий звук или речь "
                        "(слишком коротко или слишком тихо).\n\n"
                        "Пожалуйста, отправьте другую ссылку на Reels."
                    ),
                }
                await status_msg.edit_text(unclear_messages.get(lang, unclear_messages["lang_kirill"]))
                return
            transcript_put(url, text, reel_caption)

        logging.info(f"WHISPER MATNI (user_id={message.from_user.id}): {text[:1000]}")

        if reel_caption:
            text = f"CAPTION:\n{reel_caption}\n\nAUDIO MATNI:\n{text}"

        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            file_path = None

        if lang == "lang_rus":
            await status_msg.edit_text("🔍 Контент анализируется...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🔍 Mazmun tahlil qilinmoqda...")
        else:
            await status_msg.edit_text("🔍 Мазмун таҳлил қилиняпти...")

        fallback_used = False
        try:
            analysis = await loop.run_in_executor(None, analyze_content, text, lang)
            refused = is_moderation_refusal(analysis)
        except BadRequestError as e:
            err_msg = str(e).lower()
            err_code = str(getattr(e, "code", "") or "").lower()
            if "content_filter" not in err_msg and "content_filter" not in err_code and "moderation" not in err_msg:
                raise
            analysis = None
            refused = True

        if refused:
            logging.warning(
                f"GPT-4o moderatsiya rad etishi (handle_reel): user_id={message.from_user.id} "
                f"url={url}, gpt-4o-mini fallback urinilmoqda"
            )
            try:
                analysis = await loop.run_in_executor(None, analyze_content, text, lang, "gpt-4o-mini")
                fallback_refused = is_moderation_refusal(analysis)
            except BadRequestError:
                fallback_refused = True

            if fallback_refused:
                logging.warning(f"gpt-4o-mini fallback ham rad etdi (handle_reel): user_id={message.from_user.id} url={url}")
                stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "moderation", "refusal-heuristic+fallback")
                await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
                return
            else:
                fallback_used = True
                logging.info(f"gpt-4o-mini fallback muvaffaqiyatli (handle_reel): user_id={message.from_user.id} url={url}")

        result_text = (
            f"🔍 <b>{RESULT_HEADERS.get(lang, RESULT_HEADERS['lang_kirill'])}:</b>\n\n"
            f"{_author_line(lang, author)}"
            f"{_rich(analysis, reel_caption, lang)}"
            f"{_links_block(lang, reel_caption, url)}"
        )
        cache_put(url, lang, result_text)

        await _send_long(status_msg, message, result_text, "HTML")

        _recent_http400.clear()
        stats.log_analysis(
            message.from_user.id, "instagram_reels", lang, "success",
            error_type="scrapecreators" if used_sc else ("cookie_fallback" if used_cookie_fallback else None),
            fallback_used=1 if fallback_used else 0,
        )

    except asyncio.TimeoutError:
        stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "download_error", "timeout")
        if lang == "lang_rus":
            await status_msg.edit_text("⏳ Instagram слишком долго не отвечает. Попробуйте позже.")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⏳ Instagram javob berishda kechikmoqda. Keyinroq urinib ko'ring.")
        else:
            await status_msg.edit_text("⏳ Instagram жавоб беришда кечикмоқда. Кейинроқ уриниб кўринг.")
    except ReelTooLongError as e:
        stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "other", f"video juda uzun: {e.duration}s")
        await status_msg.edit_text(
            REEL_TOO_LONG_MESSAGES.get(lang, REEL_TOO_LONG_MESSAGES["lang_kirill"]).format(minutes=MAX_REEL_SECONDS // 60)
        )
    except PrivateContentError:
        stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "private_content", "xususiy kontent")
        await status_msg.edit_text(PRIVATE_CONTENT_MESSAGES.get(lang, PRIVATE_CONTENT_MESSAGES["lang_kirill"]))
    except CookieExpiredError as e:
        await notify_cookie_expired(message, lang, "instagram_reels", e)
        await status_msg.edit_text(COOKIE_EXPIRED_USER_MESSAGES.get(lang, COOKIE_EXPIRED_USER_MESSAGES["lang_kirill"]))
    except NoAudioTrackError as e:
        logging.warning(f"Videoda ovoz yo'q (user_id={message.from_user.id}, url={url}): {e}")
        stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "other", "videoda ovoz yo'q")
        await status_msg.edit_text(NO_AUDIO_TRACK_MESSAGES.get(lang, NO_AUDIO_TRACK_MESSAGES["lang_kirill"]))
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_reel", message.from_user.id, "instagram_reels"):
            return
        logging.error(f"Error handling reel: {e}")
        err = str(e).lower()
        if is_rate_limited(e):
            stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "instagram_limit", str(e))
            rate_messages = {
                "lang_kirill": (
                    "⏳ Instagram вақтинчалик чеклов қўйди.\n\n"
                    "10-15 дақиқадан сўнг қайта уриниб кўринг.\n\n"
                    "Ёки бошқа Reels ҳаволасини юборинг."
                ),
                "lang_lotin": (
                    "⏳ Instagram vaqtinchalik cheklov qo'ydi.\n\n"
                    "10-15 daqiqadan so'ng qayta urining.\n\n"
                    "Yoki boshqa Reels havolasini yuboring."
                ),
                "lang_rus": (
                    "⏳ Instagram временно ограничил доступ.\n\n"
                    "Повторите попытку через 10-15 минут.\n\n"
                    "Или отправьте другую ссылку на Reels."
                ),
            }
            await status_msg.edit_text(
                rate_messages.get(lang, rate_messages["lang_kirill"])
                + VIDEO_FILE_HINT.get(lang, VIDEO_FILE_HINT["lang_kirill"])
            )
        elif "http error 400" in err and register_http400(url):
            # Ketma-ket bir nechta turli havolada 400: muammo havolada emas, hisob yoki cookie'da.
            await notify_cookie_expired(message, lang, "instagram_reels", e, admin_text=HTTP400_ADMIN_MESSAGE)
            await status_msg.edit_text(
                COOKIE_EXPIRED_USER_MESSAGES.get(lang, COOKIE_EXPIRED_USER_MESSAGES["lang_kirill"])
                + VIDEO_FILE_HINT.get(lang, VIDEO_FILE_HINT["lang_kirill"])
            )
        elif "http error 400" in err or "empty media response" in err:
            # Instagram aynan shu havola uchun 400 yoki bo'sh javob ("empty media
            # response") qaytardi (o'chirilgan, cheklangan
            # yoki video bo'lmagan kontent). Cookie va limit bilan bog'liq emas.
            stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "bad_link", str(e))
            bad_link_messages = {
                "lang_kirill": (
                    "⚠️ Бу Reels юкланмади.\n\n"
                    "У ўчирилган, чекланган ёки видео бўлмаслиги мумкин. "
                    "Ҳаволани текшириб, қайта юборинг ёки бошқа Reels ҳаволасини юборинг."
                ),
                "lang_lotin": (
                    "⚠️ Bu Reels yuklanmadi.\n\n"
                    "U o'chirilgan, cheklangan yoki video bo'lmasligi mumkin. "
                    "Havolani tekshirib, qayta yuboring yoki boshqa Reels havolasini yuboring."
                ),
                "lang_rus": (
                    "⚠️ Этот Reels не загрузился.\n\n"
                    "Он мог быть удалён, ограничен или не быть видео. "
                    "Проверьте ссылку и отправьте её ещё раз или отправьте другую ссылку на Reels."
                ),
            }
            await status_msg.edit_text(
                bad_link_messages.get(lang, bad_link_messages["lang_kirill"])
                + VIDEO_FILE_HINT.get(lang, VIDEO_FILE_HINT["lang_kirill"])
            )
        else:
            stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "other", str(e))
            error_messages = {
                "lang_kirill": "❌ Хато юз берди. Илтимос, бошқа Reels ҳаволасини юборинг.",
                "lang_lotin": "❌ Xato yuz berdi. Iltimos, boshqa Reels havolasini yuboring.",
                "lang_rus": "❌ Произошла ошибка. Пожалуйста, отправьте другую ссылку на Reels.",
            }
            await status_msg.edit_text(
                error_messages.get(lang, error_messages["lang_kirill"])
                + VIDEO_FILE_HINT.get(lang, VIDEO_FILE_HINT["lang_kirill"])
            )
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

@router.message(F.text.func(lambda text: extract_youtube_url(text) is not None))
async def handle_youtube(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")
    await message.answer(YOUTUBE_DISABLED_MESSAGES.get(lang, YOUTUBE_DISABLED_MESSAGES["lang_kirill"]))
    return

    url = extract_youtube_url(message.text)
    logging.info(f"YOUTUBE SO'ROV: user_id={message.from_user.id} url={url} shorts_path={is_youtube_shorts_path(url)}")
    status_msg = await message.answer(YOUTUBE_STATUS_MESSAGES.get(lang, YOUTUBE_STATUS_MESSAGES["lang_kirill"]))
    file_path = None
    tmp_dir = None
    loop = asyncio.get_running_loop()

    try:
        # 1-urinish: YouTube Data API orqali subtitr (tezroq, yt-dlp/Whisper'siz)
        video_id = extract_youtube_video_id(url)
        subtitle_text = await loop.run_in_executor(None, get_youtube_subtitles, video_id) if video_id else None

        if subtitle_text:
            if lang == "lang_rus":
                await status_msg.edit_text("🔍 Контент анализируется...")
            elif lang == "lang_lotin":
                await status_msg.edit_text("🔍 Mazmun tahlil qilinmoqda...")
            else:
                await status_msg.edit_text("🔍 Мазмун таҳлил қилиняпти...")

            analysis = await loop.run_in_executor(None, analyze_content, subtitle_text, lang)
            result_text = (
                f"🔍 <b>{RESULT_HEADERS.get(lang, RESULT_HEADERS['lang_kirill'])}:</b>\n\n"
                f"{_rich(analysis)}"
            )
            await _send_long(status_msg, message, result_text, "HTML")

            stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "success")
            return

        # Subtitr topilmadi (yoki video_id ajratib bo'lmadi, yoki OAuth2
        # yo'qligi sababli yuklab bo'lmadi) — foydalanuvchiga xabar bermay,
        # avvalgi yt-dlp+Whisper pipeline'iga xabarsiz o'tiladi.
        file_path, video_caption = await download_youtube_shorts(url)
        yt_author = pop_reel_author(url)
        tmp_dir = os.path.dirname(file_path)

        if lang == "lang_rus":
            await status_msg.edit_text("🎙 Аудио преобразуется в текст...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🎙 Audio matnga aylantirilmoqda...")
        else:
            await status_msg.edit_text("🎙 Аудио матнга айлантирилаяпти...")

        try:
            whisper_lang = None  # til avtomatik aniqlanadi; GPT keyin kerakli tilga tarjima qiladi
            text = await loop.run_in_executor(None, transcribe_audio, file_path, whisper_lang)
        except UnclearAudioError as e:
            logging.warning(f"Aniq bo'lmagan audio (user_id={message.from_user.id}, url={url}): {e}")
            stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "error", "other", "audio sifati past")
            unclear_messages = {
                "lang_kirill": (
                    "⚠️ Видеода аниқ товуш ёки нутқ топилмади "
                    "(жуда қисқа ёки жуда паст овозли).\n\n"
                    "Илтимос, бошқа YouTube Shorts ҳаволасини юборинг."
                ),
                "lang_lotin": (
                    "⚠️ Videoda aniq tovush yoki nutq topilmadi "
                    "(juda qisqa yoki juda past ovozli).\n\n"
                    "Iltimos, boshqa YouTube Shorts havolasini yuboring."
                ),
                "lang_rus": (
                    "⚠️ В видео не найден чёткий звук или речь "
                    "(слишком коротко или слишком тихо).\n\n"
                    "Пожалуйста, отправьте другую ссылку на YouTube Shorts."
                ),
            }
            await status_msg.edit_text(unclear_messages.get(lang, unclear_messages["lang_kirill"]))
            return

        logging.info(f"WHISPER MATNI YOUTUBE (user_id={message.from_user.id}): {text[:1000]}")

        if video_caption:
            text = f"CAPTION:\n{video_caption}\n\nAUDIO MATNI:\n{text}"

        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            file_path = None

        if lang == "lang_rus":
            await status_msg.edit_text("🔍 Контент анализируется...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🔍 Mazmun tahlil qilinmoqda...")
        else:
            await status_msg.edit_text("🔍 Мазмун таҳлил қилиняпти...")

        fallback_used = False
        try:
            analysis = await loop.run_in_executor(None, analyze_content, text, lang)
            refused = is_moderation_refusal(analysis)
        except BadRequestError as e:
            err_msg = str(e).lower()
            err_code = str(getattr(e, "code", "") or "").lower()
            if "content_filter" not in err_msg and "content_filter" not in err_code and "moderation" not in err_msg:
                raise
            analysis = None
            refused = True

        if refused:
            logging.warning(
                f"GPT-4o moderatsiya rad etishi (handle_youtube): user_id={message.from_user.id} "
                f"url={url}, gpt-4o-mini fallback urinilmoqda"
            )
            try:
                analysis = await loop.run_in_executor(None, analyze_content, text, lang, "gpt-4o-mini")
                fallback_refused = is_moderation_refusal(analysis)
            except BadRequestError:
                fallback_refused = True

            if fallback_refused:
                logging.warning(f"gpt-4o-mini fallback ham rad etdi (handle_youtube): user_id={message.from_user.id} url={url}")
                stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "error", "moderation", "refusal-heuristic+fallback")
                await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
                return
            else:
                fallback_used = True
                logging.info(f"gpt-4o-mini fallback muvaffaqiyatli (handle_youtube): user_id={message.from_user.id} url={url}")

        result_text = (
            f"🔍 <b>{RESULT_HEADERS.get(lang, RESULT_HEADERS['lang_kirill'])}:</b>\n\n"
            f"{_author_line(lang, yt_author)}"
            f"{_rich(analysis, video_caption, lang)}"
            f"{_links_block(lang, video_caption)}"
        )

        await _send_long(status_msg, message, result_text, "HTML")

        stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "success", fallback_used=1 if fallback_used else 0)

    except VideoTooLongError as e:
        logging.warning(f"YouTube video juda uzun: user_id={message.from_user.id} url={url} duration={e.duration}s")
        stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "error", "download_error", f"video {e.duration}s uzun")
        await status_msg.edit_text(YOUTUBE_TOO_LONG_MESSAGES.get(lang, YOUTUBE_TOO_LONG_MESSAGES["lang_kirill"]))
    except AudioExtractionFailedError as e:
        logging.warning(f"YouTube audio ajratib bo'lmadi: user_id={message.from_user.id} url={url} error={e}")
        stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "error", "download_error", "audio ajratilmadi")
        audio_fail_messages = {
            "lang_kirill": (
                "⚠️ Бу видео учун аудио олиб бўлмади. "
                "YouTube серверида вақтинчалик чеклов. "
                "Бошқа видео синаб кўринг."
            ),
            "lang_lotin": "⚠️ Bu video uchun audio olib bo'lmadi. Boshqa video sinab ko'ring.",
            "lang_rus": "⚠️ Не удалось получить аудио для этого видео. Попробуйте другое видео.",
        }
        await status_msg.edit_text(audio_fail_messages.get(lang, audio_fail_messages["lang_kirill"]))
    except asyncio.TimeoutError:
        logging.warning(f"YouTube tahlili timeout: user_id={message.from_user.id} url={url}")
        stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "error", "download_error", "timeout")
        if lang == "lang_rus":
            await status_msg.edit_text("⏳ YouTube слишком долго не отвечает. Попробуйте позже.")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⏳ YouTube javob berishda kechikmoqda. Keyinroq urinib ko'ring.")
        else:
            await status_msg.edit_text("⏳ YouTube жавоб беришда кечикмоқда. Кейинроқ уриниб кўринг.")
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_youtube", message.from_user.id, "youtube_shorts"):
            return
        logging.error(f"YouTube tahlili xatosi: user_id={message.from_user.id} url={url} error={e}")
        err = str(e).lower()
        if any(k in err for k in ("private video", "this is a private", "sign in if you")):
            stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "error", "private_content", str(e))
            await status_msg.edit_text(PRIVATE_CONTENT_MESSAGES.get(lang, PRIVATE_CONTENT_MESSAGES["lang_kirill"]))
        else:
            stats.log_analysis(message.from_user.id, "youtube_shorts", lang, "error", "other", str(e))
            if lang == "lang_rus":
                await status_msg.edit_text("❌ Произошла ошибка. Попробуйте другую ссылку.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("❌ Xatolik yuz berdi. Iltimos, boshqa havola yuboring.")
            else:
                await status_msg.edit_text("❌ Хато юз берди. Илтимос, бошқа ҳавола юборинг.")
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

@router.message()
async def echo_all(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")
    if lang == "lang_rus":
        text = "📎 Отправьте ссылку на Instagram Reels/пост или YouTube Shorts.\n\nℹ️ /help — справка"
    elif lang == "lang_lotin":
        text = "📎 Instagram Reels/post yoki YouTube Shorts havolasini yuboring.\n\nℹ️ /help — yordam"
    else:
        text = "📎 Instagram Reels/пост ёки YouTube Shorts ҳаволасини юборинг.\n\nℹ️ /help — ёрдам"
    await message.answer(text)