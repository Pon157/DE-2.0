import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from config import MASTER_BOT_TOKEN, AD_WEBHOOK_PORT
from db.base import init_db
from master.router import router as master_router
from services.bot_manager import manager
from services.ad_webhook import run_webhook_server
from services.scheduler import run_scheduler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def main():
    await init_db()

    master = Bot(MASTER_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(master_router)

    await manager.start_all()           # поднять все дочерние боты
    webhook_runner = await run_webhook_server(AD_WEBHOOK_PORT)
    scheduler_task = asyncio.create_task(run_scheduler())
    logging.info("Dialogue Engine started")
    try:
        # ВАЖНО: раньше здесь был единственный `await dp.start_polling(...)` —
        # любое необработанное исключение в обработке главного бота (сетевой
        # сбой, ошибка БД и т.п.) вылетало из main() и валило ВЕСЬ процесс,
        # А ВМЕСТЕ С НИМ — все дочерние боты, ведь они просто asyncio-задачи
        # внутри этого же процесса. Это и есть баг "менеджер падает вместе с
        # другими ботами". Теперь поллинг мастер-бота обёрнут в тот же
        # устойчивый retry-цикл, что и у дочерних ботов: единичный сбой
        # логируется и приводит к повторной попытке, а не к смерти всего.
        backoff = 5
        while True:
            try:
                await dp.start_polling(master, handle_signals=True,
                                       allowed_updates=dp.resolve_used_update_types())
                break  # штатная остановка (SIGTERM/SIGINT)
            except TelegramConflictError:
                log.warning("Master bot conflict, retry in %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except TelegramUnauthorizedError:
                log.error("Master bot token revoked — остановка.")
                break
            except Exception:
                log.exception("Master bot polling crashed, restarting in %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        # Важно для бага с TelegramConflictError после рестарта: если процесс
        # убит без закрытия getUpdates-сессий дочерних ботов, Telegram ещё
        # какое-то время считает их "занятыми" и новый инстанс получает Conflict.
        # Поэтому при любом штатном завершении (SIGTERM/SIGINT/ошибка) явно
        # останавливаем всех дочерних ботов и закрываем их сессии.
        logging.info("Shutting down, stopping all child bots...")
        scheduler_task.cancel()
        await manager.stop_all()
        await webhook_runner.cleanup()
        await master.session.close()


if __name__ == "__main__":
    asyncio.run(main())
