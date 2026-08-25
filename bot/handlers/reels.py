from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
import asyncio
import html
import os
import re
import shutil
import logging
from urllib.parse import urlparse
from bot.utils.downloader import download_reels_audio, download_instagram_image
from bot.utils.stt import transcribe_audio, UnclearAudioError
from bot.utils.analyzer import analyze_content, analyze_image_content, analyze_caption_only

router = Router()

user_language: dict[int, str] = {}

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
    pattern = r'https?://(?:www\.)?instagram\.com/p/[A-Za-z0-9_-]+/?'
    match = re.search(pattern, text)
    return match.group(0) if match else None

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
        "🎙 Telegram ovozli xabar / овозли хабар\n\n"
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
            "• Отправьте голосовое — бот проанализирует\n\n"
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
            "• Ovozli xabar yuboring — bot tahlil qiladi\n\n"
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
            "• Овозли хабар юборинг — бот таҳлил қилади\n\n"
            "⚙️ Буйруқлар:\n"
            "/start — қайтадан бошлаш\n"
            "/help — ушбу ёрдам"
        )
    await message.answer(text, parse_mode="HTML")

@router.callback_query(F.data.in_({"lang_kirill", "lang_lotin", "lang_rus"}))
async def set_language(callback: CallbackQuery):
    lang = callback.data
    user_language[callback.from_user.id] = lang

    confirmations = {
        "lang_kirill": "✅ Тил танланди: Ўзбекча (Кирилл)\nИнди Instagram Reels ҳаволасини юборинг.",
        "lang_lotin":  "✅ Til tanlandi: O'zbekcha (Lotin)\nEndi Instagram Reels havolasini yuboring.",
        "lang_rus":    "✅ Язык выбран: Русский\nТеперь отправьте ссылку на Instagram Reels.",
    }
    await callback.message.edit_text(confirmations[lang])
    await callback.answer()

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
            await status_msg.edit_text("❌ Постда таҳлил қилишга мазмун топилмади.")
            return
        if len(result) > 4000:
            await status_msg.edit_text(result[:4000])
            await message.answer(result[4000:])
        else:
            await status_msg.edit_text(result)
    except Exception as e:
        if lang == "lang_rus":
            await status_msg.edit_text("❌ Произошла ошибка. Попробуйте другую ссылку.")
        elif lang == "lang_lotin":
            await status_msg.edit_text("❌ Xatolik yuz berdi. Iltimos, boshqa havola yuboring.")
        else:
            await status_msg.edit_text("❌ Хато юз берди. Илтимос, бошқа ҳавола юборинг.")
    finally:
        import shutil, os
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

        import tempfile, os, shutil
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

        import subprocess
        subprocess.run(
            ["ffmpeg", "-i", ogg_path, "-ar", "16000", "-ac", "1", mp3_path],
            capture_output=True
        )

        # Whisper orqali transkripsiya
        from bot.utils.stt import transcribe_audio
        import asyncio
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, transcribe_audio, mp3_path)

        if not text or not text.strip():
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
        analysis = await loop.run_in_executor(None, analyze_content, text, lang)

        import html
        result_text = f"🔍 <b>Таҳлил натижаси:</b>\n\n{html.escape(analysis)}"

        if len(result_text) > 4000:
            await status_msg.edit_text(result_text[:4000], parse_mode="HTML")
            await message.answer(result_text[4000:], parse_mode="HTML")
        else:
            await status_msg.edit_text(result_text, parse_mode="HTML")

    except UnclearAudioError:
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
    except Exception as e:
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
    status_msg = await message.answer("⏳ Video yuklab olinmoqda...")
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
            text = await loop.run_in_executor(None, transcribe_audio, file_path)
        except UnclearAudioError as e:
            logging.warning(f"Aniq bo'lmagan audio (user_id={message.from_user.id}, url={url}): {e}")
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

        analysis = await loop.run_in_executor(None, analyze_content, text, lang)

        result_text = (
            f"🔍 <b>Таҳлил натижаси:</b>\n\n"
            f"{html.escape(analysis)}"
        )

        if len(result_text) > 4000:
            await status_msg.edit_text(result_text[:4000], parse_mode="HTML")
            await message.answer(result_text[4000:], parse_mode="HTML")
        else:
            await status_msg.edit_text(result_text, parse_mode="HTML")

    except Exception as e:
        logging.error(f"Error handling reel: {e}")
        err = str(e).lower()
        if any(k in err for k in ("rate", "429", "too many", "limit", "ratelimit")):
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
            error_messages = {
                "lang_kirill": "❌ Хато юз берди. Илтимос, бошқа Reels ҳаволасини юборинг.",
                "lang_lotin": "❌ Xato yuz berdi. Iltimos, boshqa Reels havolasini yuboring.",
                "lang_rus": "❌ Произошла ошибка. Пожалуйста, отправьте другую ссылку на Reels.",
            }
            await status_msg.edit_text(error_messages.get(lang, error_messages["lang_kirill"]))
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)

@router.message()
async def echo_all(message: Message):
    lang = user_language.get(message.from_user.id, "lang_kirill")
    if lang == "lang_rus":
        text = "📎 Отправьте ссылку на Instagram Reels или пост.\n\nℹ️ /help — справка"
    elif lang == "lang_lotin":
        text = "📎 Instagram Reels yoki post havolasini yuboring.\n\nℹ️ /help — yordam"
    else:
        text = "📎 Instagram Reels ёки пост ҳаволасини юборинг.\n\nℹ️ /help — ёрдам"
    await message.answer(text)