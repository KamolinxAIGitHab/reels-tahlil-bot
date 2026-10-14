import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat
from bot.config import settings
from bot.handlers import reels
from bot.utils import stats

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(message)s",
)

async def cost_alert_loop(bot: Bot):
    """Навбатдаги харажат огоҳлантиришларини ҳар 30 сонияда маъмурга юборади.
    Юбориб бўлмаса, кейинги айланишда қайта уринади."""
    while True:
        await asyncio.sleep(30)
        try:
            for key, text in await asyncio.to_thread(stats.pending_alerts):
                try:
                    await bot.send_message(reels.ADMIN_USER_ID, text)
                    await asyncio.to_thread(stats.mark_alert_sent, key)
                except Exception as e:
                    logging.warning(f"Харажат огоҳлантиришини юбориб бўлмади ({key}): {e}")
        except Exception as e:
            logging.warning(f"Харажат огоҳлантириш сикли хатоси: {e}")


async def main():
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()

    dp.include_router(reels.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Ботни бошлаш / Начать"),
        BotCommand(command="help", description="Ёрдам / Помощь"),
        BotCommand(command="stats", description="Статистика"),
    ])

    # /xarajat фақат маъмурнинг менюсида кўринади (бошқа фойдаланувчиларда йўқ)
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Ботни бошлаш / Начать"),
                BotCommand(command="help", description="Ёрдам / Помощь"),
                BotCommand(command="stats", description="Статистика"),
                BotCommand(command="xarajat", description="OpenAI харажати (маъмур)"),
            ],
            scope=BotCommandScopeChat(chat_id=reels.ADMIN_USER_ID),
        )
    except Exception as e:
        logging.warning(f"Маъмур буйруқлар менюсини ўрнатиб бўлмади: {e}")

    alert_task = asyncio.create_task(cost_alert_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        alert_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped")
    except Exception as e:
        print(f"Error: {e}")
