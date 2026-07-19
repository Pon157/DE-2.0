import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from sqlalchemy import select
from db.base import Session
from db.models import ChildBot, BotType, BotRuntimeLock

log = logging.getLogger("bot_manager")

LOCK_STALE_AFTER = timedelta(seconds=15)   # если heartbeat старше — лок считается брошенным
HEARTBEAT_EVERY = 5


class BotManager:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}   # bot_id -> polling task
        self.bots: dict[int, Bot] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._instance_id = str(uuid.uuid4())

    def _lock(self, bot_id: int) -> asyncio.Lock:
        lock = self._locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[bot_id] = lock
        return lock

    async def start_all(self):
        async with Session() as s:
            rows = (await s.scalars(select(ChildBot).where(ChildBot.is_active))).all()
        for cb in rows:
            await self.start_bot(cb)

    async def start_bot(self, cb: ChildBot):
        async with self._lock(cb.id):
            await self._start_bot_locked(cb)

    async def _claim_runtime_lock(self, bot_id: int) -> bool:
        now = datetime.utcnow()
        async with Session() as s:
            row = await s.get(BotRuntimeLock, bot_id)
            if row is None:
                s.add(BotRuntimeLock(bot_id=bot_id, holder=self._instance_id, last_seen=now))
                await s.commit()
                return True
            if row.holder == self._instance_id or (now - row.last_seen) > LOCK_STALE_AFTER:
                row.holder = self._instance_id
                row.last_seen = now
                await s.commit()
                return True
            return False

    async def _release_runtime_lock(self, bot_id: int):
        async with Session() as s:
            row = await s.get(BotRuntimeLock, bot_id)
            if row and row.holder == self._instance_id:
                await s.delete(row)
                await s.commit()

    async def _heartbeat_loop(self, bot_id: int, dp: Dispatcher):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_EVERY)
                async with Session() as s:
                    row = await s.get(BotRuntimeLock, bot_id)
                    if row and row.holder == self._instance_id:
                        row.last_seen = datetime.utcnow()
                        await s.commit()
                    cb = await s.get(ChildBot, bot_id)
                
                # ДЕБАГ: Логируем реакцию фонового хертбита на изменение записи в БД
                if cb is None or not cb.is_active:
                    log.info("[DEBUG_BOT] Хертбит: Бот %s деактивирован (is_active=False) или удален. Сигнализирую stop_polling()", bot_id)
                    await dp.stop_polling()
        except asyncio.CancelledError:
            pass

    async def _start_bot_locked(self, cb: ChildBot):
        existing = self.tasks.get(cb.id)
        if existing and not existing.done():
            return
        if existing and existing.done():
            self.tasks.pop(cb.id, None)
            self.bots.pop(cb.id, None)

        if not await self._claim_runtime_lock(cb.id):
            log.warning(
                "Bot %s (@%s) уже поднят ДРУГИМ процессом (по метке в БД) — "
                "не запускаю второй поллинг.", cb.id, cb.username)
            return

        from child.feedback import build_feedback_router
        from child.posting import build_posting_router
        from child.common import build_common_router

        bot = Bot(cb.token, default=DefaultBotProperties(parse_mode="HTML"))

        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass

        dp = Dispatcher()
        dp["bot_db_id"] = cb.id
        dp.include_router(build_common_router())
        if cb.bot_type == BotType.feedback:
            dp.include_router(build_feedback_router())
        else:
            dp.include_router(build_posting_router())

        async def _run():
            heartbeat = asyncio.create_task(self._heartbeat_loop(cb.id, dp))
            backoff = 5
            try:
                while True:
                    # ДЕБАГ: Логируем факт новой итерации цикла и запрос к БД
                    log.info("[DEBUG_BOT] Воркер @%s: Проверяю актуальный статус в БД перед стартом сессии...", cb.username)
                    async with Session() as s:
                        current_cb = await s.get(ChildBot, cb.id)
                    
                    if current_cb is None:
                        log.warning("[DEBUG_BOT] Воркер @%s: Бот полностью удален из БД. Завершаю поток воркера.", cb.username)
                        return
                    
                    log.info("[DEBUG_BOT] Воркер @%s: Ответ БД получен. Текущий флаг is_active = %s", cb.username, current_cb.is_active)
                    
                    if not current_cb.is_active:
                        log.info("[DEBUG_BOT] Воркер @%s: Обнаружен флаг остановки (is_active=False). Мягко выхожу из цикла.", cb.username)
                        return

                    try:
                        log.info("[DEBUG_BOT] Воркер @%s: Инициализирую вызов dp.start_polling()", cb.username)
                        await dp.start_polling(
                            bot, handle_signals=False,
                            allowed_updates=dp.resolve_used_update_types())
                        
                        log.info("[DEBUG_BOT] Воркер @%s: Сессия dp.start_polling() завершилась штатно.", cb.username)
                        return  # штатная остановка (stop_polling / отмена)
                    except TelegramConflictError:
                        log.warning("[DEBUG_BOT] Воркер @%s: Конфликт токенов (TelegramConflictError). Засыпаю на %ss перед ретраем.",
                                    cb.username, backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60)
                    except TelegramUnauthorizedError:
                        log.error("[DEBUG_BOT] Воркер @%s: Токен заблокирован ТГ (TelegramUnauthorizedError). Выхожу.", cb.username)
                        return
                    except Exception as e:
                        log.exception("[DEBUG_BOT] Воркер @%s: Поймано исключение внутри старта поллинга: %s", cb.username, e)
                        await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                log.info("[DEBUG_BOT] Воркер @%s: Получена жесткая отмена задачи (CancelledError).", cb.username)
                raise
            except Exception as e:
                log.exception("Bot %s crashed: %s", cb.username, e)
            finally:
                log.info("[DEBUG_BOT] Воркер @%s: Вхожу в блок finally очистки ресурсов воркера.", cb.username)
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                await self._release_runtime_lock(cb.id)
                try:
                    await bot.session.close()
                    log.info("[DEBUG_BOT] Воркер @%s: Сессия aiohttp закрыта успешно.", cb.username)
                except Exception:
                    pass

        self.bots[cb.id] = bot
        self.tasks[cb.id] = asyncio.create_task(_run(), name=f"bot-{cb.username}")
        log.info("Started child bot @%s (%s)", cb.username, cb.bot_type.value)

    async def stop_bot(self, bot_id: int):
        async with self._lock(bot_id):
            await self._stop_bot_locked(bot_id)

    async def _stop_bot_locked(self, bot_id: int):
        task = self.tasks.pop(bot_id, None)
        bot = self.bots.pop(bot_id, None)
        if task:
            log.info("[DEBUG_BOT] stop_bot: Вызываю явный cancel() для таски бота id=%s", bot_id)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except asyncio.TimeoutError:
                log.warning("Bot %s: остановка зависла дольше 10с, продолжаю без ожидания", bot_id)
            except (asyncio.CancelledError, Exception):
                pass
        if bot:
            try:
                await bot.session.close()
            except Exception:
                pass
        await self._release_runtime_lock(bot_id)

    async def restart_bot(self, bot_id: int):
        async with self._lock(bot_id):
            await self._stop_bot_locked(bot_id)
            async with Session() as s:
                cb = await s.get(ChildBot, bot_id)
            if cb and cb.is_active:
                await self._start_bot_locked(cb)

    async def stop_all(self):
        ids = list(self.tasks.keys())
        await asyncio.gather(*(self.stop_bot(i) for i in ids), return_exceptions=True)


manager = BotManager()


async def reupload_photo_for_bot(source_bot: Bot, bot_id: int, file_id: str,
                                 target_chat_id: int) -> str | None:
    child_bot = manager.bots.get(bot_id)
    if not child_bot:
        return None
    try:
        from aiogram.types import BufferedInputFile
        tg_file = await source_bot.get_file(file_id)
        buf = await source_bot.download_file(tg_file.file_path)
        input_file = BufferedInputFile(buf.read(), filename="photo.jpg")
        sent = await child_bot.send_photo(target_chat_id, input_file)
        try:
            await child_bot.delete_message(target_chat_id, sent.message_id)
        except Exception:
            pass
        return sent.photo[-1].file_id
    except Exception as e:
        log.warning("reupload_photo_for_bot: не удалось перенести file_id для бота %s: %s",
                    bot_id, e)
        return None
