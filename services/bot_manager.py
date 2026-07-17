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
        # Отдельный лок на каждого бота, чтобы конкурентные старт/стоп/рестарт
        # (например, несколько настроек сохранены подряд -> несколько restart_bot)
        # не порождали два параллельных getUpdates для одного токена -> TelegramConflictError.
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
        """Пытается атомарно "застолбить" бота за этим процессом.

        КОРЕНЬ ПРОБЛЕМЫ "TelegramConflictError" на протяжении долгого времени
        (не единичный реконнект, а минуты подряд) почти всегда означает, что
        ДВА процесса одновременно держат getUpdates для одного токена — самый
        частый случай: старый контейнер/деплой не был до конца остановлен,
        когда поднялся новый. Раньше приложение никак не могло это обнаружить
        и просто запускало поллинг, полагаясь, что "снаружи" всё чисто.
        Теперь перед стартом каждый процесс пишет в БД свою метку-heartbeat;
        если свежую метку уже держит ДРУГОЙ instance_id — второй экземпляр
        не запускает поллинг, а только предупреждает в лог, вместо того чтобы
        бесконечно конфликтовать с "живым" инстансом.
        """
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

    async def _heartbeat_loop(self, bot_id: int):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_EVERY)
                async with Session() as s:
                    row = await s.get(BotRuntimeLock, bot_id)
                    if row and row.holder == self._instance_id:
                        row.last_seen = datetime.utcnow()
                        await s.commit()
        except asyncio.CancelledError:
            pass

    async def _start_bot_locked(self, cb: ChildBot):
        existing = self.tasks.get(cb.id)
        if existing and not existing.done():
            return
        if existing and existing.done():
            # Задача когда-то упала/завершилась сама, но не была вычищена — чистим,
            # иначе бот никогда не перезапустится (баг: "бот навсегда мёртв после креша").
            self.tasks.pop(cb.id, None)
            self.bots.pop(cb.id, None)

        if not await self._claim_runtime_lock(cb.id):
            log.warning(
                "Bot %s (@%s) уже поднят ДРУГИМ процессом (по метке в БД) — "
                "не запускаю второй поллинг. Если это ошибка (например, старый "
                "контейнер завис) — проверьте, что запущен только один "
                "инстанс приложения на эту БД.", cb.id, cb.username)
            return

        from child.feedback import build_feedback_router
        from child.posting import build_posting_router
        from child.common import build_common_router

        bot = Bot(cb.token, default=DefaultBotProperties(parse_mode="HTML"))

        # Гарантируем, что предыдущая long-poll сессия (если была) закрыта
        # ПЕРЕД тем, как открывать новую — иначе Telegram какое-то время видит
        # два getUpdates-подключения на один токен и отдаёт TelegramConflictError.
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
            heartbeat = asyncio.create_task(self._heartbeat_loop(cb.id))
            backoff = 5
            try:
                while True:
                    try:
                        await dp.start_polling(
                            bot, handle_signals=False,
                            allowed_updates=dp.resolve_used_update_types())
                        return  # штатная остановка (stop_polling / отмена)
                    except TelegramConflictError:
                        # aiogram сам ретраит конфликты внутри start_polling и
                        # обычно не пробрасывает исключение сюда, но на случай
                        # если пробросит — не долбим Telegram мгновенными
                        # реконнектами, ждём и пробуем снова с нарастанием.
                        log.warning("Conflict for bot %s (@%s), retry in %ss",
                                   cb.id, cb.username, backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60)
                    except TelegramUnauthorizedError:
                        log.error("Bot @%s: token revoked, stopping", cb.username)
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Bot %s crashed: %s", cb.username, e)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                await self._release_runtime_lock(cb.id)
                try:
                    await bot.session.close()
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
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if bot:
            try:
                await bot.session.close()
            except Exception:
                pass
        await self._release_runtime_lock(bot_id)

    async def restart_bot(self, bot_id: int):
        # Стоп и старт под одним локом — исключает гонку, когда несколько
        # быстрых сохранений настроек шлют несколько restart_bot() подряд.
        async with self._lock(bot_id):
            await self._stop_bot_locked(bot_id)
            async with Session() as s:
                cb = await s.get(ChildBot, bot_id)
            if cb and cb.is_active:
                await self._start_bot_locked(cb)

    async def stop_all(self):
        """Аккуратно останавливает всех дочерних ботов (используется при shutdown)."""
        ids = list(self.tasks.keys())
        await asyncio.gather(*(self.stop_bot(i) for i in ids), return_exceptions=True)


manager = BotManager()


async def reupload_photo_for_bot(source_bot: Bot, bot_id: int, file_id: str,
                                 target_chat_id: int) -> str | None:
    """Конвертирует file_id, полученный ЧЕРЕЗ ОДНОГО бота (например мастер-бот
    при настройке в конструкторе), в file_id, валидный для ДОЧЕРНЕГО бота.

    Корень бага "wrong file identifier/HTTP URL specified": file_id в
    Telegram Bot API привязан к конкретному боту, которым файл был получен.
    Когда владелец присылает фото приветствия/кнопки МАСТЕР-боту, а его потом
    пытается отправить ДОЧЕРНИЙ бот (другой токен) — Telegram отвечает
    ошибкой, потому что для него это чужой, непонятный идентификатор.

    Единственный способ "перенести" файл между ботами — скачать его байты и
    заново загрузить от имени целевого бота. Отправляем результат в личный
    чат владельца (target_chat_id) и сразу удаляем служебное сообщение —
    остаётся только новый, валидный для ДОЧЕРНЕГО бота file_id.

    Возвращает None, если конвертация не удалась (например, владелец ещё ни
    разу не писал дочернему боту, и слать ему нельзя — Forbidden). В этом
    случае вызывающий код должен либо не сохранять фото, либо сохранить
    исходный file_id как best-effort (тогда сработает defensive fallback в
    child/common.py::send_with_keyboards — приветствие уйдёт текстом, без
    падения, но без фото).
    """
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
            pass  # неважно, удалилось служебное сообщение или нет — file_id уже получен
        return sent.photo[-1].file_id
    except Exception as e:
        log.warning("reupload_photo_for_bot: не удалось перенести file_id для бота %s: %s",
                   bot_id, e)
        return None
