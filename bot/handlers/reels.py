from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
import asyncio
import html
import os
import re
import shutil
import subprocess
import logging
from urllib.parse import urlparse
from openai import RateLimitError, AuthenticationError, BadRequestError
from bot.utils.downloader import (
    download_reels_audio, download_instagram_image, download_account_posts,
    download_youtube_shorts, VideoTooLongError, AudioExtractionFailedError,
    CookieExpiredError,
)
from bot.utils import stats
from bot.utils.stt import transcribe_audio, UnclearAudioError
from bot.utils.analyzer import analyze_content, analyze_image_content, analyze_caption_only, analyze_account

router = Router()

ADMIN_USER_ID = 47575298

user_language: dict[int, str] = {}

COOKIE_EXPIRED_USER_MESSAGES = {
    "lang_kirill": "⚠️ Техник носозлик. Тез орада тузатилади.",
    "lang_lotin": "⚠️ Texnik nosozlik. Tez orada tuzatiladi.",
    "lang_rus": "⚠️ Техническая неполадка. Скоро будет исправлено.",
}

COOKIE_EXPIRED_ADMIN_MESSAGE = (
    "⚠️ Instagram cookie eskirdi! COOKIE_YANGILASH.md bo'yicha yangilang."
)


async def notify_cookie_expired(message: Message, lang: str, source_type: str) -> None:
    """Instagram cookie eskirganda foydalanuvchiga texnik nosozlik
    xabarini, adminga esa cookie yangilash haqida alohida ogohlantirish
    yuboradi va statistikaga 'cookie_expired' sifatida yozadi."""
    logging.error(f"Instagram cookie eskirgan ({source_type}): user_id={message.from_user.id}")
    stats.log_analysis(message.from_user.id, source_type, lang, "error", "cookie_expired")
    try:
        await message.bot.send_message(ADMIN_USER_ID, COOKIE_EXPIRED_ADMIN_MESSAGE)
    except Exception as e:
        logging.error(f"Adminga cookie ogohlantirishini yuborib bo'lmadi: {e}")

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

MODERATION_MESSAGES = {
    "lang_kirill": "⚠️ Ушбу контент таҳлил қилинмади. Мазмун хизмат шартларига мос келмайди.",
    "lang_lotin": "⚠️ Ushbu kontent tahlil qilinmadi. Mazmun xizmat shartlariga mos kelmaydi.",
    "lang_rus": (
        "⚠️ Контент не был проанализирован. Содержание не "
        "соответствует условиям сервиса."
    ),
}

YOUTUBE_STATUS_MESSAGES = {
    "lang_kirill": "🎬 YouTube Shorts таҳлил қилиняпти...",
    "lang_lotin": "🎬 YouTube Shorts tahlil qilinmoqda...",
    "lang_rus": "🎬 YouTube Shorts анализируется...",
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
)


def is_moderation_refusal(text: str | None) -> bool:
    """GPT-4o javobi moderatsiya sababli rad etish (refusal) matni
    ekanligini aniqlaydi — model so'rovni bajarishdan bosh tortganda
    odatda ingliz tilida qisqa 'can't assist' uslubidagi javob beradi,
    tizim promptidagi til talabidan qat'i nazar."""
    if not text:
        return False
    lowered = text.lower()
    return any(p in lowered for p in _REFUSAL_PATTERNS)


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
        "👤 Instagram akkount / аккаунт таҳлили\n\n"
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
            "• Отправьте ссылку на аккаунт — анализ последних 3 постов\n\n"
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
            "• Akkount havolasini yuboring — oxirgi 3 post tahlil qilinadi\n\n"
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
            "• Аккаунт ҳаволасини юборинг — охирги 3 пост таҳлил қилинади\n\n"
            "⚙️ Буйруқлар:\n"
            "/start — қайтадан бошлаш\n"
            "/help — ушбу ёрдам"
        )
    await message.answer(text, parse_mode="HTML")

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")
    s = stats.get_stats()
    et = s["today_error_types"]
    src = s["by_source"]
    lng = s["by_lang"]

    hourly_text = ""
    if s["hourly"]:
        if lang == "lang_rus":
            hourly_rows = "\n".join(f"{h}:00 — {c}" for h, c in sorted(s["hourly"].items()))
            hourly_text = f"\n\n📈 Активность сегодня:\n{hourly_rows}\nВсего: {s['today_total']}"
        elif lang == "lang_lotin":
            hourly_rows = "\n".join(f"{h}:00 — {c} ta" for h, c in sorted(s["hourly"].items()))
            hourly_text = f"\n\n📈 Bugungi faollik:\n{hourly_rows}\nJami: {s['today_total']} ta"
        else:
            hourly_rows = "\n".join(f"{h}:00 — {c} та" for h, c in sorted(s["hourly"].items()))
            hourly_text = f"\n\n📈 Бугунги фаоллик:\n{hourly_rows}\nЖами: {s['today_total']} та"

    if lang == "lang_rus":
        text = (
            f"📊 Статистика:\n"
            f"📅 Сегодня: {s['today_total']} запросов\n"
            f"  ✅ Успешно: {s['today_success']}\n"
            f"  ❌ Ошибки: {s['today_error']}\n"
            f"    - Ограничение Instagram: {et['instagram_limit']}\n"
            f"    - Кредит OpenAI: {et['openai_credit']}\n"
            f"    - Модерация: {et['moderation']}\n"
            f"    - Другое: {et['boshqa']}\n\n"
            f"📅 На этой неделе: {s['week_total']}\n"
            f"📅 В этом месяце: {s['month_total']}\n"
            f"📅 Всего (с начала работы бота): {s['all_total']}\n\n"
            f"📌 По типу (всего):\n"
            f"  🎬 Instagram Reels: {src['instagram_reels']}\n"
            f"  📸 Instagram пост: {src['instagram_post']}\n"
            f"  👤 Instagram аккаунт: {src['instagram_account']}\n"
            f"  🎥 YouTube Shorts: {src['youtube_shorts']}\n"
            f"  🎤 Голосовое сообщение: {src['voice']}\n\n"
            f"📌 По языку (всего):\n"
            f"  🇺🇿 Узбекский кириллица: {lng['lang_kirill']}\n"
            f"  🔤 Узбекский латиница: {lng['lang_lotin']}\n"
            f"  🇷🇺 Русский: {lng['lang_rus']}\n\n"
            f"🔄 Fallback (gpt-4o-mini): {s['fallback_used_count']} ta"
            f"{hourly_text}"
        )
    elif lang == "lang_lotin":
        text = (
            f"📊 Statistika:\n"
            f"📅 Bugun: {s['today_total']} ta so'rov\n"
            f"  ✅ Muvaffaqiyatli: {s['today_success']}\n"
            f"  ❌ Xato: {s['today_error']}\n"
            f"    - Instagram cheklov: {et['instagram_limit']}\n"
            f"    - OpenAI kredit: {et['openai_credit']}\n"
            f"    - Moderatsiya: {et['moderation']}\n"
            f"    - Boshqa: {et['boshqa']}\n\n"
            f"📅 Bu hafta: {s['week_total']} ta\n"
            f"📅 Bu oy: {s['month_total']} ta\n"
            f"📅 Jami (botdan beri): {s['all_total']} ta\n\n"
            f"📌 Tur bo'yicha (jami):\n"
            f"  🎬 Instagram Reels: {src['instagram_reels']}\n"
            f"  📸 Instagram post: {src['instagram_post']}\n"
            f"  👤 Instagram akkaunt: {src['instagram_account']}\n"
            f"  🎥 YouTube Shorts: {src['youtube_shorts']}\n"
            f"  🎤 Ovozli xabar: {src['voice']}\n\n"
            f"📌 Til bo'yicha (jami):\n"
            f"  🇺🇿 O'zbek kirill: {lng['lang_kirill']}\n"
            f"  🔤 O'zbek lotin: {lng['lang_lotin']}\n"
            f"  🇷🇺 Rus: {lng['lang_rus']}\n\n"
            f"🔄 Fallback (gpt-4o-mini): {s['fallback_used_count']} ta"
            f"{hourly_text}"
        )
    else:
        text = (
            f"📊 Статистика:\n"
            f"📅 Бугун: {s['today_total']} та сўров\n"
            f"  ✅ Муваффақиятли: {s['today_success']}\n"
            f"  ❌ Хато: {s['today_error']}\n"
            f"    - Instagram чеклов: {et['instagram_limit']}\n"
            f"    - OpenAI кредит: {et['openai_credit']}\n"
            f"    - Модерация: {et['moderation']}\n"
            f"    - Бошқа: {et['boshqa']}\n\n"
            f"📅 Бу ҳафта: {s['week_total']} та\n"
            f"📅 Бу ой: {s['month_total']} та\n"
            f"📅 Жами (ботдан бери): {s['all_total']} та\n\n"
            f"📌 Тур бўйича (жами):\n"
            f"  🎬 Instagram Reels: {src['instagram_reels']}\n"
            f"  📸 Instagram пост: {src['instagram_post']}\n"
            f"  👤 Instagram аккаунт: {src['instagram_account']}\n"
            f"  🎥 YouTube Shorts: {src['youtube_shorts']}\n"
            f"  🎤 Овозли хабар: {src['voice']}\n\n"
            f"📌 Тил бўйича (жами):\n"
            f"  🇺🇿 Ўзбек кирилл: {lng['lang_kirill']}\n"
            f"  🔤 Ўзбек лотин: {lng['lang_lotin']}\n"
            f"  🇷🇺 Рус: {lng['lang_rus']}\n\n"
            f"🔄 Fallback (gpt-4o-mini): {s['fallback_used_count']} ta"
            f"{hourly_text}"
        )
    await message.answer(text)

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
            "👤 Instagram аккаунт ҳаволаси\n"
            "🎥 YouTube Shorts ҳаволаси\n"
            "🎤 Овозли хабар\n"
            "/help — батафсил кўрсатма"
        ),
        "lang_lotin": (
            "✅ Til tanlandi: O'zbekcha (Lotin)\n"
            "Quyidagilarni yuborishingiz mumkin:\n"
            "🎬 Instagram Reels havolasi\n"
            "📸 Instagram post havolasi\n"
            "👤 Instagram akkaunt havolasi\n"
            "🎥 YouTube Shorts havolasi\n"
            "🎤 Ovozli xabar\n"
            "/help — batafsil ko'rsatma"
        ),
        "lang_rus": (
            "✅ Язык выбран: Русский\n"
            "Вы можете отправить:\n"
            "🎬 Ссылку на Instagram Reels\n"
            "📸 Ссылку на Instagram пост\n"
            "👤 Ссылку на Instagram аккаунт\n"
            "🎥 Ссылку на YouTube Shorts\n"
            "🎤 Голосовое сообщение\n"
            "/help — подробная инструкция"
        ),
    }
    await callback.message.edit_text(confirmations[lang])
    await callback.answer()

@router.message(F.text.func(lambda text: extract_account_url(text) is not None and extract_post_url(text) is None and extract_reel_url(text) is None))
async def handle_account(message: Message):
    text = message.text or ""
    username = extract_account_url(text)
    lang = user_language.get(message.from_user.id, "lang_kirill")

    if lang == "lang_rus":
        status_msg = await message.answer("⏳ Анализируется аккаунт...")
    elif lang == "lang_lotin":
        status_msg = await message.answer("⏳ Akkount tahlil qilinmoqda...")
    else:
        status_msg = await message.answer("⏳ Аккаунт таҳлил қилиняпти...")

    try:
        if lang == "lang_rus":
            await status_msg.edit_text("⬇️ Посты загружаются...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⬇️ Postlar yuklanmoqda...")
        else:
            await status_msg.edit_text("⬇️ Постлар юкланяпти...")

        posts, biography = await download_account_posts(username)

        if not posts:
            stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "download_error", "posts bo'sh")
            if lang == "lang_rus":
                await status_msg.edit_text("❌ Аккаунт не найден или нет постов.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("❌ Akkount topilmadi yoki postlar yo'q.")
            else:
                await status_msg.edit_text("❌ Аккаунт топилмади ёки постлар йўқ.")
            return

        if lang == "lang_rus":
            await status_msg.edit_text("🔍 Контент анализируется...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🔍 Kontent tahlil qilinmoqda...")
        else:
            await status_msg.edit_text("🔍 Мазмун таҳлил қилиняпти...")

        result = await analyze_account(posts, biography, username, lang)

        if is_moderation_refusal(result):
            logging.warning(f"Moderatsiya rad etishi (handle_account): user_id={message.from_user.id} username={username}")
            stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "moderation", "refusal-heuristic")
            await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
            return

        result_text = f"👤 <b>@{username} аккаунт таҳлили:</b>\n\n{html.escape(result)}"

        if len(result_text) > 4000:
            await status_msg.edit_text(result_text[:4000], parse_mode="HTML")
            await message.answer(result_text[4000:], parse_mode="HTML")
        else:
            await status_msg.edit_text(result_text, parse_mode="HTML")

        stats.log_analysis(message.from_user.id, "instagram_account", lang, "success")

    except asyncio.TimeoutError:
        logging.warning(f"Akkount tahlili timeout: user_id={message.from_user.id} username={username}")
        stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "download_error", "timeout")
        if lang == "lang_rus":
            await status_msg.edit_text("⏳ Instagram слишком долго не отвечает. Попробуйте позже.")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⏳ Instagram javob berishda kechikmoqda. Keyinroq urinib ko'ring.")
        else:
            await status_msg.edit_text("⏳ Instagram жавоб беришда кечикмоқда. Кейинроқ уриниб кўринг.")
    except CookieExpiredError:
        await notify_cookie_expired(message, lang, "instagram_account")
        await status_msg.edit_text(COOKIE_EXPIRED_USER_MESSAGES.get(lang, COOKIE_EXPIRED_USER_MESSAGES["lang_kirill"]))
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_account", message.from_user.id, "instagram_account"):
            return
        logging.error(f"Akkount tahlili xatosi: user_id={message.from_user.id} username={username} error={e}")
        err = str(e).lower()
        if any(k in err for k in ("429", "too many", "rate", "wait a few minutes")):
            stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "instagram_limit", str(e))
            if lang == "lang_rus":
                await status_msg.edit_text("⏳ Instagram временно ограничил запросы. Повторите через 10-15 минут.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("⏳ Instagram vaqtinchalik cheklov qo'ydi. 10-15 daqiqadan so'ng qayta urining.")
            else:
                await status_msg.edit_text("⏳ Instagram вақтинчалик чеклов қўйди. 10-15 дақиқадан сўнг қайта уриниб кўринг.")
        else:
            stats.log_analysis(message.from_user.id, "instagram_account", lang, "error", "other", str(e))
            if lang == "lang_rus":
                await status_msg.edit_text("❌ Произошла ошибка. Аккаунт закрытый или не существует.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("❌ Xatolik yuz berdi. Akkount yopiq yoki mavjud emas.")
            else:
                await status_msg.edit_text("❌ Хато юз берди. Аккаунт ёпиқ ёки мавжуд эмас.")

@router.message(F.text.func(lambda text: extract_post_url(text) is not None))
async def handle_post(message: Message):
    text = message.text or ""
    url = extract_post_url(text)
    lang = user_language.get(message.from_user.id, "lang_kirill")
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

        if images:
            result = await analyze_image_content(images, caption, lang)
        elif caption:
            result = await analyze_caption_only(caption, lang)
        else:
            stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "other", "mazmun topilmadi")
            if lang == "lang_rus":
                await status_msg.edit_text("❌ В посте не найдено содержимого для анализа.")
            elif lang == "lang_lotin":
                await status_msg.edit_text("❌ Postda tahlil qilishga mazmun topilmadi.")
            else:
                await status_msg.edit_text("❌ Постда таҳлил қилишга мазмун топилмади.")
            return

        if is_moderation_refusal(result):
            logging.warning(f"Moderatsiya rad etishi (handle_post): user_id={message.from_user.id} url={url}")
            stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "moderation", "refusal-heuristic")
            await status_msg.edit_text(MODERATION_MESSAGES.get(lang, MODERATION_MESSAGES["lang_kirill"]))
            return

        if len(result) > 4000:
            await status_msg.edit_text(result[:4000])
            await message.answer(result[4000:])
        else:
            await status_msg.edit_text(result)

        stats.log_analysis(message.from_user.id, "instagram_post", lang, "success")
    except asyncio.TimeoutError:
        stats.log_analysis(message.from_user.id, "instagram_post", lang, "error", "download_error", "timeout")
        if lang == "lang_rus":
            await status_msg.edit_text("⏳ Instagram слишком долго не отвечает. Попробуйте позже.")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⏳ Instagram javob berishda kechikmoqda. Keyinroq urinib ko'ring.")
        else:
            await status_msg.edit_text("⏳ Instagram жавоб беришда кечикмоқда. Кейинроқ уриниб кўринг.")
    except CookieExpiredError:
        await notify_cookie_expired(message, lang, "instagram_post")
        await status_msg.edit_text(COOKIE_EXPIRED_USER_MESSAGES.get(lang, COOKIE_EXPIRED_USER_MESSAGES["lang_kirill"]))
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_post", message.from_user.id, "instagram_post"):
            return
        logging.error(f"Post tahlili xatosi: user_id={message.from_user.id} url={url} error={e}")
        err = str(e).lower()
        if any(k in err for k in ("429", "too many", "rate", "wait a few minutes")):
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
async def handle_voice(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")

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
        whisper_lang = "ru" if lang == "lang_rus" else None
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

        result_text = f"🔍 <b>Таҳлил натижаси:</b>\n\n{html.escape(analysis)}"

        if len(result_text) > 4000:
            await status_msg.edit_text(result_text[:4000], parse_mode="HTML")
            await message.answer(result_text[4000:], parse_mode="HTML")
        else:
            await status_msg.edit_text(result_text, parse_mode="HTML")

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

@router.message(F.text.func(lambda text: extract_reel_url(text) is not None))
async def handle_reel(message: Message):
    url = extract_reel_url(message.text)
    lang = user_language.get(message.from_user.id, "lang_kirill")
    logging.info(f"REELS SO'ROV: user_id={message.from_user.id} url={url}")
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

        file_path, reel_caption = await download_reels_audio(url)
        tmp_dir = os.path.dirname(file_path)

        if lang == "lang_rus":
            await status_msg.edit_text("🎙 Аудио преобразуется в текст...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🎙 Audio matnga aylantirilmoqda...")
        else:
            await status_msg.edit_text("🎙 Аудио матнга айлантирилаяпти...")

        try:
            whisper_lang = "ru" if lang == "lang_rus" else None
            text = await loop.run_in_executor(None, transcribe_audio, file_path, whisper_lang)
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
            f"🔍 <b>Таҳлил натижаси:</b>\n\n"
            f"{html.escape(analysis)}"
        )

        if len(result_text) > 4000:
            await status_msg.edit_text(result_text[:4000], parse_mode="HTML")
            await message.answer(result_text[4000:], parse_mode="HTML")
        else:
            await status_msg.edit_text(result_text, parse_mode="HTML")

        stats.log_analysis(message.from_user.id, "instagram_reels", lang, "success", fallback_used=1 if fallback_used else 0)

    except asyncio.TimeoutError:
        stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "download_error", "timeout")
        if lang == "lang_rus":
            await status_msg.edit_text("⏳ Instagram слишком долго не отвечает. Попробуйте позже.")
        elif lang == "lang_lotin":
            await status_msg.edit_text("⏳ Instagram javob berishda kechikmoqda. Keyinroq urinib ko'ring.")
        else:
            await status_msg.edit_text("⏳ Instagram жавоб беришда кечикмоқда. Кейинроқ уриниб кўринг.")
    except CookieExpiredError:
        await notify_cookie_expired(message, lang, "instagram_reels")
        await status_msg.edit_text(COOKIE_EXPIRED_USER_MESSAGES.get(lang, COOKIE_EXPIRED_USER_MESSAGES["lang_kirill"]))
    except Exception as e:
        if await handle_openai_error(status_msg, lang, e, "handle_reel", message.from_user.id, "instagram_reels"):
            return
        logging.error(f"Error handling reel: {e}")
        err = str(e).lower()
        if any(k in err for k in ("rate", "429", "too many", "limit", "ratelimit")):
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
            await status_msg.edit_text(rate_messages.get(lang, rate_messages["lang_kirill"]))
        else:
            stats.log_analysis(message.from_user.id, "instagram_reels", lang, "error", "other", str(e))
            error_messages = {
                "lang_kirill": "❌ Хато юз берди. Илтимос, бошқа Reels ҳаволасини юборинг.",
                "lang_lotin": "❌ Xato yuz berdi. Iltimos, boshqa Reels havolasini yuboring.",
                "lang_rus": "❌ Произошла ошибка. Пожалуйста, отправьте другую ссылку на Reels.",
            }
            await status_msg.edit_text(error_messages.get(lang, error_messages["lang_kirill"]))
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

@router.message(F.text.func(lambda text: extract_youtube_url(text) is not None))
async def handle_youtube(message: Message):
    url = extract_youtube_url(message.text)
    lang = user_language.get(message.from_user.id, "lang_kirill")
    logging.info(f"YOUTUBE SO'ROV: user_id={message.from_user.id} url={url} shorts_path={is_youtube_shorts_path(url)}")
    status_msg = await message.answer(YOUTUBE_STATUS_MESSAGES.get(lang, YOUTUBE_STATUS_MESSAGES["lang_kirill"]))
    file_path = None
    tmp_dir = None
    loop = asyncio.get_running_loop()

    try:
        file_path, video_caption = await download_youtube_shorts(url)
        tmp_dir = os.path.dirname(file_path)

        if lang == "lang_rus":
            await status_msg.edit_text("🎙 Аудио преобразуется в текст...")
        elif lang == "lang_lotin":
            await status_msg.edit_text("🎙 Audio matnga aylantirilmoqda...")
        else:
            await status_msg.edit_text("🎙 Аудио матнга айлантирилаяпти...")

        try:
            whisper_lang = "ru" if lang == "lang_rus" else None
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
            f"🔍 <b>Таҳлил натижаси:</b>\n\n"
            f"{html.escape(analysis)}"
        )

        if len(result_text) > 4000:
            await status_msg.edit_text(result_text[:4000], parse_mode="HTML")
            await message.answer(result_text[4000:], parse_mode="HTML")
        else:
            await status_msg.edit_text(result_text, parse_mode="HTML")

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