from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
import os
import logging
from bot.utils.downloader import download_reels_audio
from bot.utils.stt import transcribe_audio
from bot.utils.analyzer import analyze_content

router = Router()

user_language: dict[int, str] = {}

def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇿 Ўзбекча (Кирилл)", callback_data="lang_kirill")],
        [InlineKeyboardButton(text="🇺🇿 O'zbekcha (Lotin)",  callback_data="lang_lotin")],
        [InlineKeyboardButton(text="🇷🇺 Русский",            callback_data="lang_rus")],
    ])

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Tilni tanlang / Выберите язык / Тилни танланг:",
        reply_markup=lang_keyboard()
    )

@router.callback_query(F.data.in_({"lang_kirill", "lang_lotin", "lang_rus"}))
async def set_language(callback: CallbackQuery):
    lang = callback.data
    user_language[callback.from_user.id] = lang

    confirmations = {
        "lang_kirill": "✅ Тил танланди: Ўзбекча (Кирилл)\nИнди Instagram Reel ҳаволасини юборинг.",
        "lang_lotin":  "✅ Til tanlandi: O'zbekcha (Lotin)\nEndi Instagram Reel havolasini yuboring.",
        "lang_rus":    "✅ Язык выбран: Русский\nТеперь отправьте ссылку на Instagram Reel.",
    }
    await callback.message.edit_text(confirmations[lang])
    await callback.answer()

@router.message(F.text.contains("instagram.com/reels/") | F.text.contains("instagram.com/reel/"))
async def handle_reel(message: Message):
    url = message.text.strip()
    lang = user_language.get(message.from_user.id, "lang_kirill")
    status_msg = await message.answer("⏳ Video yuklab olinmoqda...")
    file_path = None

    try:
        file_path = await download_reels_audio(url)
        await status_msg.edit_text("🎙 Ovoz tekstga aylantirilmoqda...")

        text = transcribe_audio(file_path)

        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            file_path = None

        await status_msg.edit_text("🔍 Tahlil qilinmoqda...")

        analysis = analyze_content(text, lang=lang)

        if len(analysis) > 4000:
            await status_msg.edit_text(analysis[:4000])
            await message.answer(analysis[4000:])
        else:
            await status_msg.edit_text(analysis)

    except Exception as e:
        logging.error(f"Error handling reel: {e}")
        await status_msg.edit_text(f"❌ Xatolik: {str(e)}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

@router.message()
async def echo_all(message: Message):
    await message.answer("📎 Iltimos, Instagram Reel havolasini yuboring.")
