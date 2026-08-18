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


TOKEN_WATCHDOG_EVERY = 45  # секунд между проверками валидности токена

# ---- отслеживание "залипших" TelegramConflictError (по запросу) ----
# Один TelegramConflictError сам по себе — норма (например, короткое
# перекрытие при рестарте деплоя) и обрабатывается ретраем с backoff внутри
# _run(). Проблема — когда бот УСТОЙЧИВО не может подняться (например,
# токен используется где-то ещё — второй инстанс/забытый локальный запуск
# у владельца бота) и вечно ретраит getUpdates, тратя ресурсы и никогда не
# доставляя сообщения. Раз в CONFLICT_CHECK_EVERY проверяем все боты,
# которые непрерывно (без единого успешного старта) находятся в конфликте
# дольше CONFLICT_DISABLE_AFTER, и отключаем их.
CONFLICT_CHECK_EVERY = 30 * 60      # 30 минут
CONFLICT_DISABLE_AFTER = timedelta(minutes=30)


class BotManager:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}   # bot_id -> polling task
        self.bots: dict[int, Bot] = {}
        self.dispatchers: dict[int, Dispatcher] = {}  # bot_id -> Dispatcher object
        self._locks: dict[int, asyncio.Lock] = {}
        self._instance_id = str(uuid.uuid4())
        # bot_id -> момент, с которого бот НЕПРЕРЫВНО в состоянии конфликта
        # (сбрасывается при любом успешном старте поллинга или остановке).
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
        # Запускаем ботов пачками по STARTUP_BATCH_SIZE с паузой между пачками.
        # Без этого при большом числе ботов все разом бьются в Telegram API
        # с get_me()/delete_webhook() → получаем шквал 502 Bad Gateway,
        # которые роняют start_polling() ещё до первого getUpdates.
        STARTUP_BATCH_SIZE = 5
        STARTUP_BATCH_DELAY = 1.5  # сек между пачками
        for i in range(0, len(rows), STARTUP_BATCH_SIZE):
            batch = rows[i:i + STARTUP_BATCH_SIZE]
            await asyncio.gather(*(self.start_bot(cb) for cb in batch),
                                 return_exceptions=True)
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
                
                # ДЕБАГ: Логируем реакцию фонового хертбита на изменение записи в БД
                if cb is None or not cb.is_active:
                    log.info("[DEBUG_BOT] Хертбит: Бот %s деактивирован (is_active=False) или удален. Сигнализирую stop_polling()", bot_id)
                    await dp.stop_polling()
        except asyncio.CancelledError:
            pass

    async def _deactivate_invalid_token(self, cb_id: int, username: str):
        """БАГ/ресурсная проблема (по запросу): если токен дочернего бота
        отозван/невалиден (TelegramUnauthorizedError), aiogram'овский
        dp.start_polling() НЕ пробрасывает это исключение наружу — его
        внутренний бесконечный цикл _listen_updates() ловит ЛЮБОЕ
        исключение (это его штатное поведение "so you may not worry that
        the polling will stop working") и просто ретраит get_updates раз в
        1-5 секунд НАВСЕГДА. Отсюда в логах тысячи строк "Sleep for N
        seconds and try again... (tryings = 57199...)" и постоянная
        нагрузка на CPU/сеть от бота, который заведомо не может работать.
        Наш собственный except TelegramUnauthorizedError вокруг
        dp.start_polling() в _run() из-за этого никогда не срабатывал.
        Решение — активно проверять токен (см. _token_watchdog и
        preflight-проверку в _start_bot_locked) и при невалидности
        ОСТАНАВЛИВАТЬ поллинг самим, а не полагаться на то, что aiogram
        когда-нибудь перестанет ретраить (он не перестанет)."""
        log.error("Bot %s (@%s): токен недействителен (TelegramUnauthorizedError) — "
                  "останавливаю бота и отключаю (is_active=False), чтобы не "
                  "жечь ресурсы на бесконечный ретрай.", cb_id, username)
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
                notifier = Bot(MASTER_BOT_TOKEN)
                try:
                    await notifier.send_message(
                        owner_id,
                        f"⚠️ Бот @{username} остановлен: его токен недействителен "
                        "(отозван или отсутствует в Telegram — TelegramUnauthorizedError). "
                        "Скорее всего, токен был отозван через @BotFather. "
                        "Создайте нового бота или обновите токен и включите бота заново.")
                finally:
                    await notifier.session.close()
            except Exception:
                pass

    async def _token_watchdog(self, cb_id: int, bot: Bot, dp: Dispatcher, username: str):
        """Периодически (раз в TOKEN_WATCHDOG_EVERY сек) проверяет, что токен
        всё ещё валиден (bot.get_me()) — см. докстринг
        _deactivate_invalid_token про то, почему это нельзя переложить на
        встроенный ретрай aiogram. При TelegramUnauthorizedError сразу же
        останавливает поллинг этого бота вместо бесконечных ретраев."""
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
                    # Сетевые/временные ошибки — не повод останавливать бота,
                    # это дело самого get_updates внутри aiogram.
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
            log.warning(
                "Bot %s (@%s) уже поднят ДРУГИМ процессом (по метке в БД) — "
                "не запускаю второй поллинг.", cb.id, cb.username)
            return

        from child.feedback import build_feedback_router
        from child.posting import build_posting_router
        from child.survey import build_survey_router
        from child.common import build_common_router

        bot = Bot(cb.token, default=DefaultBotProperties(parse_mode="HTML"))

        # Preflight-проверка (по запросу): если токен уже невалиден на
        # момент запуска — не поднимаем поллинг вообще (см. докстринг
        # _deactivate_invalid_token — иначе он бы молча ушёл в бесконечный
        # ретрай внутри aiogram и жёг ресурсы).
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
            pass  # временная сетевая ошибка — не блокируем запуск из-за неё

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
            heartbeat = asyncio.create_task(self._heartbeat_loop(cb.id, dp))
            watchdog = asyncio.create_task(self._token_watchdog(cb.id, bot, dp, cb.username))
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
                        self._conflict_since.pop(cb.id, None)
                        return  # штатная остановка (stop_polling / отмена)
                    except TelegramConflictError:
                        log.warning("[DEBUG_BOT] Воркер @%s: Конфликт токенов (TelegramConflictError). Засыпаю на %ss перед ретраем.",
                                    cb.username, backoff)
                        # Помечаем начало непрерывной серии конфликтов (если
                        # ещё не помечено) — см. _conflict_watchdog_loop,
                        # который раз в 30 минут отключит бота, если тот так
                        # и не поднимется до этого момента.
                        self._conflict_since.setdefault(cb.id, datetime.utcnow())
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60)
                    except TelegramUnauthorizedError:
                        # На практике сюда aiogram обычно НЕ доходит (см.
                        # докстринг _deactivate_invalid_token — сам
                        # start_polling эту ошибку проглатывает и ретраит
                        # бесконечно) — оставлено как доп. защита на случай
                        # если исключение всё же прилетит сюда напрямую
                        # (например, из delete_webhook() чуть выше).
                        log.error("[DEBUG_BOT] Воркер @%s: Токен заблокирован ТГ (TelegramUnauthorizedError). Выхожу.", cb.username)
                        await self._deactivate_invalid_token(cb.id, cb.username)
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
                    log.info("[DEBUG_BOT] Воркер @%s: Сессия aiohttp закрыта успешно.", cb.username)
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
            log.info("[DEBUG_BOT] stop_bot: Сигнализирую dp.stop_polling() для корректного завершения сетевого цикла.")
            await dp.stop_polling()
            # Короткая пауза, чтобы aiogram успел обработать изменение флага running
            await asyncio.sleep(0.2)

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
        """Запускает фоновый цикл, который раз в CONFLICT_CHECK_EVERY (30
        минут по запросу) отключает ботов, непрерывно застрявших в
        TelegramConflictError (см. _conflict_since)."""
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
        """Отключает бота, который непрерывно (без единого успешного
        старта) находится в TelegramConflictError дольше
        CONFLICT_DISABLE_AFTER — обычно значит, что токен уже используется
        где-то ещё (второй запущенный инстанс/локальный запуск у
        владельца), и бесконечный ретрай только жжёт ресурсы, ничего не
        доставляя пользователям."""
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
        log.error("Bot %s (@%s): непрерывный TelegramConflictError дольше %s — "
                  "отключаю (is_active=False), скорее всего токен используется "
                  "где-то ещё.", bot_id, username, CONFLICT_DISABLE_AFTER)
        if owner_id:
            try:
                from config import MASTER_BOT_TOKEN
                notifier = Bot(MASTER_BOT_TOKEN)
                try:
                    await notifier.send_message(
                        owner_id,
                        f"⚠️ Бот @{username} остановлен: обнаружен постоянный конфликт "
                        "получения обновлений (TelegramConflictError) — похоже, этот "
                        "же токен сейчас используется где-то ещё (другой запущенный "
                        "экземпляр бота). Остановите второй экземпляр и включите бота "
                        "заново.")
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


# Расширяемый вариант reupload_photo_for_bot на все типы медиа — нужен для
# вопросов анкет и финального сообщения анкеты (см. child/survey.py,
# master/router.py::survey_q_text_save/surveyfinish_save), т.к. владелец
# настраивает их через МАСТЕР-бота, а показывает их ДОЧЕРНИЙ — тот же класс
# бага с "file_id действителен только для бота, которым получен", что и с
# welcome_photo/response_photo (см. reupload_photo_for_bot выше).
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
        log.warning("reupload_media_for_bot: не удалось перенести file_id (%s) для "
                    "бота %s: %s", media_type, bot_id, e)
        return None
