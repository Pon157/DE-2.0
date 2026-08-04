import asyncio
import logging
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from datetime import datetime, timedelta
from db.base import Session
from db.models import BotUser
from config import BROADCAST_RATE

log = logging.getLogger("broadcast")


async def run_broadcast(token: str, bot_id: int, *, target: str,
                        html_text: str | None,
                        media_bytes: bytes | None = None,
                        media_filename: str | None = None,
                        media_type: str | None = None,
                        # оставлено для обратной совместимости старых вызовов —
                        # БАГ (критичный, объясняет "0 доставлено"/сотни ошибок
                        # при рассылке по всем ботам): file_id в Telegram
                        # привязан к КОНКРЕТНОМУ боту, которым файл был
                        # получен. Медиа для рассылки всегда захватывается в
                        # чате с МАСТЕР-ботом, а рассылается потом ДОЧЕРНИМ —
                        # с точки зрения Telegram это разные боты, и старый
                        # file_id для них невалиден ("wrong file identifier").
                        # Раньше это тихо проглатывалось `except Exception:
                        # failed += 1` без единой строчки в логах. Теперь
                        # вызывающий код (master/router.py) СКАЧИВАЕТ байты
                        # файла через master-бота один раз и передаёт их сюда
                        # (media_bytes) — мы заново загружаем их в целевого
                        # (дочернего) бота. file_id больше не принимается.
                        media_file_id: str | None = None,
                        progress_cb=None) -> dict:
    """target: 'all' | 'active' (активные за 7 дней)."""
    if media_file_id and not media_bytes:
        log.error(
            "run_broadcast(bot_id=%s): передан media_file_id из чужого бота "
            "(скорее всего мастер-бота) без media_bytes — это ГАРАНТИРОВАННО "
            "не сработает (file_id чужого бота недействителен). Передавайте "
            "media_bytes.", bot_id)

    async with Session() as s:
        q = select(BotUser.user_id).where(
            BotUser.bot_id == bot_id,
            BotUser.is_blocked_bot.is_(False),
            BotUser.is_banned.is_(False))
        if target == "active":
            q = q.where(BotUser.last_active >= datetime.utcnow() - timedelta(days=7))
        user_ids = (await s.scalars(q)).all()

    sent = blocked = failed = 0
    bot = Bot(token)
    # Кэш file_id, полученного ПОСЛЕ первой заливки media_bytes В ЭТОГО
    # конкретного (дочернего) бота — Telegram при первой отправке возвращает
    # свежий file_id, валидный именно для этого бота, дальше рассылаем уже
    # им, а не гоняем сырые байты на каждого получателя (быстрее и дешевле).
    reusable_file_id: str | None = None
    try:
        for i, uid in enumerate(user_ids):
            try:
                reusable_file_id = await _send(
                    bot, uid, html_text, media_bytes, media_filename, media_type,
                    reusable_file_id)
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    reusable_file_id = await _send(
                        bot, uid, html_text, media_bytes, media_filename, media_type,
                        reusable_file_id)
                    sent += 1
                except Exception:
                    failed += 1
                    log.exception("Broadcast bot_id=%s: не удалось отправить user_id=%s "
                                  "(после RetryAfter)", bot_id, uid)
            except TelegramForbiddenError:
                blocked += 1
                async with Session() as s:
                    u = await s.scalar(select(BotUser).where(
                        BotUser.bot_id == bot_id, BotUser.user_id == uid))
                    if u:
                        u.is_blocked_bot = True
                        await s.commit()
            except Exception:
                failed += 1
                log.exception("Broadcast bot_id=%s: не удалось отправить user_id=%s",
                              bot_id, uid)
            if progress_cb and i % 50 == 0:
                await progress_cb(i + 1, len(user_ids))
            await asyncio.sleep(1 / BROADCAST_RATE)
    finally:
        await bot.session.close()
    if failed:
        log.warning("Broadcast bot_id=%s завершена с ошибками: total=%s sent=%s "
                    "blocked=%s failed=%s", bot_id, len(user_ids), sent, blocked, failed)
    return {"total": len(user_ids), "sent": sent, "blocked": blocked, "failed": failed}


async def _send(bot: Bot, chat_id: int, text, media_bytes: bytes | None,
                media_filename: str | None, media_type: str | None,
                reusable_file_id: str | None) -> str | None:
    """Отправляет сообщение и возвращает file_id, который можно переиспользовать
    для СЛЕДУЮЩИХ отправок ЭТИМ ЖЕ ботом (если он ещё не был получен раньше).

    Кружки/стикеры в Telegram не поддерживают caption вообще — раньше их
    там, где такое медиа всё же доходило, отправка либо падала, либо текст
    молча терялся. Теперь для них текст (если есть) шлём отдельным
    сообщением следом.
    """
    if media_bytes and media_type:
        media = reusable_file_id or BufferedInputFile(
            media_bytes, filename=media_filename or "file")
        if media_type == "photo":
            msg = await bot.send_photo(chat_id, media, caption=text, parse_mode="HTML")
            return reusable_file_id or msg.photo[-1].file_id
        elif media_type == "video":
            msg = await bot.send_video(chat_id, media, caption=text, parse_mode="HTML")
            return reusable_file_id or msg.video.file_id
        elif media_type == "document":
            msg = await bot.send_document(chat_id, media, caption=text, parse_mode="HTML")
            return reusable_file_id or msg.document.file_id
        elif media_type == "animation":
            msg = await bot.send_animation(chat_id, media, caption=text, parse_mode="HTML")
            return reusable_file_id or msg.animation.file_id
        elif media_type == "audio":
            msg = await bot.send_audio(chat_id, media, caption=text, parse_mode="HTML")
            return reusable_file_id or msg.audio.file_id
        elif media_type == "voice":
            msg = await bot.send_voice(chat_id, media, caption=text, parse_mode="HTML")
            return reusable_file_id or msg.voice.file_id
        elif media_type == "video_note":
            msg = await bot.send_video_note(chat_id, media)
            if text:
                await bot.send_message(chat_id, text, parse_mode="HTML")
            return reusable_file_id or msg.video_note.file_id
        elif media_type == "sticker":
            msg = await bot.send_sticker(chat_id, media)
            if text:
                await bot.send_message(chat_id, text, parse_mode="HTML")
            return reusable_file_id or msg.sticker.file_id
    await bot.send_message(chat_id, text, parse_mode="HTML")
    return reusable_file_id
