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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

async def run_master_polling(dp: Dispatcher, master: Bot):
    """Отдельный устойчивый цикл поллинга для мастер-бота"""
    backoff = 5
    while True:
        try:
            log.info("Запуск polling для Master Bot...")
            await dp.start_polling(master, handle_signals=False, allowed_updates=dp.resolve_used_update_types())
            break  # Штатный останов
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

async def main():
    await init_db()

    master = Bot(MASTER_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(master_router)

    webhook_runner = await run_webhook_server(AD_WEBHOOK_PORT)
    scheduler_task = asyncio.create_task(run_scheduler())
    
    # 1. СНАЧАЛА запускаем Мастер-бот в фоновом режиме
    master_task = asyncio.create_task(run_master_polling(dp, master))
    
    # 2. ЗАТЕМ фоном запускаем дочерних ботов (без await, чтобы не блокировать main)
    asyncio.create_task(manager.start_all())
    manager.start_conflict_watchdog()

    logging.info("Dialogue Engine fully initialized and running")

    try:
        # Держим main() живым, пока работает главный бот
        await master_task
    finally:
        logging.info("Shutting down, stopping all child bots...")
        scheduler_task.cancel()
        await manager.stop_all()
        await webhook_runner.cleanup()
        await master.session.close()

if __name__ == "__main__":
    asyncio.run(main())
