import asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from datetime import datetime, timedelta
from db.base import Session
from db.models import BotUser
from config import BROADCAST_RATE


async def run_broadcast(token: str, bot_id: int, *, target: str,
                        html_text: str | None, media_file_id: str | None,
                        media_type: str | None,
                        progress_cb=None) -> dict:
    """target: 'all' | 'active' (активные за 7 дней)."""
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
    try:
        for i, uid in enumerate(user_ids):
            try:
                await _send(bot, uid, html_text, media_file_id, media_type)
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await _send(bot, uid, html_text, media_file_id, media_type)
                    sent += 1
                except Exception:
                    failed += 1
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
            if progress_cb and i % 50 == 0:
                await progress_cb(i + 1, len(user_ids))
            await asyncio.sleep(1 / BROADCAST_RATE)
    finally:
        await bot.session.close()
    return {"total": len(user_ids), "sent": sent, "blocked": blocked, "failed": failed}


async def _send(bot: Bot, chat_id: int, text, file_id, media_type):
    # Кружки/стикеры в Telegram не поддерживают caption вообще — раньше их
    # там, где такое медиа всё же доходило, отправка либо падала, либо текст
    # молча терялся. Теперь для них текст (если есть) шлём отдельным
    # сообщением следом.
    if file_id and media_type == "photo":
        await bot.send_photo(chat_id, file_id, caption=text, parse_mode="HTML")
    elif file_id and media_type == "video":
        await bot.send_video(chat_id, file_id, caption=text, parse_mode="HTML")
    elif file_id and media_type == "document":
        await bot.send_document(chat_id, file_id, caption=text, parse_mode="HTML")
    elif file_id and media_type == "animation":
        await bot.send_animation(chat_id, file_id, caption=text, parse_mode="HTML")
    elif file_id and media_type == "audio":
        await bot.send_audio(chat_id, file_id, caption=text, parse_mode="HTML")
    elif file_id and media_type == "voice":
        await bot.send_voice(chat_id, file_id, caption=text, parse_mode="HTML")
    elif file_id and media_type == "video_note":
        await bot.send_video_note(chat_id, file_id)
        if text:
            await bot.send_message(chat_id, text, parse_mode="HTML")
    elif file_id and media_type == "sticker":
        await bot.send_sticker(chat_id, file_id)
        if text:
            await bot.send_message(chat_id, text, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML")
