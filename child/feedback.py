from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from db.base import Session
from db.models import (ChildBot, BotButton, Ticket, MessageLog,
                       MsgMap, OpenMode, ForwardMode)
from services import moderation as mod
from child.common import inject_extras, build_keyboards, send_with_keyboards, handle_keyboard_button, open_ticket
from utils.emoji import em


async def _cfg(bot_db_id: int) -> ChildBot:
    async with Session() as s:
        return await s.get(ChildBot, bot_db_id)


def build_feedback_router() -> Router:
    r = Router()

    # ================= пользователь =================
    @r.message(CommandStart(), F.chat.type == "private")
    async def start(m: Message, bot: Bot, bot_db_id: int):
        cfg = await _cfg(bot_db_id)
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            await s.commit()
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        ikb, rkb = await build_keyboards(bot_db_id, cfg)
        welcome = await inject_extras(bot_db_id, cfg.welcome_text)
        await send_with_keyboards(m, welcome, ikb, rkb, photo=cfg.welcome_photo)
        if cfg.open_mode == OpenMode.start_command:
            await open_ticket(bot, cfg, m.from_user.id)

    @r.message(Command("restart"), F.chat.type == "private")
    async def restart(m: Message, bot: Bot, bot_db_id: int):
        cfg = await _cfg(bot_db_id)
        await open_ticket(bot, cfg, m.from_user.id, force_new=True)
        await m.answer(f"{em('new')} Новое обращение открыто!"
                       if cfg.always_new_ticket else
                       f"{em('check')} Обращение продолжено.")

    @r.callback_query(F.data == "open_ticket")
    async def cb_open(c: CallbackQuery, bot: Bot, bot_db_id: int):
        cfg = await _cfg(bot_db_id)
        await open_ticket(bot, cfg, c.from_user.id, force_new=True)
        await c.answer("Обращение открыто! Напишите сообщение.", show_alert=True)

    @r.callback_query(F.data.startswith("trg:"))
    async def cb_trigger(c: CallbackQuery, bot_db_id: int):
        async with Session() as s:
            b = await s.get(BotButton, int(c.data.split(":")[1]))
        if b and b.response_text:
            if b.response_photo:
                await c.message.answer_photo(b.response_photo, caption=b.response_text)
            else:
                await c.message.answer(b.response_text)
        await c.answer()

    @r.message(F.chat.type == "private", F.text.startswith("/"))
    async def custom_command(m: Message, bot_db_id: int):
        """Триггер-команды, настроенные владельцем."""
        cmd = m.text.split()[0].lstrip("/").lower()
        async with Session() as s:
            b = await s.scalar(select(BotButton).where(
                BotButton.bot_id == bot_db_id, BotButton.kind == "command",
                BotButton.text == cmd))
        if b:
            if b.response_photo:
                await m.answer_photo(b.response_photo, caption=b.response_text or "")
            else:
                await m.answer(b.response_text or "")

    @r.message(F.chat.type == "private")
    async def user_message(m: Message, bot: Bot, bot_db_id: int):
        cfg = await _cfg(bot_db_id)
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id, direction="in"))
            await s.commit()
        if await handle_keyboard_button(m, bot_db_id):
            return
        if not cfg.admin_chat_id:
            return

        # первое сообщение = открытие обращения
        ticket = await open_ticket(bot, cfg, m.from_user.id)
        thread = ticket.topic_id if cfg.use_topics else None

        if cfg.forward_mode == ForwardMode.forward:
            fwd = await bot.forward_message(cfg.admin_chat_id, m.chat.id, m.message_id,
                                            message_thread_id=thread)
        else:
            header = cfg.copy_header.format(
                name=m.from_user.full_name,
                username=m.from_user.username or "—",
                id=m.from_user.id)
            await bot.send_message(cfg.admin_chat_id, header, message_thread_id=thread)
            fwd = await bot.copy_message(cfg.admin_chat_id, m.chat.id, m.message_id,
                                         message_thread_id=thread)
        async with Session() as s:
            s.add(MsgMap(bot_id=bot_db_id, admin_chat_msg_id=fwd.message_id,
                         user_id=m.from_user.id))
            await s.commit()

    # ================= админ-чат: ответы =================
    @r.message(F.chat.type.in_({"group", "supergroup"}))
    async def admin_reply(m: Message, bot: Bot, bot_db_id: int):
        cfg = await _cfg(bot_db_id)
        if m.chat.id != cfg.admin_chat_id or m.from_user.is_bot:
            return
        target_uid = None
        if cfg.use_topics and m.message_thread_id:
            async with Session() as s:
                t = await s.scalar(select(Ticket).where(
                    Ticket.bot_id == bot_db_id, Ticket.topic_id == m.message_thread_id))
                target_uid = t.user_id if t else None
        elif m.reply_to_message:
            async with Session() as s:
                mp = await s.scalar(select(MsgMap).where(
                    MsgMap.bot_id == bot_db_id,
                    MsgMap.admin_chat_msg_id == m.reply_to_message.message_id))
                target_uid = mp.user_id if mp else None
        if not target_uid:
            return
        try:
            await bot.copy_message(target_uid, m.chat.id, m.message_id)
            async with Session() as s:
                s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id,
                                 direction="out", is_admin=True,
                                 admin_username=m.from_user.username))
                await s.commit()
            await m.react([{"type": "emoji", "emoji": "👍"}])
        except Exception:
            await m.reply(f"{em('cross')} Не доставлено (пользователь заблокировал бота).")

    return r
