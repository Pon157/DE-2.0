import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramUnauthorizedError,
    TelegramNetworkError,
)

from config import MASTER_BOT_TOKEN, AD_WEBHOOK_PORT
from db.base import init_db
from master.router import router as master_router
from services.bot_manager import manager
from services.ad_webhook import run_webhook_server
from services.scheduler import run_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("main")


async def setup_master_bot(bot: Bot) -> None:
    """Вызывается один раз при старте — настраивает мастер-бота:
    регистрирует команды и разрешает домен Mini App через setMyCommands.

    Домен для web_app InlineKeyboardButton регистрировать НЕ нужно —
    Telegram принимает любой HTTPS-домен напрямую. initData подписывается
    токеном бота который выдаёт кнопку, поэтому валидация на бэкенде
    будет работать корректно без дополнительных шагов.
    """
    from aiogram.types import BotCommand, BotCommandScopeDefault
    from config import FLOW_MINIAPP_URL
    try:
        await bot.set_my_commands([
            BotCommand(command="start",     description="Главное меню"),
            BotCommand(command="my_bots",   description="Мои боты"),
            BotCommand(command="pro",       description="Pro-подписка"),
            BotCommand(command="help",      description="Помощь"),
        ], scope=BotCommandScopeDefault())
        log.info("Команды мастер-бота установлены")
    except Exception as e:
        log.warning("Не удалось установить команды мастер-бота: %s", e)

    # Проверяем доступность Mini App домена (не блокируем запуск если недоступен)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.head(FLOW_MINIAPP_URL, timeout=aiohttp.ClientTimeout(total=5)) as r:
                log.info("Mini App домен %s доступен (HTTP %s)", FLOW_MINIAPP_URL, r.status)
    except Exception as e:
        log.warning("Mini App домен недоступен при старте (%s) — "
                    "убедитесь что flow-api и фронтенд запущены", e)


async def run_master_polling(dp: Dispatcher):
    """Отдельный устойчивый цикл поллинга для мастер-бота со сбросом сессии"""
    backoff = 5
    while True:
        # Передаем обычное число (30.0), чтобы aiogram мог корректно
        # вычислять request_timeout = bot.session.timeout + polling_timeout
        session = AiohttpSession(timeout=30.0)
        master = Bot(
            MASTER_BOT_TOKEN,
            session=session,
            default=DefaultBotProperties(parse_mode="HTML")
        )

        try:
            log.info("Запуск polling для Master Bot...")
            # Настраиваем бота один раз перед стартом поллинга
            await setup_master_bot(master)
            await dp.start_polling(
                master,
                handle_signals=False,
                allowed_updates=dp.resolve_used_update_types()
            )
            backoff = 5  # Сбрасываем backoff при успешном завершении
            break  # Штатный останов
        except TelegramNetworkError as e:
            log.warning(
                "Master bot сетевой сбой (%s), пересоздаю сессию и перезапускаю через %ss...",
                e, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except TelegramConflictError:
            log.warning("Master bot conflict, retry in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except TelegramUnauthorizedError:
            log.error("Master bot token revoked — остановка.")
            break
        except Exception as e:
            log.exception(
                "Master bot polling crashed (%s), restarting in %ss", e, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            try:
                await master.session.close()
            except Exception:
                pass


async def main():
    await init_db()

    dp = Dispatcher()
    dp.include_router(master_router)

    webhook_runner = await run_webhook_server(AD_WEBHOOK_PORT)
    scheduler_task = asyncio.create_task(run_scheduler())

    # 1. СНАЧАЛА запускаем Мастер-бот в фоновом режиме
    master_task = asyncio.create_task(run_master_polling(dp))

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


if __name__ == "__main__":
    asyncio.run(main())
