import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from bot.config import settings
from bot.handlers import reels

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

async def main():
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()

    dp.include_router(reels.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Ботни бошлаш / Начать"),
        BotCommand(command="help", description="Ёрдам / Помощь"),
        BotCommand(command="stats", description="Статистика"),
    ])

    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped")
    except Exception as e:
        print(f"Error: {e}")
