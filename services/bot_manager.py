import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError, TelegramNetworkError
from sqlalchemy import select
from db.base import Session
from db.models import ChildBot, BotType, BotRuntimeLock

log = logging.getLogger("bot_manager")

LOCK_STALE_AFTER = timedelta(seconds=15)   # если heartbeat старше — лок считается брошенным
HEARTBEAT_EVERY = 5

TOKEN_WATCHDOG_EVERY = 45  # секунд между проверками валидности токена

CONFLICT_CHECK_EVERY = 30 * 60      # 30 минут
CONFLICT_DISABLE_AFTER = timedelta(minutes=30)


class BotManager:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}   # bot_id -> polling task
        self.bots: dict[int, Bot] = {}
        self.dispatchers: dict[int, Dispatcher] = {}  # bot_id -> Dispatcher object
        self._locks: dict[int, asyncio.Lock] = {}
        self._instance_id = str(uuid.uuid4())
        self._conflict_since: dict[int, datetime] = {}
        self._conflict_watchdog_task: asyncio.Task | None = None

    def _lock(self, bot_id: int) -> asyncio.Lock:
        lock = self._locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[bot_id] = lock
        return lock

    async def start_all(self):
        async with Session() as s:
            rows = (await s.scalars(select(ChildBot).where(ChildBot.is_active))).all()

        # Запускаем ботов небольшими пачками с задержкой, чтобы избежать спама в Telegram API
        STARTUP_BATCH_SIZE = 3
        STARTUP_BATCH_DELAY = 2.0  # сек между пачками
        for i in range(0, len(rows), STARTUP_BATCH_SIZE):
            batch = rows[i:i + STARTUP_BATCH_SIZE]
            await asyncio.gather(*(self.start_bot(cb) for cb in batch), return_exceptions=True)
            if i + STARTUP_BATCH_SIZE < len(rows):
                await asyncio.sleep(STARTUP_BATCH_DELAY)

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
                
                if cb is None or not cb.is_active:
                    log.info("[DEBUG_BOT] Хертбит: Бот %s деактивирован (is_active=False) или удален. Сигнализирую stop_polling()", bot_id)
                    await dp.stop_polling()
        except asyncio.CancelledError:
            pass

    async def _deactivate_invalid_token(self, cb_id: int, username: str):
        log.error("Bot %s (@%s): токен недействителен (TelegramUnauthorizedError) — останавливаю и отключаю.", cb_id, username)
        async with Session() as s:
            cb = await s.get(ChildBot, cb_id)
            if cb and cb.is_active:
                cb.is_active = False
                owner_id = cb.owner_id
                await s.commit()
            else:
                owner_id = None
        if owner_id:
            try:
                from config import MASTER_BOT_TOKEN
                notifier = Bot(MASTER_BOT_TOKEN, session=AiohttpSession(timeout=10.0))
                try:
                    await notifier.send_message(
                        owner_id,
                        f"⚠️ Бот @{username} остановлен: его токен недействителен "
                        "(отозван или отсутствует в Telegram — TelegramUnauthorizedError)."
                    )
                finally:
                    await notifier.session.close()
            except Exception:
                pass

    async def _token_watchdog(self, cb_id: int, bot: Bot, dp: Dispatcher, username: str):
        try:
            while True:
                await asyncio.sleep(TOKEN_WATCHDOG_EVERY)
                try:
                    await bot.get_me()
                except TelegramUnauthorizedError:
                    await self._deactivate_invalid_token(cb_id, username)
                    await dp.stop_polling()
                    return
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def _start_bot_locked(self, cb: ChildBot):
        existing = self.tasks.get(cb.id)
        if existing and not existing.done():
            return
        if existing and existing.done():
            self.tasks.pop(cb.id, None)
            self.bots.pop(cb.id, None)
            self.dispatchers.pop(cb.id, None)

        if not await self._claim_runtime_lock(cb.id):
            log.warning("Bot %s (@%s) уже поднят ДРУГИМ процессом — не запускаю.", cb.id, cb.username)
            return

        from child.feedback import build_feedback_router
        from child.posting import build_posting_router
        from child.survey import build_survey_router
        from child.common import build_common_router

        # Явный таймаут для aiohttp сессии (30 сек)
        session = AiohttpSession(timeout=30.0)
        bot = Bot(cb.token, session=session, default=DefaultBotProperties(parse_mode="HTML"))

        # Preflight-проверка токена
        try:
            await bot.get_me()
        except TelegramUnauthorizedError:
            await self._release_runtime_lock(cb.id)
            await self._deactivate_invalid_token(cb.id, cb.username)
            try:
                await bot.session.close()
            except Exception:
                pass
            return
        except Exception:
            pass  # Сетевую ошибку обработаем внутри воркера

        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass

        dp = Dispatcher()
        dp["bot_db_id"] = cb.id
        dp.include_router(build_common_router())
        if cb.bot_type == BotType.feedback:
            dp.include_router(build_feedback_router())
        elif cb.bot_type == BotType.survey:
            dp.include_router(build_survey_router())
        else:
            dp.include_router(build_posting_router())

        async def _run():
            nonlocal bot
            heartbeat = asyncio.create_task(self._heartbeat_loop(cb.id, dp))
            watchdog = asyncio.create_task(self._token_watchdog(cb.id, bot, dp, cb.username))
            backoff = 5
            try:
                while True:
                    log.info("[DEBUG_BOT] Воркер @%s: Проверяю статус в БД...", cb.username)
                    async with Session() as s:
                        current_cb = await s.get(ChildBot, cb.id)
                    
                    if current_cb is None:
                        log.warning("[DEBUG_BOT] Воркер @%s: Бот удален из БД.", cb.username)
                        return
                    
                    log.info("[DEBUG_BOT] Воркер @%s: is_active = %s", cb.username, current_cb.is_active)
                    if not current_cb.is_active:
                        log.info("[DEBUG_BOT] Воркер @%s: Остановка по флагу is_active=False", cb.username)
                        return

                    try:
                        log.info("[DEBUG_BOT] Воркер @%s: Инициализирую dp.start_polling()", cb.username)
                        await dp.start_polling(
                            bot, handle_signals=False,
                            allowed_updates=dp.resolve_used_update_types()
                        )

                        log.info("[DEBUG_BOT] Воркер @%s: Сессия поллинга завершилась штатно.", cb.username)
                        self._conflict_since.pop(cb.id, None)
                        return
                    except TelegramNetworkError as e:
                        log.warning("[DEBUG_BOT] Воркер @%s: Сетевой сбой/таймаут (%s). Сбрасываю сессию и ретраю через %ss...", cb.username, e, backoff)
                        # Закрываем старую застрявшую сессию
                        try:
                            await bot.session.close()
                        except Exception:
                            pass
                        # Пересоздаем свежую сессию
                        new_session = AiohttpSession(timeout=30.0)
                        bot = Bot(cb.token, session=new_session, default=DefaultBotProperties(parse_mode="HTML"))
                        self.bots[cb.id] = bot

                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30)
                    except TelegramConflictError:
                        log.warning("[DEBUG_BOT] Воркер @%s: Конфликт токенов (TelegramConflictError). Пауза %ss.", cb.username, backoff)
                        self._conflict_since.setdefault(cb.id, datetime.utcnow())
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60)
                    except TelegramUnauthorizedError:
                        log.error("[DEBUG_BOT] Воркер @%s: Токен заблокирован (TelegramUnauthorizedError).", cb.username)
                        await self._deactivate_invalid_token(cb.id, cb.username)
                        return
                    except Exception as e:
                        log.exception("[DEBUG_BOT] Воркер @%s: Исключение в поллинге: %s", cb.username, e)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30)
            except asyncio.CancelledError:
                log.info("[DEBUG_BOT] Воркер @%s: Отмена задачи (CancelledError).", cb.username)
                raise
            except Exception as e:
                log.exception("Bot %s crashed: %s", cb.username, e)
            finally:
                log.info("[DEBUG_BOT] Воркер @%s: Очистка ресурсов воркера...", cb.username)
                heartbeat.cancel()
                watchdog.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass
                await self._release_runtime_lock(cb.id)
                self._conflict_since.pop(cb.id, None)
                try:
                    await bot.session.close()
                    log.info("[DEBUG_BOT] Воркер @%s: Сессия aiohttp закрыта.", cb.username)
                except Exception:
                    pass

        self.bots[cb.id] = bot
        self.dispatchers[cb.id] = dp
        self.tasks[cb.id] = asyncio.create_task(_run(), name=f"bot-{cb.username}")
        log.info("Started child bot @%s (%s)", cb.username, cb.bot_type.value)

    async def stop_bot(self, bot_id: int):
        async with self._lock(bot_id):
            await self._stop_bot_locked(bot_id)

    async def _stop_bot_locked(self, bot_id: int):
        dp = self.dispatchers.pop(bot_id, None)
        task = self.tasks.pop(bot_id, None)
        bot = self.bots.pop(bot_id, None)

        if dp:
            log.info("[DEBUG_BOT] stop_bot: Сигнализирую dp.stop_polling() для бота id=%s", bot_id)
            await dp.stop_polling()
            await asyncio.sleep(0.2)

        if task:
            log.info("[DEBUG_BOT] stop_bot: Вызываю task.cancel() для бота id=%s", bot_id)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except asyncio.TimeoutError:
                log.warning("Bot %s: остановка зависла дольше 10с, продолжаю...", bot_id)
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
        if self._conflict_watchdog_task:
            self._conflict_watchdog_task.cancel()
            try:
                await self._conflict_watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._conflict_watchdog_task = None
        ids = list(self.tasks.keys())
        await asyncio.gather(*(self.stop_bot(i) for i in ids), return_exceptions=True)

    def start_conflict_watchdog(self):
        if self._conflict_watchdog_task is None or self._conflict_watchdog_task.done():
            self._conflict_watchdog_task = asyncio.create_task(
                self._conflict_watchdog_loop(), name="conflict-watchdog")

    async def _conflict_watchdog_loop(self):
        try:
            while True:
                await asyncio.sleep(CONFLICT_CHECK_EVERY)
                await self._check_stuck_conflicts()
        except asyncio.CancelledError:
            pass

    async def _check_stuck_conflicts(self):
        now = datetime.utcnow()
        stuck = [bot_id for bot_id, since in list(self._conflict_since.items())
                 if now - since >= CONFLICT_DISABLE_AFTER]
        for bot_id in stuck:
            await self._deactivate_conflicting_bot(bot_id)

    async def _deactivate_conflicting_bot(self, bot_id: int):
        async with Session() as s:
            cb = await s.get(ChildBot, bot_id)
            if not cb:
                self._conflict_since.pop(bot_id, None)
                return
            username = cb.username
            owner_id = cb.owner_id
            still_active = cb.is_active
            if still_active:
                cb.is_active = False
                await s.commit()
        self._conflict_since.pop(bot_id, None)
        await self.stop_bot(bot_id)
        if not still_active:
            return
        log.error("Bot %s (@%s): постоянный TelegramConflictError — отключаю.", bot_id, username)
        if owner_id:
            try:
                from config import MASTER_BOT_TOKEN
                notifier = Bot(MASTER_BOT_TOKEN, session=AiohttpSession(timeout=10.0))
                try:
                    await notifier.send_message(
                        owner_id,
                        f"⚠️ Бот @{username} остановлен: постоянный конфликт (TelegramConflictError). "
                        "Токен используется в другом месте."
                    )
                finally:
                    await notifier.session.close()
            except Exception:
                pass


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


_REUPLOAD_FILENAMES = {
    "photo": "photo.jpg", "video": "video.mp4", "animation": "animation.gif",
    "document": "file", "audio": "audio.mp3", "voice": "voice.ogg",
    "video_note": "note.mp4", "sticker": "sticker.webp",
}


async def reupload_media_for_bot(source_bot: Bot, bot_id: int, file_id: str,
                                 media_type: str, target_chat_id: int) -> str | None:
    child_bot = manager.bots.get(bot_id)
    if not child_bot:
        return None
    try:
        from aiogram.types import BufferedInputFile
        tg_file = await source_bot.get_file(file_id)
        buf = await source_bot.download_file(tg_file.file_path)
        input_file = BufferedInputFile(buf.read(), filename=_REUPLOAD_FILENAMES.get(media_type, "file"))
        send_fn = {
            "photo": child_bot.send_photo, "video": child_bot.send_video,
            "animation": child_bot.send_animation, "document": child_bot.send_document,
            "audio": child_bot.send_audio, "voice": child_bot.send_voice,
            "video_note": child_bot.send_video_note, "sticker": child_bot.send_sticker,
        }.get(media_type)
        if send_fn is None:
            return None
        sent = await send_fn(target_chat_id, input_file)
        try:
            await child_bot.delete_message(target_chat_id, sent.message_id)
        except Exception:
            pass
        obj = getattr(sent, media_type, None)
        if media_type == "photo" and obj:
            return obj[-1].file_id
        return obj.file_id if obj else None
    except Exception as e:
        log.warning("reupload_media_for_bot: не удалось перенести file_id (%s) для бота %s: %s",
                    media_type, bot_id, e)
        return None
