import asyncio
import hashlib
import logging
import re
import time as _time
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, BaseFilter
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, PreCheckoutQuery, LabeledPrice,
                           CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
                           MessageReactionUpdated, ReactionTypeEmoji, ReplyParameters)
from sqlalchemy import select
from db.base import Session
from db.models import (ChildBot, BotAdmin, Donation, BotButton, OpenMode, ForwardMode,
                       BotUser, Ticket, MsgMap, MessageLog)
from services import moderation as mod
from services import ads as ads_service
from services import referrals
from utils.emoji import em, styled_button
from config import PLATFORM_BOT_USERNAME

log = logging.getLogger("child.common")


async def safe_call(coro_func, *args, retries: int = 2, **kwargs):
    for attempt in range(retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except TelegramRetryAfter as e:
            if attempt >= retries:
                log.warning("safe_call: flood control не отступил после %d попыток", retries)
                raise
            await asyncio.sleep(e.retry_after + 0.5)
    return None


class DonateSt(StatesGroup):
    amount = State()


RESERVED_COMMANDS = {"start", "restart", "cancel", "donate", "newpost", "done",
                     "ads", "ban", "unban", "warn", "unwarn", "ref", "pro"}


async def get_cfg(bot_db_id: int) -> ChildBot | None:
    async with Session() as s:
        return await s.get(ChildBot, bot_db_id)

async def is_bot_admin(bot_db_id: int, user_id: int) -> bool:
    async with Session() as s:
        cfg = await s.get(ChildBot, bot_db_id)
        if cfg.owner_id == user_id:
            return True
        return bool(await s.scalar(select(BotAdmin).where(
            BotAdmin.bot_id == bot_db_id, BotAdmin.user_id == user_id)))


async def should_apply_antispam(bot_db_id: int, cfg: ChildBot, user_id: int) -> bool:
    if not cfg.antispam_enabled:
        return False
    is_admin = await is_bot_admin(bot_db_id, user_id)
    if not is_admin:
        return True
    if cfg.owner_id == user_id and not cfg.antispam_ignore_owner:
        return True
    return False


ALBUM_DEBOUNCE = 0.8


def message_media(m: Message):
    if m.photo:
        return m.photo[-1].file_id, "photo"
    if m.video:
        return m.video.file_id, "video"
    if m.animation:
        return m.animation.file_id, "animation"
    if m.document:
        return m.document.file_id, "document"
    if m.audio:
        return m.audio.file_id, "audio"
    if m.voice:
        return m.voice.file_id, "voice"
    if m.video_note:
        return m.video_note.file_id, "video_note"
    if m.sticker:
        return m.sticker.file_id, "sticker"
    return None, None


_album_buffers: dict[str, list[Message]] = {}
_album_timers: dict[str, asyncio.Task] = {}


async def buffer_or_process(m: Message, process):
    if not m.media_group_id:
        await process([m])
        return
    gid = m.media_group_id
    _album_buffers.setdefault(gid, []).append(m)
    old = _album_timers.get(gid)
    if old:
        old.cancel()

    async def _fire():
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE)
        except asyncio.CancelledError:
            return
        msgs = _album_buffers.pop(gid, [])
        _album_timers.pop(gid, None)
        if msgs:
            msgs.sort(key=lambda x: x.message_id)
            await process(msgs)

    _album_timers[gid] = asyncio.create_task(_fire())


def group_from_messages(msgs: list[Message]) -> list[dict] | None:
    if len(msgs) < 2:
        return None
    items = []
    for mm in msgs:
        fid, mtype = message_media(mm)
        if fid and mtype in ("photo", "video"):
            items.append({"file_id": fid, "type": mtype})
    return items or None


def text_from_messages(msgs: list[Message]) -> str:
    for mm in msgs:
        if mm.html_text:
            return mm.html_text
    return ""


def anon_id_for(bot_id: int, user_id: int) -> str:
    h = hashlib.md5(f"de:{bot_id}:{user_id}".encode()).hexdigest()[:8]
    return f"#{h}"


def _tpl_vars(bot_id: int, user_id: int, full_name: str | None, username: str | None) -> dict:
    return {
        "name": full_name or str(user_id),
        "username": username or "—",
        "id": user_id,
        "anon_id": anon_id_for(bot_id, user_id),
    }


def build_header(cfg: ChildBot, user) -> str:
    try:
        return cfg.copy_header.format(**_tpl_vars(cfg.id, user.id, user.full_name, user.username))
    except Exception:
        return (f"{user.full_name} | @{user.username or '—'} | <code>{user.id}</code> "
                f"· {anon_id_for(cfg.id, user.id)}")


_TAG_RE = re.compile(r"<[^>]+>")


def build_topic_name(cfg: ChildBot, user_id: int,
                     full_name: str | None, username: str | None) -> str:
    tpl = cfg.topic_name_template or "✉️ {name} · {id}"
    try:
        name = tpl.format(**_tpl_vars(cfg.id, user_id, full_name, username))
    except Exception:
        name = f"✉️ {full_name or user_id} · {user_id}"
    name = _TAG_RE.sub("", name).strip()
    return (name or f"✉️ {user_id}")[:120]


async def inject_ad(bot_db_id: int, text: str) -> str:
    ad = await ads_service.get_active_ad_for_display(bot_db_id)
    if not ad:
        return text
    await ads_service.register_impression(ad.id)
    return text + f"\n\n— — — — —\n{em('megaphone')} <i>{ad.text}</i>"


async def inject_footer(bot_db_id: int, text: str) -> str:
    async with Session() as s:
        cfg = await s.get(ChildBot, bot_db_id)
    if not cfg:
        return text
    if await referrals.is_pro(cfg.owner_id):
        return text
    return text + f"\n\n<i>🤖 создано на платформе @{PLATFORM_BOT_USERNAME}</i>"


async def inject_extras(bot_db_id: int, text: str) -> str:
    text = (text or "").strip()
    text = await inject_ad(bot_db_id, text)
    text = await inject_footer(bot_db_id, text)
    return text


async def build_keyboards(bot_db_id: int, cfg: ChildBot, extra_inline: list | None = None,
                          with_close_ticket: bool = False):
    async with Session() as s:
        btns = (await s.scalars(select(BotButton).where(
            BotButton.bot_id == bot_db_id).order_by(BotButton.position))).all()
    inline_rows, kb_rows = [], []
    for b in btns:
        if b.kind == "inline_url":
            inline_rows.append([InlineKeyboardButton(
                text=b.text, url=b.url, style=b.style, icon_custom_emoji_id=b.icon_emoji_id)])
        elif b.kind == "inline_trigger":
            inline_rows.append([InlineKeyboardButton(
                text=b.text, callback_data=f"trg:{b.id}",
                style=b.style, icon_custom_emoji_id=b.icon_emoji_id)])
        elif b.kind == "keyboard":
            kb_rows.append([KeyboardButton(text=b.text)])
    if extra_inline:
        inline_rows = extra_inline + inline_rows
    if getattr(cfg, "open_mode", None) == OpenMode.button:
        inline_rows.insert(0, [InlineKeyboardButton(
            text=cfg.ticket_button_text, callback_data="open_ticket",
            style=cfg.ticket_button_style, icon_custom_emoji_id=cfg.ticket_button_icon)])
    if cfg.donate_enabled:
        if cfg.donate_button_type == "inline":
            inline_rows.append([styled_button(cfg.donate_button_text, callback_data="donate_btn")])
        else:
            kb_rows.append([KeyboardButton(text=cfg.donate_button_text)])
    if with_close_ticket:
        kb_rows.append([KeyboardButton(text=cfg.close_ticket_button_text)])
    ikb = InlineKeyboardMarkup(inline_keyboard=inline_rows) if inline_rows else None
    rkb = ReplyKeyboardMarkup(keyboard=kb_rows, resize_keyboard=True) if kb_rows else None
    return ikb, rkb


async def send_with_keyboards(m: Message, text: str, ikb, rkb, photo: str | None = None):
    PHOTO_CAPTION_LIMIT = 1024
    safe_text = text if text and text.strip() else f"{em('wave')} Привет!"
    if photo:
        try:
            if len(safe_text) <= PHOTO_CAPTION_LIMIT:
                msg = await m.answer_photo(photo, caption=safe_text, reply_markup=ikb or rkb)
            else:
                await m.answer_photo(photo)
                msg = await m.answer(safe_text, reply_markup=ikb or rkb)
        except TelegramBadRequest as e:
            log.warning("send_with_keyboards: не смог отправить фото (%s), шлю только текст", e)
            msg = await m.answer(safe_text, reply_markup=ikb or rkb)
    else:
        msg = await m.answer(safe_text, reply_markup=ikb or rkb)
    if ikb and rkb:
        await m.answer(f"{em('gear')} Меню", reply_markup=rkb)
    return msg


async def send_response(m: Message, text: str | None, photo: str | None = None):
    text = text or ""
    if photo:
        try:
            if len(text) <= 1024:
                await m.answer_photo(photo, caption=text)
            else:
                await m.answer_photo(photo)
                if text:
                    await m.answer(text)
            return
        except TelegramBadRequest as e:
            log.warning("send_response: не смог отправить фото (%s), шлю только текст", e)
    if text:
        await m.answer(text)


async def handle_keyboard_button(m: Message, bot_db_id: int) -> bool:
    if not m.text:
        return False
    async with Session() as s:
        b = await s.scalar(select(BotButton).where(
            BotButton.bot_id == bot_db_id, BotButton.kind == "keyboard",
            BotButton.text == m.text))
    if b and (b.response_text is not None or b.response_photo):
        await send_response(m, b.response_text, b.response_photo)
        return True
    return False


async def _notify_user(bot: Bot, bot_db_id: int, user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
    except TelegramForbiddenError:
        await mod.mark_blocked_bot(bot_db_id, user_id)
    except Exception:
        pass


async def open_ticket(bot: Bot, cfg: ChildBot, user_id: int,
                      force_new: bool = False) -> tuple[Ticket, bool]:
    async with Session() as s:
        t = await s.scalar(select(Ticket).where(
            Ticket.bot_id == cfg.id, Ticket.user_id == user_id, Ticket.is_open))
        if t and not force_new:
            return t, False
        if t and force_new:
            if not getattr(cfg, "always_new_ticket", False):
                return t, False
            t.is_open = False
            await s.commit()
        topic_id = None
        if cfg.use_topics and cfg.admin_chat_id:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == cfg.id, BotUser.user_id == user_id))
            topic_name = build_topic_name(
                cfg, user_id,
                full_name=(u.full_name if u else None),
                username=(u.username if u else None))
            try:
                topic = await bot.create_forum_topic(cfg.admin_chat_id, topic_name)
                topic_id = topic.message_thread_id
            except Exception as e:
                log.warning("Bot %s: create_forum_topic failed (%s) — "
                            "продолжаю без топика", cfg.id, e)
        t = Ticket(bot_id=cfg.id, user_id=user_id, topic_id=topic_id)
        s.add(t)
        await s.commit()
    await _send_ticket_opened_keyboard(bot, cfg, user_id)
    return t, True


async def _send_ticket_opened_keyboard(bot: Bot, cfg: ChildBot, user_id: int):
    ikb, rkb = await build_keyboards(cfg.id, cfg, with_close_ticket=True)
    if rkb:
        try:
            await bot.send_message(user_id, f"{em('check')} Обращение открыто — "
                                   "можно продолжать писать сюда.", reply_markup=rkb)
        except Exception:
            pass


async def _map_msg(bot_db_id: int, admin_msg_id: int, user_id: int,
                   user_msg_id: int | None = None):
    async with Session() as s:
        s.add(MsgMap(bot_id=bot_db_id, admin_chat_msg_id=admin_msg_id,
                     user_id=user_id, user_chat_msg_id=user_msg_id))
        await s.commit()


def _combine_kb(kb1, kb2):
    if kb1 and kb2:
        return InlineKeyboardMarkup(inline_keyboard=kb1.inline_keyboard + kb2.inline_keyboard)
    return kb1 or kb2


async def relay_to_admin_chat(msgs: list[Message], bot: Bot, cfg: ChildBot,
                              extra_kb: InlineKeyboardMarkup | None = None):
    user = msgs[0].from_user
    ticket, created = await open_ticket(bot, cfg, user.id)
    thread = ticket.topic_id if cfg.use_topics else None
    header = build_header(cfg, user)
    header_mode = getattr(cfg, "header_mode", "separate") or "separate"
    is_album = len(msgs) > 1
    first = msgs[0]

    reply_params = None
    if first.reply_to_message:
        async with Session() as s:
            mp = await s.scalar(select(MsgMap).where(
                MsgMap.bot_id == cfg.id,
                MsgMap.user_chat_msg_id == first.reply_to_message.message_id
            ).order_by(MsgMap.id.desc()))
        if mp:
            reply_params = ReplyParameters(message_id=mp.admin_chat_msg_id)

    close_kb = None
    if cfg.forward_mode == ForwardMode.copy:
        close_kb = InlineKeyboardMarkup(inline_keyboard=[[
            styled_button("🔒 Закрыть обращение", callback_data=f"close_ticket:{ticket.id}")]])

    if header_mode == "merge" and cfg.forward_mode == ForwardMode.copy and not is_album:
        markup = _combine_kb(extra_kb, close_kb)
        if first.text:
            merged = f"{header}\n\n{first.html_text}"
            if len(merged) <= 4096:
                sent = await safe_call(bot.send_message, cfg.admin_chat_id, merged,
                                       message_thread_id=thread,
                                       reply_markup=markup,
                                       reply_parameters=reply_params)
                await _map_msg(cfg.id, sent.message_id, user.id, first.message_id)
                return
        else:
            fid, mtype = message_media(first)
            if fid and mtype in ("photo", "video", "animation", "document", "audio"):
                cap = first.html_text or ""
                merged = f"{header}\n\n{cap}" if cap else header
                if len(merged) <= 1024:
                    sent = await safe_call(
                        bot.copy_message, cfg.admin_chat_id, first.chat.id, first.message_id,
                        message_thread_id=thread, caption=merged,
                        reply_markup=markup, reply_parameters=reply_params)
                    await _map_msg(cfg.id, sent.message_id, user.id, first.message_id)
                    return

    if header_mode != "off":
        hm = await safe_call(bot.send_message, cfg.admin_chat_id, header,
                             message_thread_id=thread)
        await _map_msg(cfg.id, hm.message_id, user.id, None)

    if is_album:
        ids = [mm.message_id for mm in msgs]
        markup = _combine_kb(extra_kb, close_kb)
        if cfg.forward_mode == ForwardMode.forward:
            copies = await safe_call(bot.forward_messages, cfg.admin_chat_id, first.chat.id, ids,
                                     message_thread_id=thread)
            for mm, cp in zip(msgs, copies):
                await _map_msg(cfg.id, cp.message_id, user.id, mm.message_id)
            if markup:
                sm_expl = await safe_call(bot.send_message, cfg.admin_chat_id, "к сожалению, телеграм мне не позволяет прикреплять кнопки к альбомам. Но я могу отправить кнопки следующим сообщением.", message_thread_id=thread)
                sm_btn = await safe_call(bot.send_message, cfg.admin_chat_id, "👆 Кнопки к посту выше",
                                     message_thread_id=thread, reply_markup=markup)
                await _map_msg(cfg.id, sm_btn.message_id, user.id, None)
        elif markup:
            copies = await safe_call(bot.copy_messages, cfg.admin_chat_id, first.chat.id, ids, message_thread_id=thread)
            for mm, cp in zip(msgs, copies):
                await _map_msg(cfg.id, cp.message_id, user.id, mm.message_id)

            sm_expl = await safe_call(bot.send_message, cfg.admin_chat_id, "к сожалению, телеграм мне не позволяет прикреплять кнопки к альбомам. Но я могу отправить кнопки следующим сообщением.", message_thread_id=thread)
            sm_btn = await safe_call(bot.send_message, cfg.admin_chat_id, "👆 Кнопки к посту выше", message_thread_id=thread, reply_markup=markup)
            await _map_msg(cfg.id, sm_btn.message_id, user.id, None)
        else:
            copies = await safe_call(bot.copy_messages, cfg.admin_chat_id, first.chat.id, ids,
                                     message_thread_id=thread)
            for mm, cp in zip(msgs, copies):
                await _map_msg(cfg.id, cp.message_id, user.id, mm.message_id)
        return

    markup = _combine_kb(extra_kb, close_kb)
    if cfg.forward_mode == ForwardMode.forward and not markup:
        sent = await safe_call(bot.forward_message, cfg.admin_chat_id, first.chat.id,
                               first.message_id, message_thread_id=thread)
        await _map_msg(cfg.id, sent.message_id, user.id, first.message_id)
    else:
        sent = await safe_call(bot.copy_message, cfg.admin_chat_id, first.chat.id,
                               first.message_id, message_thread_id=thread,
                               reply_markup=markup, reply_parameters=reply_params)
        await _map_msg(cfg.id, sent.message_id, user.id, first.message_id)


async def _mirror_reaction(bot: Bot, chat_id: int, message_id: int, reactions):
    emojis = [ReactionTypeEmoji(emoji=r.emoji) for r in reactions
              if getattr(r, "type", None) == "emoji" and getattr(r, "emoji", None)]
    try:
        await bot.set_message_reaction(chat_id, message_id, reaction=emojis)
    except Exception:
        pass


_recent_mod_cmds: dict[tuple[int, int], float] = {}
_MOD_CMD_DEDUP_TTL = 30.0


def _mod_cmd_already_handled(bot_db_id: int, message_id: int) -> bool:
    now = _time.monotonic()
    key = (bot_db_id, message_id)
    stale = [k for k, ts in _recent_mod_cmds.items() if now - ts > _MOD_CMD_DEDUP_TTL]
    for k in stale:
        _recent_mod_cmds.pop(k, None)
    if key in _recent_mod_cmds:
        return True
    _recent_mod_cmds[key] = now
    return False


async def _target_from_reply(bot_db_id: int, m: Message) -> int | None:
    if not m.reply_to_message:
        return None
    async with Session() as s:
        mp = await s.scalar(select(MsgMap).where(
            MsgMap.bot_id == bot_db_id,
            MsgMap.admin_chat_msg_id == m.reply_to_message.message_id))
    return mp.user_id if mp else None


def build_common_router() -> Router:
    r = Router()

    @r.message(Command("ban"))
    async def cmd_ban(m: Message, command: CommandObject, bot_db_id: int, bot: Bot):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        if _mod_cmd_already_handled(bot_db_id, m.message_id):
            return
        reply_uid = await _target_from_reply(bot_db_id, m)
        if reply_uid is not None:
            args = command.args or ""
            reason, dur = "Не указана", "perm"
            if args.strip():
                parts = args.split()
                if parts and mod.DURATION_RE.match(parts[-1]):
                    dur = parts.pop()
                if parts:
                    reason = " ".join(parts)
            uid = reply_uid
        else:
            parsed = mod.parse_ban_args(command.args or "")
            if not parsed:
                await m.answer(f"{em('info')} Формат: <code>/ban 123456 Причина 7d</code>\n"
                               "Сроки: m/h/d/w/y/perm\n"
                               "Или ответьте (реплай) на сообщение пользователя в "
                               "админ-чате командой <code>/ban Причина 7d</code> без ID.")
                return
            uid, reason, dur = parsed
        text = await mod.ban_user(bot_db_id, uid, reason, dur,
                                  m.from_user.id, m.from_user.username)
        await m.answer(f"{em('no_entry')} " + text)
        until = "навсегда" if dur == "perm" else dur
        await _notify_user(bot, bot_db_id, uid, f"{em('no_entry')} Вы забанены в этом боте "
                           f"({until}).\nПричина: {reason}")

    @r.message(Command("unban"))
    async def cmd_unban(m: Message, command: CommandObject, bot_db_id: int, bot: Bot):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        if _mod_cmd_already_handled(bot_db_id, m.message_id):
            return
        reply_uid = await _target_from_reply(bot_db_id, m)
        if reply_uid is not None:
            uid = reply_uid
        elif command.args and command.args.split()[0].isdigit():
            uid = int(command.args.split()[0])
        else:
            await m.answer("Формат: <code>/unban 123456</code> либо реплай на "
                           "сообщение пользователя командой <code>/unban</code> без ID.")
            return
        text = await mod.unban_user(bot_db_id, uid, m.from_user.id, m.from_user.username)
        await m.answer(f"{em('check')} " + text)
        await _notify_user(bot, bot_db_id, uid, f"{em('check')} Вы разбанены в этом боте, снова можно писать.")

    @r.message(Command("warn"))
    async def cmd_warn(m: Message, command: CommandObject, bot_db_id: int, bot: Bot):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        if _mod_cmd_already_handled(bot_db_id, m.message_id):
            return
        reply_uid = await _target_from_reply(bot_db_id, m)
        if reply_uid is not None:
            uid = reply_uid
            reason = (command.args or "").strip() or "Не указана"
        else:
            parts = (command.args or "").split(maxsplit=1)
            if not parts or not parts[0].isdigit():
                await m.answer("Формат: <code>/warn 123456 Причина</code> либо реплай на "
                               "сообщение пользователя командой <code>/warn Причина</code> без ID.")
                return
            uid = int(parts[0])
            reason = parts[1] if len(parts) > 1 else "Не указана"
        text, autoban = await mod.warn_user(bot_db_id, uid, reason,
                                            m.from_user.id, m.from_user.username)
        await m.answer(f"{em('warn')} " + text)
        note = f"{em('warn')} Вам выдано предупреждение.\nПричина: {reason}"
        if autoban:
            note += f"\n{em('no_entry')} Достигнут лимит предупреждений — вы забанены."
        await _notify_user(bot, bot_db_id, uid, note)

    @r.message(Command("unwarn"))
    async def cmd_unwarn(m: Message, command: CommandObject, bot_db_id: int, bot: Bot):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        if _mod_cmd_already_handled(bot_db_id, m.message_id):
            return
        reply_uid = await _target_from_reply(bot_db_id, m)
        if reply_uid is not None:
            uid = reply_uid
        elif command.args and command.args.split()[0].isdigit():
            uid = int(command.args.split()[0])
        else:
            await m.answer("Формат: <code>/unwarn 123456</code> либо реплай на "
                           "сообщение пользователя командой <code>/unwarn</code> без ID.")
            return
        text = await mod.unwarn_user(bot_db_id, uid, m.from_user.id, m.from_user.username)
        await m.answer(f"{em('check')} " + text)
        await _notify_user(bot, bot_db_id, uid, f"{em('check')} С вас снято предупреждение.")

    class _DonateKbText(BaseFilter):
        async def __call__(self, m: Message, bot_db_id: int) -> bool:
            if not m.text:
                return False
            cfg = await get_cfg(bot_db_id)
            return bool(cfg and cfg.donate_enabled
                        and cfg.donate_button_type == "keyboard"
                        and m.text.strip() == cfg.donate_button_text.strip())

    @r.message(Command("donate"), F.chat.type == "private")
    @r.message(_DonateKbText(), F.chat.type == "private")
    async def donate_start(m: Message, bot_db_id: int, state: FSMContext):
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.donate_enabled:
            return
        await state.set_state(DonateSt.amount)
        await m.answer(f"{em('star')} Введите количество звёзд для доната (1–10000):")

    @r.message(DonateSt.amount, F.chat.type == "private")
    async def donate_amount(m: Message, bot: Bot, bot_db_id: int, state: FSMContext):
        if await mod.is_banned(bot_db_id, m.from_user.id):
            await state.clear()
            return
        if m.text and m.text.startswith("/"):
            await state.clear()
            raise SkipHandler
        await state.clear()
        if not m.text or not m.text.strip().isdigit():
            await m.answer(f"{em('warn')} Нужно целое число звёзд (1–10000). "
                           "Попробуйте /donate ещё раз.")
            return
        cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.donate_enabled:
            return
        stars = int(m.text.strip())
        if not 1 <= stars <= 10000:
            await m.answer(f"{em('warn')} Число должно быть от 1 до 10000.")
            return
        await bot.send_invoice(
            chat_id=m.chat.id, title="Донат", description=f"Поддержка на {stars} ⭐️",
            payload=f"donate:{stars}", currency="XTR",
            prices=[LabeledPrice(label=f"{stars} Stars", amount=stars)])

    @r.pre_checkout_query()
    async def pre_checkout(q: PreCheckoutQuery):
        await q.answer(ok=True)

    @r.message(F.successful_payment)
    async def paid(m: Message, bot_db_id: int):
        stars = m.successful_payment.total_amount
        async with Session() as s:
            s.add(Donation(bot_id=bot_db_id, user_id=m.from_user.id, stars=stars))
            await s.commit()
        await m.answer(f"{em('party')} Спасибо за донат {stars} {em('star')}!")

    @r.callback_query(F.data == "donate_btn")
    async def cb_donate(c: CallbackQuery, bot_db_id: int, state: FSMContext):
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.donate_enabled:
            await c.answer()
            return
        await state.set_state(DonateSt.amount)
        await c.message.answer(f"{em('star')} Введите количество звёзд для доната (1–10000):")
        await c.answer()

    class _CloseTicketKbText(BaseFilter):
        async def __call__(self, m: Message, bot_db_id: int) -> bool:
            if not m.text:
                return False
            cfg = await get_cfg(bot_db_id)
            return bool(cfg and m.text.strip() == cfg.close_ticket_button_text.strip())

    @r.message(_CloseTicketKbText(), F.chat.type == "private")
    async def user_close_ticket(m: Message, bot: Bot, bot_db_id: int):
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        async with Session() as s:
            t = await s.scalar(select(Ticket).where(
                Ticket.bot_id == bot_db_id, Ticket.user_id == m.from_user.id, Ticket.is_open))
            if not t:
                await m.answer(f"{em('info')} У вас нет открытых обращений.",
                               reply_markup=ReplyKeyboardRemove())
                return
            t.is_open = False
            await s.commit()
        cfg = await get_cfg(bot_db_id)
        await m.answer(f"{em('check')} Обращение закрыто. Спасибо!",
                       reply_markup=ReplyKeyboardRemove())
        if cfg and cfg.admin_chat_id:
            try:
                await bot.send_message(
                    cfg.admin_chat_id, f"{em('info')} Пользователь сам закрыл обращение.",
                    message_thread_id=t.topic_id if cfg.use_topics else None)
            except Exception:
                pass

    @r.callback_query(F.data.startswith("trg:"))
    async def cb_trigger(c: CallbackQuery, bot_db_id: int):
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        cfg = await get_cfg(bot_db_id)
        async with Session() as s:
            b = await s.get(BotButton, int(c.data.split(":")[1]))
        if b and (b.response_text or b.response_photo):
            await send_response(c.message, b.response_text, b.response_photo)
        await c.answer()

    @r.callback_query(F.data == "open_ticket")
    async def cb_open_ticket(c: CallbackQuery, bot: Bot, bot_db_id: int):
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        cfg = await get_cfg(bot_db_id)
        await open_ticket(bot, cfg, c.from_user.id, force_new=True)
        await c.answer("Обращение открыто! Напишите сообщение.", show_alert=True)

    @r.message(F.chat.type == "private", F.text.startswith("/"))
    async def custom_command(m: Message, bot_db_id: int):
        cmd = m.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in RESERVED_COMMANDS:
            raise SkipHandler
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        cfg = await get_cfg(bot_db_id)
        async with Session() as s:
            b = await s.scalar(select(BotButton).where(
                BotButton.bot_id == bot_db_id, BotButton.kind == "command",
                BotButton.text == cmd))
        if not b:
            raise SkipHandler
        await send_response(m, b.response_text, b.response_photo)

    async def _close_ticket_core(bot: Bot, bot_db_id: int, tid: int) -> str | None:
        async with Session() as s:
            t = await s.get(Ticket, tid)
            cfg = await s.get(ChildBot, bot_db_id)
            if not t or t.bot_id != bot_db_id:
                return "Обращение не найдено"
            if not t.is_open:
                return "Обращение уже закрыто"
            t.is_open = False
            await s.commit()
        if cfg.use_topics and cfg.admin_chat_id and t.topic_id:
            try:
                await bot.close_forum_topic(cfg.admin_chat_id, t.topic_id)
            except Exception:
                pass
        try:
            await bot.send_message(t.user_id,
                                   f"{em('lock')} Обращение закрыто администрацией. "
                                   "Ваше новое сообщение откроет новое обращение.",
                                   reply_markup=ReplyKeyboardRemove())
        except TelegramForbiddenError:
            await mod.mark_blocked_bot(bot_db_id, t.user_id)
        except Exception:
            pass
        return None

    @r.callback_query(F.data.startswith("close_ticket:"))
    async def cb_close_ticket(c: CallbackQuery, bot: Bot, bot_db_id: int):
        tid = int(c.data.split(":")[1])
        err = await _close_ticket_core(bot, bot_db_id, tid)
        if err:
            await c.answer(err, show_alert=(err == "Обращение не найдено"))
            return
        
        uname = c.from_user.username or c.from_user.first_name
        user_str = f"@{uname}" if c.from_user.username else uname
        
        try:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[styled_button("🔓 Открыть снова",
                                                callback_data=f"reopen_ticket:{tid}")]])
            
            text = c.message.html_text or c.message.caption or ""
            text += f"\n\n🔒 закрыл - {user_str}"
            if c.message.text:
                await c.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            else:
                await c.message.edit_caption(caption=text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        await c.answer("Обращение закрыто")

    @r.message(Command("close"))
    async def cmd_close(m: Message, bot: Bot, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        ticket_id = None
        if m.reply_to_message:
            uid = await _target_from_reply(bot_db_id, m)
            if uid is not None:
                async with Session() as s:
                    t = await s.scalar(select(Ticket).where(
                        Ticket.bot_id == bot_db_id, Ticket.user_id == uid, Ticket.is_open))
                if t:
                    ticket_id = t.id
        elif m.message_thread_id:
            async with Session() as s:
                t = await s.scalar(select(Ticket).where(
                    Ticket.bot_id == bot_db_id, Ticket.topic_id == m.message_thread_id,
                    Ticket.is_open))
            if t:
                ticket_id = t.id
        if ticket_id is None:
            await m.answer(f"{em('warn')} Не нашёл открытое обращение — ответьте (реплай) "
                           "на сообщение пользователя командой /close, либо напишите /close "
                           "прямо в топике этого обращения.")
            return
        err = await _close_ticket_core(bot, bot_db_id, ticket_id)
        await m.answer(f"{em('cross') if err else em('check')} {err or 'Обращение закрыто.'}")

    @r.callback_query(F.data.startswith("reopen_ticket:"))
    async def cb_reopen_ticket(c: CallbackQuery, bot: Bot, bot_db_id: int):
        tid = int(c.data.split(":")[1])
        async with Session() as s:
            t = await s.get(Ticket, tid)
            cfg = await s.get(ChildBot, bot_db_id)
            if not t or t.bot_id != bot_db_id:
                await c.answer("Обращение не найдено", show_alert=True)
                return
            if t.is_open:
                await c.answer("Обращение уже открыто")
                return
            t.is_open = True
            await s.commit()
        if cfg.use_topics and cfg.admin_chat_id and t.topic_id:
            try:
                await bot.reopen_forum_topic(cfg.admin_chat_id, t.topic_id)
            except Exception:
                pass
        
        uname = c.from_user.username or c.from_user.first_name
        user_str = f"@{uname}" if c.from_user.username else uname

        try:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[styled_button("🔒 Закрыть обращение",
                                                callback_data=f"close_ticket:{tid}")]])
            
            text = c.message.html_text or c.message.caption or ""
            text += f"\n\n🔓 открыл - {user_str}"
            if c.message.text:
                await c.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            else:
                await c.message.edit_caption(caption=text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        await c.answer("Обращение снова открыто")

    @r.message(F.chat.type.in_({"group", "supergroup"}))
    async def admin_reply(m: Message, bot: Bot, bot_db_id: int):
        cfg = await get_cfg(bot_db_id)
        if not cfg or m.chat.id != cfg.admin_chat_id or m.from_user.is_bot:
            return
        if m.text and m.text.startswith("/"):
            return
        target_uid = None
        reply_params = None
        if cfg.use_topics and m.message_thread_id:
            async with Session() as s:
                t = await s.scalar(select(Ticket).where(
                    Ticket.bot_id == bot_db_id, Ticket.topic_id == m.message_thread_id
                ).order_by(Ticket.id.desc()))
                target_uid = t.user_id if t else None
        if m.reply_to_message:
            async with Session() as s:
                mp = await s.scalar(select(MsgMap).where(
                    MsgMap.bot_id == bot_db_id,
                    MsgMap.admin_chat_msg_id == m.reply_to_message.message_id
                ).order_by(MsgMap.id.desc()))
            if mp:
                target_uid = target_uid or mp.user_id
                if mp.user_chat_msg_id and mp.user_id == target_uid:
                    reply_params = ReplyParameters(message_id=mp.user_chat_msg_id)
        if not target_uid:
            return
        try:
            await bot.copy_message(target_uid, m.chat.id, m.message_id,
                                   reply_parameters=reply_params)
            async with Session() as s:
                s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id,
                                 direction="out", is_admin=True,
                                 admin_username=m.from_user.username))
                await s.commit()
            await m.react([{"type": "emoji", "emoji": "👍"}])
        except TelegramForbiddenError:
            await mod.mark_blocked_bot(bot_db_id, target_uid)
            await m.reply(f"{em('cross')} Не доставлено (пользователь заблокировал бота).")
        except Exception:
            await m.reply(f"{em('cross')} Не доставлено (пользователь заблокировал бота).")

    @r.message_reaction(F.chat.type == "private")
    async def user_reaction(ev: MessageReactionUpdated, bot: Bot, bot_db_id: int):
        cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.admin_chat_id:
            return
        async with Session() as s:
            mp = await s.scalar(select(MsgMap).where(
                MsgMap.bot_id == bot_db_id,
                MsgMap.user_chat_msg_id == ev.message_id
            ).order_by(MsgMap.id.desc()))
        if not mp:
            return
        await _mirror_reaction(bot, cfg.admin_chat_id, mp.admin_chat_msg_id,
                               ev.new_reaction)

    @r.message_reaction(F.chat.type.in_({"group", "supergroup"}))
    async def admin_reaction(ev: MessageReactionUpdated, bot: Bot, bot_db_id: int):
        cfg = await get_cfg(bot_db_id)
        if not cfg or ev.chat.id != cfg.admin_chat_id:
            return
        async with Session() as s:
            mp = await s.scalar(select(MsgMap).where(
                MsgMap.bot_id == bot_db_id,
                MsgMap.admin_chat_msg_id == ev.message_id
            ).order_by(MsgMap.id.desc()))
        if not mp or not mp.user_chat_msg_id:
            return
        await _mirror_reaction(bot, mp.user_id, mp.user_chat_msg_id, ev.new_reaction)

    return r
