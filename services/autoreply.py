# services/autoreply.py
"""Логика проверки и срабатывания автоответов (AutoReply).

Три вида правил (AutoReplyKind):
  - first_message : срабатывает ровно один раз, когда incoming_msg_count == 1
  - every_n       : срабатывает каждые N сообщений (param = строка с числом)
  - keyword       : срабатывает, если текст сообщения содержит param (без регистра)

Вызов: await check_and_fire(m, bot, bot_db_id)
ВАЖНО: вызывать уже ПОСЛЕ того, как incoming_msg_count был инкрементирован
в текущей транзакции и закоммичен.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from sqlalchemy import select

from db.base import Session
from db.models import AutoReply, AutoReplyKind, BotUser

log = logging.getLogger("services.autoreply")


async def check_and_fire(m: Message, bot: Bot, bot_db_id: int) -> bool:
    """Проверяет все активные правила бота и отправляет подходящие автоответы.

    Возвращает True, если хотя бы одно правило сработало.
    keyword-правила проверяются все (можно поставить несколько на разные слова).
    first_message и every_n тоже не прерывают цепочку.
    """
    async with Session() as s:
        rules = (await s.scalars(
            select(AutoReply)
            .where(AutoReply.bot_id == bot_db_id, AutoReply.is_active.is_(True))
            .order_by(AutoReply.position, AutoReply.id)
        )).all()
        user = await s.scalar(
            select(BotUser).where(
                BotUser.bot_id == bot_db_id,
                BotUser.user_id == m.from_user.id,
            )
        )

    if not rules or not user:
        return False

    count = user.incoming_msg_count or 0
    fired = False

    for rule in rules:
        matched = False

        if rule.kind == AutoReplyKind.keyword:
            if m.text and rule.param:
                matched = rule.param.lower() in m.text.lower()

        elif rule.kind == AutoReplyKind.first_message:
            matched = (count == 1)

        elif rule.kind == AutoReplyKind.every_n:
            try:
                n = int(rule.param or "0")
                matched = (n > 0 and count % n == 0)
            except ValueError:
                log.warning("AutoReply %s: невалидный param '%s'", rule.id, rule.param)

        if matched:
            await _send_autoreply(m, rule)
            fired = True

    return fired


async def _send_autoreply(m: Message, rule: AutoReply) -> None:
    """Отправляет один автоответ пользователю."""
    try:
        if rule.photo:
            try:
                if rule.text and len(rule.text) <= 1024:
                    await m.answer_photo(rule.photo, caption=rule.text, parse_mode="HTML")
                else:
                    await m.answer_photo(rule.photo)
                    if rule.text:
                        await m.answer(rule.text, parse_mode="HTML")
                return
            except TelegramBadRequest as e:
                log.warning("AutoReply %s: фото не отправилось (%s), шлю текст", rule.id, e)
        if rule.text:
            await m.answer(rule.text, parse_mode="HTML")
    except TelegramBadRequest as e:
        log.warning("AutoReply %s: ошибка отправки: %s", rule.id, e)
    except Exception:
        log.exception("AutoReply %s: неожиданная ошибка", rule.id)
