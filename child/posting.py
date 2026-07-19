import copy
import json
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, InputMediaPhoto, InputMediaVideo)
from sqlalchemy import select
from sqlalchemy import update as sa_update
from db.base import Session
from db.models import ChildBot, Suggestion, Post, MessageLog
from services import moderation as mod
from services import antispam
from child.common import (is_bot_admin, inject_extras, build_keyboards, send_with_keyboards,
                          handle_keyboard_button, buffer_or_process, message_media,
                          group_from_messages, text_from_messages, relay_to_admin_chat,
                          get_cfg, should_apply_antispam)
from utils.emoji import em, styled_button


def _media(m: Message):
    return message_media(m)


_group_from_messages = group_from_messages
_text_from_messages = text_from_messages


def _buttons_markup(*sources: str | None) -> InlineKeyboardMarkup | None:
    rows = []
    for src in sources:
        if not src:
            continue
        try:
            parsed = json.loads(src)
        except Exception:
            continue
        rows.extend(parsed)
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled_button(b["text"], url=b["url"], style=b.get("style")) if not b.get("icon")
         else InlineKeyboardButton(text=b["text"], url=b["url"], style=b.get("style"),
                                    icon_custom_emoji_id=b.get("icon"))
         for b in row] for row in rows])


BTN_MODES = [("both", "шаблон + свои"), ("template", "только из шаблона"),
             ("custom", "только свои"), ("none", "без кнопок")]


def btn_mode_label(mode: str | None) -> str:
    return dict(BTN_MODES).get(mode or "both", "шаблон + свои")


_MEDIA_SENDERS = {
    "photo": "send_photo", "video": "send_video", "animation": "send_animation",
    "audio": "send_audio", "document": "send_document", "voice": "send_voice",
}

CAPTION_LIMIT = 1024


class PublishResult:
    __slots__ = ("album_ids", "single_ids", "buttons_on")

    def __init__(self, album_ids: list[int], single_ids: list[int], buttons_on: int | None):
        self.album_ids = album_ids
        self.single_ids = single_ids
        self.buttons_on = buttons_on


async def publish(bot: Bot, cfg: ChildBot, *, html_text: str = "",
                  file_id: str | None = None, media_type: str | None = None,
                  media_group: list[dict] | None = None,
                  origin_chat_id: int | None = None,
                  origin_message_id: int | None = None,
                  origin_message_ids: str | None = None,
                  use_template: bool = True, buttons_json: str | None = None,
                  buttons_mode: str = "both", thread_id: int | None = None) -> PublishResult:
    
    if buttons_mode == "template":
        markup = _buttons_markup(cfg.template_buttons_json)
    elif buttons_mode == "custom":
        markup = _buttons_markup(buttons_json)
    elif buttons_mode == "none":
        markup = None
    else:  
        markup = _buttons_markup(buttons_json, cfg.template_buttons_json)
    text = cfg.post_template.replace("{text}", html_text) if use_template else html_text

    if not text and not file_id and not media_group \
            and not (origin_message_id or origin_message_ids):
        raise ValueError("пустой пост — нечего публиковать")

    if use_template and (origin_message_id or origin_message_ids):
        try:
            return await _publish_reconstructed(
                bot, cfg, text=text, file_id=file_id, media_type=media_type,
                media_group=media_group, markup=markup,
                origin_chat_id=origin_chat_id, origin_message_id=origin_message_id,
                origin_message_ids=origin_message_ids, thread_id=thread_id)
        except TelegramBadRequest:
            return await _publish_fallback_copy(
                bot, cfg, text=text, origin_chat_id=origin_chat_id,
                origin_message_id=origin_message_id,
                origin_message_ids=origin_message_ids, markup=markup, thread_id=thread_id)

    return await _publish_reconstructed(
        bot, cfg, text=text, file_id=file_id, media_type=media_type,
        media_group=media_group, markup=markup,
        origin_chat_id=origin_chat_id, origin_message_id=origin_message_id,
        origin_message_ids=origin_message_ids, thread_id=thread_id)


async def _publish_via_copy(bot: Bot, cfg: ChildBot, source_chat_id: int,
                            result: PublishResult, markup: InlineKeyboardMarkup | None):
    target_chat_id = cfg.channel_id
    forward = cfg.channel_publish_mode == "forward"
    if result.album_ids:
        if forward:
            await bot.forward_messages(target_chat_id, source_chat_id, result.album_ids)
        else:
            await bot.copy_messages(target_chat_id, source_chat_id, result.album_ids)
    for mid in result.single_ids:
        has_buttons = mid == result.buttons_on
        rm = markup if has_buttons else None
        
        if forward and not has_buttons:
            await bot.forward_message(target_chat_id, source_chat_id, mid)
        else:
            await bot.copy_message(target_chat_id, source_chat_id, mid, reply_markup=rm)


async def _publish_fallback_copy(bot: Bot, cfg: ChildBot, *, text: str,
                                 origin_chat_id: int | None,
                                 origin_message_id: int | None,
                                 origin_message_ids: str | None,
                                 markup: InlineKeyboardMarkup | None,
                                 thread_id: int | None = None) -> PublishResult:
    single_ids: list[int] = []
    buttons_on = None
    if origin_message_ids:
        ids = [int(x) for x in origin_message_ids.split(",")]
        # Изменение: всегда используем группировку для альбома
        sent_ids = [m.message_id for m in
                   await bot.copy_messages(cfg.channel_id, origin_chat_id, ids,
                                           message_thread_id=thread_id)]
        if text:
            m = await bot.send_message(cfg.channel_id, text, message_thread_id=thread_id)
            single_ids.append(m.message_id)
        return PublishResult(sent_ids, single_ids, None)
    try:
        m = await bot.copy_message(cfg.channel_id, origin_chat_id, origin_message_id,
                                   message_thread_id=thread_id,
                                   caption=text or None, parse_mode="HTML" if text else None,
                                   reply_markup=markup)
        return PublishResult([], [m.message_id], m.message_id if markup else None)
    except TelegramBadRequest:
        m = await bot.copy_message(cfg.channel_id, origin_chat_id, origin_message_id,
                                   message_thread_id=thread_id, reply_markup=markup)
        single_ids = [m.message_id]
        buttons_on = m.message_id if markup else None
        if text:
            m2 = await bot.send_message(cfg.channel_id, text, message_thread_id=thread_id)
            single_ids.append(m2.message_id)
        return PublishResult([], single_ids, buttons_on)


async def _publish_reconstructed(bot: Bot, cfg: ChildBot, *, text: str,
                                 file_id: str | None, media_type: str | None,
                                 media_group: list[dict] | None,
                                 markup: InlineKeyboardMarkup | None,
                                 origin_chat_id: int | None = None,
                                 origin_message_id: int | None = None,
                                 origin_message_ids: str | None = None,
                                 thread_id: int | None = None) -> PublishResult:
    if cfg.channel_delivery_mode == "copy" and origin_chat_id \
            and (origin_message_id or origin_message_ids):
        if origin_message_ids:
            ids = [int(x) for x in origin_message_ids.split(",")]
            # Изменение: отключено разделение альбомов на одиночные медиа
            sent_ids = [m.message_id for m in
                       await bot.copy_messages(cfg.channel_id, origin_chat_id, ids,
                                               message_thread_id=thread_id)]
            return PublishResult(sent_ids, [], None)
        else:
            m = await bot.copy_message(cfg.channel_id, origin_chat_id, origin_message_id,
                                       message_thread_id=thread_id, reply_markup=markup)
            return PublishResult([], [m.message_id], m.message_id if markup else None)

    if media_group:
        caption = text if text and len(text) <= CAPTION_LIMIT else None
        # Изменение: публикация строго через MediaGroup
        media_objs = []
        for i, item in enumerate(media_group):
            cls = InputMediaPhoto if item["type"] == "photo" else InputMediaVideo
            kwargs = {"caption": caption, "parse_mode": "HTML"} if i == 0 and caption else {}
            media_objs.append(cls(media=item["file_id"], **kwargs))
        sent_album = list(await bot.send_media_group(cfg.channel_id, media_objs,
                                                      message_thread_id=thread_id))
        album_ids = [m.message_id for m in sent_album]
        single_ids = []
        if text and caption is None:
            m = await bot.send_message(cfg.channel_id, text, message_thread_id=thread_id)
            single_ids.append(m.message_id)
        return PublishResult(album_ids, single_ids, None)

    if file_id and media_type in _MEDIA_SENDERS:
        sender = getattr(bot, _MEDIA_SENDERS[media_type])
        if text and len(text) > CAPTION_LIMIT:
            m = await sender(cfg.channel_id, file_id, message_thread_id=thread_id,
                             reply_markup=markup)
            m2 = await bot.send_message(cfg.channel_id, text, message_thread_id=thread_id)
            return PublishResult([], [m.message_id, m2.message_id],
                                 m.message_id if markup else None)
        m = await sender(cfg.channel_id, file_id, message_thread_id=thread_id,
                         caption=text or None, reply_markup=markup)
        return PublishResult([], [m.message_id], m.message_id if markup else None)
    
    if file_id and media_type == "video_note":
        m = await bot.send_video_note(cfg.channel_id, file_id, message_thread_id=thread_id,
                                      reply_markup=markup)
        single_ids = [m.message_id]
        if text:
            m2 = await bot.send_message(cfg.channel_id, text, message_thread_id=thread_id)
            single_ids.append(m2.message_id)
        return PublishResult([], single_ids, m.message_id if markup else None)
    
    if file_id and media_type == "sticker":
        m = await bot.send_sticker(cfg.channel_id, file_id, message_thread_id=thread_id,
                                   reply_markup=markup)
        single_ids = [m.message_id]
        if text:
            m2 = await bot.send_message(cfg.channel_id, text, message_thread_id=thread_id)
            single_ids.append(m2.message_id)
        return PublishResult([], single_ids, m.message_id if markup else None)

    if not text:
        raise ValueError("пустой пост — нечего публиковать")
    m = await bot.send_message(cfg.channel_id, text, message_thread_id=thread_id,
                               reply_markup=markup)
    return PublishResult([], [m.message_id], m.message_id if markup else None)


class PostSt(StatesGroup):
    composing = State()
    editing = State()
    scheduling = State()
    btn_style = State()
    btn_icon = State()


BTN_STYLES = [
    ("⬜️ Обычная", "-"), ("🟦 Primary", "primary"),
    ("🟩 Success", "success"), ("🟥 Danger", "danger"),
]


def build_posting_router() -> Router:
    r = Router()

    async def _cfg(bot_db_id: int) -> ChildBot:
        return await get_cfg(bot_db_id)

    def _draft_kb(post_id: int, mode: str = "both") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [styled_button("✅ Опубликовать сейчас", callback_data=f"pub:{post_id}")],
            [styled_button("🕒 Отложить", callback_data=f"sched:{post_id}"),
             styled_button("🔗 Кнопки", callback_data=f"editbtn:{post_id}")],
            [styled_button(f"🔘 Источник кнопок: {btn_mode_label(mode)}",
                           callback_data=f"btnmode:{post_id}")],
            [styled_button("🗑 Отмена", callback_data=f"pubdel:{post_id}")],
        ])

    async def _create_draft(msgs: list[Message], bot_db_id: int) -> Post:
        group = _group_from_messages(msgs)
        file_id = media_type = None
        if not group:
            file_id, media_type = _media(msgs[0])
        origin_ids = ",".join(str(mm.message_id) for mm in msgs) if len(msgs) > 1 else None
        async with Session() as s:
            p = Post(bot_id=bot_db_id, author_id=msgs[0].from_user.id,
                     html_text=_text_from_messages(msgs),
                     media_file_id=file_id, media_type=media_type,
                     media_group_json=json.dumps(group) if group else None,
                     origin_chat_id=msgs[0].chat.id,
                     origin_message_id=msgs[0].message_id if len(msgs) == 1 else None,
                     origin_message_ids=origin_ids)
            s.add(p)
            await s.commit()
            await s.refresh(p)
            return p

    async def _preview_then_publish(bot: Bot, cfg: ChildBot, preview_chat_id: int,
                                    thread_id: int | None = None, **publish_kwargs):
        preview_cfg = copy.copy(cfg)
        preview_cfg.channel_id = preview_chat_id
        await bot.send_message(preview_chat_id, f"{em('eyes')} Пост будет выглядеть вот так:",
                               message_thread_id=thread_id)
        result = await publish(bot, preview_cfg, thread_id=thread_id, **publish_kwargs)
        mode = publish_kwargs.get("buttons_mode", "both")
        if mode == "template":
            markup = _buttons_markup(cfg.template_buttons_json)
        elif mode == "custom":
            markup = _buttons_markup(publish_kwargs.get("buttons_json"))
        elif mode == "none":
            markup = None
        else:
            markup = _buttons_markup(publish_kwargs.get("buttons_json"), cfg.template_buttons_json)
        await _publish_via_copy(bot, cfg, preview_chat_id, result, markup)

    async def _publish_post(bot: Bot, cfg: ChildBot, p: Post, preview_chat_id: int | None = None,
                            thread_id: int | None = None):
        group = json.loads(p.media_group_json) if p.media_group_json else None
        kwargs = dict(html_text=p.html_text, file_id=p.media_file_id,
                      media_type=p.media_type, media_group=group,
                      origin_chat_id=p.origin_chat_id, origin_message_id=p.origin_message_id,
                      origin_message_ids=p.origin_message_ids,
                      use_template=(cfg.channel_delivery_mode != "copy"),
                      buttons_json=p.buttons_json,
                      buttons_mode=p.buttons_mode or "both")
        if preview_chat_id is not None:
            await _preview_then_publish(bot, cfg, preview_chat_id, thread_id=thread_id, **kwargs)
        else:
            await publish(bot, cfg, **kwargs)

    @r.message(CommandStart(), F.chat.type == "private")
    async def start(m: Message, bot_db_id: int):
        cfg = await _cfg(bot_db_id)
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            await s.commit()
        if not await is_bot_admin(bot_db_id, m.from_user.id) \
                and await mod.is_banned(bot_db_id, m.from_user.id):
            return
        if await is_bot_admin(bot_db_id, m.from_user.id):
            ikb, rkb = await build_keyboards(bot_db_id, cfg)
            await send_with_keyboards(
                m, f"{em('crown')} Вы админ. Обычные сообщения работают как у "
                f"подписчика (предложка/чат с админами) — для публикации поста "
                f"используйте /newpost.\nПредложка: "
                f"{'вкл' if cfg.accept_suggestions else 'выкл'}", ikb, rkb)
        elif cfg.accept_suggestions or cfg.admin_chat_id:
            ikb, rkb = await build_keyboards(bot_db_id, cfg)
            welcome = await inject_extras(bot_db_id, cfg.welcome_text)
            await send_with_keyboards(m, welcome, ikb, rkb, photo=cfg.welcome_photo)
        else:
            await m.answer("Бот не принимает сообщения.")

    @r.message(Command("cancel"), F.chat.type == "private")
    async def cancel_any(m: Message, state: FSMContext):
        if await state.get_state() is not None or (await state.get_data()):
            await state.clear()
            await m.answer(f"{em('check')} Отменено.")

    @r.message(Command("newpost"), F.chat.type == "private")
    async def newpost_cmd(m: Message, bot_db_id: int, state: FSMContext):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        cfg = await _cfg(bot_db_id)
        if not cfg.channel_id:
            await m.answer(f"{em('warn')} Сначала укажите канал в настройках бота.")
            return
        await state.clear()
        await state.set_state(PostSt.composing)
        await m.answer(f"{em('pencil')} Пришлите пост (текст/фото/видео/альбом, "
                       "форматирование сохранится) — дальше предложу опубликовать "
                       "сразу или отложенно. Отмена: /cancel")

    def _not_command(m: Message) -> bool:
        return not (m.text and m.text.startswith("/"))

    @r.message(PostSt.composing, F.chat.type == "private", _not_command)
    async def newpost_content(m: Message, bot_db_id: int, state: FSMContext):
        async def _process(msgs: list[Message]):
            await state.clear()
            p = await _create_draft(msgs, bot_db_id)
            await m.answer("Черновик поста создан. Что дальше?",
                           reply_markup=_draft_kb(p.id, p.buttons_mode or "both"))
        await buffer_or_process(m, _process)

    @r.message(PostSt.editing, F.chat.type == "private")
    async def editing_button(m: Message, state: FSMContext):
        if m.text and m.text.strip() == "/done":
            await _finalize_editor(m, state)
            return
        if not m.text or "|" not in m.text:
            await m.answer(f"{em('warn')} Формат: <code>Текст кнопки | https://ссылка</code>\n"
                           "Или отправьте /done чтобы закончить.")
            return
        text, url = [p.strip() for p in m.text.split("|", 1)]
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
            await m.answer(f"{em('warn')} Ссылка должна начинаться с http(s):// или tg://")
            return
        await state.update_data(pending_text=text[:64], pending_url=url)
        await state.set_state(PostSt.btn_style)
        await m.answer(
            f"{em('sparkles')} Выберите цвет кнопки (Bot API 9.4):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t, callback_data=f"pbtnstyle:{v}")]
                for t, v in BTN_STYLES]))

    @r.message(PostSt.btn_style, F.chat.type == "private")
    async def post_btn_style_msg_fallback(m: Message, state: FSMContext):
        if m.text and m.text.strip() == "/done":
            await state.set_state(PostSt.editing)
            await _finalize_editor(m, state)
            return
        await m.answer(f"{em('warn')} Выберите цвет кнопкой ниже, или /done чтобы закончить.")

    @r.callback_query(PostSt.btn_style, F.data.startswith("pbtnstyle:"))
    async def post_btn_style(c: CallbackQuery, state: FSMContext):
        style = c.data.split(":", 1)[1]
        await state.update_data(pending_style=None if style == "-" else style)
        await state.set_state(PostSt.btn_icon)
        await c.message.edit_text(
            f"{em('sparkles')} Пришлите premium-эмодзи для кнопки (просто отправьте "
            "его как текст), или «-» чтобы пропустить.")
        await c.answer()

    @r.message(PostSt.btn_icon, F.chat.type == "private")
    async def post_btn_icon(m: Message, state: FSMContext):
        if m.text and m.text.strip() == "/done":
            data = await state.get_data()
            rows = data.get("buttons", [])
            rows.append([{"text": data["pending_text"], "url": data["pending_url"],
                         "style": data.get("pending_style"), "icon": None}])
            await state.update_data(buttons=rows, pending_text=None, pending_url=None,
                                    pending_style=None)
            await state.set_state(PostSt.editing)
            await _finalize_editor(m, state)
            return
        icon_id = None
        if m.text and m.text.strip() != "-" and m.entities:
            for e in m.entities:
                if e.type == "custom_emoji":
                    icon_id = e.custom_emoji_id
                    break
        data = await state.get_data()
        rows = data.get("buttons", [])
        rows.append([{"text": data["pending_text"], "url": data["pending_url"],
                     "style": data.get("pending_style"), "icon": icon_id}])
        await state.update_data(buttons=rows, pending_text=None, pending_url=None,
                                pending_style=None)
        await state.set_state(PostSt.editing)
        await m.answer(f"{em('check')} Кнопка добавлена ({len(rows)}). Ещё одну, "
                       "или /done чтобы закончить.")

    async def _finalize_editor(m: Message, state: FSMContext):
        data = await state.get_data()
        rows = data.get("buttons", [])
        async with Session() as s:
            p = await s.get(Post, data["post_id"])
            existing = json.loads(p.buttons_json) if p.buttons_json else []
            existing.extend(rows)
            p.buttons_json = json.dumps(existing) if existing else None
            await s.commit()
            post_id, mode = p.id, p.buttons_mode or "both"
        await state.clear()
        await m.answer(f"{em('check')} Кнопки сохранены.", reply_markup=_draft_kb(post_id, mode))

    @r.callback_query(F.data.startswith("editbtn:"))
    async def editbtn(c: CallbackQuery, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            return
        post_id = int(c.data.split(":")[1])
        async with Session() as s:
            p = await s.get(Post, post_id)
        existing = json.loads(p.buttons_json) if p and p.buttons_json else []
        rows = [[styled_button(f"🗑 {b['text']}", callback_data=f"delbtn:{post_id}:{i}")]
               for i, row in enumerate(existing) for b in row]
        rows.append([styled_button("➕ Добавить кнопку", callback_data=f"addbtn:{post_id}")])
        rows.append([styled_button("⬅️ Назад", callback_data=f"backpost:{post_id}")])
        await c.message.answer(
            f"{em('link')} Кнопки поста — нажмите чтобы удалить, или добавьте новую:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await c.answer()

    @r.callback_query(F.data.startswith("addbtn:"))
    async def addbtn(c: CallbackQuery, state: FSMContext, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            return
        post_id = int(c.data.split(":")[1])
        await state.set_state(PostSt.editing)
        await state.update_data(post_id=post_id, buttons=[])
        await c.message.edit_text(
            f"{em('link')} Присылайте кнопки по одной в формате:\n"
            "<code>Текст кнопки | https://ссылка</code>\n"
            "Когда закончите — отправьте /done")
        await c.answer()

    @r.callback_query(F.data.startswith("delbtn:"))
    async def delbtn(c: CallbackQuery, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            return
        _, post_id, idx = c.data.split(":")
        post_id, idx = int(post_id), int(idx)
        async with Session() as s:
            p = await s.get(Post, post_id)
            existing = json.loads(p.buttons_json) if p.buttons_json else []
            flat = [b for row in existing for b in row]
            if 0 <= idx < len(flat):
                flat.pop(idx)
            p.buttons_json = json.dumps([[b] for b in flat]) if flat else None
            await s.commit()
        c_new = c.model_copy(update={"data": f"editbtn:{post_id}"})
        await editbtn(c_new, bot_db_id)

    @r.callback_query(F.data.startswith("btnmode:"))
    async def btnmode(c: CallbackQuery, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            return
        post_id = int(c.data.split(":")[1])
        modes = [m for m, _ in BTN_MODES]
        async with Session() as s:
            p = await s.get(Post, post_id)
            if not p:
                await c.answer("Черновик не найден", show_alert=True)
                return
            cur = p.buttons_mode or "both"
            p.buttons_mode = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else "both"
            new_mode = p.buttons_mode
            await s.commit()
        try:
            await c.message.edit_reply_markup(reply_markup=_draft_kb(post_id, new_mode))
        except Exception:
            pass
        await c.answer(f"Кнопки: {btn_mode_label(new_mode)}")

    @r.callback_query(F.data.startswith("backpost:"))
    async def backpost(c: CallbackQuery, bot_db_id: int):
        post_id = int(c.data.split(":")[1])
        async with Session() as s:
            p = await s.get(Post, post_id)
        mode = (p.buttons_mode or "both") if p else "both"
        await c.message.edit_text("Черновик поста. Что дальше?",
                                  reply_markup=_draft_kb(post_id, mode))
        await c.answer()

    @r.callback_query(F.data.startswith("sched:"))
    async def sched(c: CallbackQuery, state: FSMContext, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            return
        post_id = int(c.data.split(":")[1])
        await state.set_state(PostSt.scheduling)
        await state.update_data(post_id=post_id)
        await c.message.answer(f"{em('calendar')} Когда опубликовать? Формат: "
                               "<code>31.12.2026 18:30</code> (время UTC). Отмена: /cancel")
        await c.answer()

    @r.message(PostSt.scheduling, F.chat.type == "private")
    async def scheduling_time(m: Message, state: FSMContext):
        try:
            dt = datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M")
        except (ValueError, AttributeError):
            await m.answer(f"{em('warn')} Формат: <code>31.12.2026 18:30</code>. "
                           "Попробуйте снова или /cancel.")
            return
        if dt <= datetime.utcnow():
            await m.answer(f"{em('warn')} Время должно быть в будущем.")
            return
        data = await state.get_data()
        async with Session() as s:
            p = await s.get(Post, data["post_id"])
            p.publish_at = dt
            await s.commit()
        await state.clear()
        await m.answer(f"{em('calendar')} Пост запланирован на {dt.strftime('%d.%m.%Y %H:%M')} UTC.")

    @r.callback_query(F.data.startswith("pub:"))
    async def pub(c: CallbackQuery, bot: Bot, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            return
        post_id = int(c.data.split(":")[1])
        async with Session() as s:
            cfg = await s.get(ChildBot, bot_db_id)
            res = await s.execute(
                sa_update(Post).where(Post.id == post_id, Post.published.is_(False))
                .values(published=True))
            await s.commit()
            if res.rowcount == 0:
                await c.answer("Уже опубликовано"); return
            p = await s.get(Post, post_id)
            if not cfg.channel_id:
                await c.answer("Не задан канал в настройках бота!", show_alert=True)
                async with Session() as s2:
                    await s2.execute(sa_update(Post).where(Post.id == post_id)
                                     .values(published=False))
                    await s2.commit()
                return
        try:
            await _publish_post(bot, cfg, p, preview_chat_id=c.message.chat.id,
                                thread_id=c.message.message_thread_id)
        except Exception as e:
            async with Session() as s:
                await s.execute(sa_update(Post).where(Post.id == post_id)
                               .values(published=False))
                await s.commit()
            await c.message.answer(f"{em('cross')} Не удалось опубликовать: {e}")
            await c.answer()
            return
        await c.message.edit_text(f"{em('check')} Опубликовано!")
        await c.answer()

    @r.callback_query(F.data.startswith("pubdel:"))
    async def pubdel(c: CallbackQuery, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            return
        async with Session() as s:
            p = await s.get(Post, int(c.data.split(":")[1]))
            if p and not p.published:
                await s.delete(p)
                await s.commit()
        await c.message.edit_text("🗑 Отменено")
        await c.answer()

    @r.message(F.chat.type == "private")
    async def incoming(m: Message, bot: Bot, bot_db_id: int, state: FSMContext):
        cfg = await _cfg(bot_db_id)
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id, direction="in"))
            await s.commit()
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        
        if await should_apply_antispam(bot_db_id, cfg, m.from_user.id):
            res = await antispam.check(bot_db_id, cfg, m.from_user.id, m.text)
            if not res.allowed:
                if res.notice:
                    await m.answer(res.notice)
                return
        
        if await state.get_state() is not None:
            return
        if await handle_keyboard_button(m, bot_db_id):
            return
        
        if m.text and m.text.startswith("/"):
            return
        if not cfg.admin_chat_id:
            return

        async def _process(msgs: list[Message]):
            await _relay_to_admins(msgs, bot, cfg, bot_db_id)

        await buffer_or_process(m, _process)

    async def _relay_to_admins(msgs: list[Message], bot: Bot, cfg: ChildBot, bot_db_id: int):
        user = msgs[0].from_user
        sugg_kb = None
        if cfg.accept_suggestions:
            is_album = len(msgs) > 1
            group = _group_from_messages(msgs)
            file_id = media_type = None
            if not group:
                file_id, media_type = _media(msgs[0])
            origin_ids = ",".join(str(mm.message_id) for mm in msgs) if is_album else None
            async with Session() as s:
                sg = Suggestion(bot_id=bot_db_id, user_id=user.id,
                                html_text=_text_from_messages(msgs),
                                media_file_id=file_id, media_type=media_type,
                                media_group_json=json.dumps(group) if group else None,
                                origin_chat_id=msgs[0].chat.id,
                                origin_message_id=msgs[0].message_id if not is_album else None,
                                origin_message_ids=origin_ids)
                s.add(sg)
                await s.commit()
                await s.refresh(sg)
            sugg_kb = InlineKeyboardMarkup(inline_keyboard=[[
                styled_button("✅ Принять", callback_data=f"sg_ok:{sg.id}"),
                styled_button("❌ Отклонить", callback_data=f"sg_no:{sg.id}")]])
        
        await relay_to_admin_chat(msgs, bot, cfg, extra_kb=sugg_kb)

    @r.callback_query(F.data.startswith(("sg_ok:", "sg_no:")))
    async def decide(c: CallbackQuery, bot: Bot, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            await c.answer("Нет доступа", show_alert=True); return
        sg_id = int(c.data.split(":")[1])
        approve = c.data.startswith("sg_ok")
        
        async with Session() as s:
            res = await s.execute(
                sa_update(Suggestion).where(Suggestion.id == sg_id, Suggestion.status == "pending")
                .values(status="approved" if approve else "rejected",
                       decided_by=c.from_user.id, decided_by_username=c.from_user.username,
                       decided_at=datetime.utcnow()))
            await s.commit()
            if res.rowcount == 0:
                await c.answer("Уже обработано"); return
            cfg = await s.get(ChildBot, bot_db_id)
            sg = await s.get(Suggestion, sg_id)
        if approve:
            if not cfg.channel_id:
                await c.answer("Не задан канал в настройках бота!", show_alert=True)
                return
            try:
                group = json.loads(sg.media_group_json) if sg.media_group_json else None
                pub_kwargs = dict(html_text=sg.html_text, file_id=sg.media_file_id,
                                  media_type=sg.media_type, media_group=group,
                                  origin_chat_id=sg.origin_chat_id,
                                  origin_message_id=sg.origin_message_id,
                                  origin_message_ids=sg.origin_message_ids,
                                  use_template=(cfg.channel_delivery_mode != "copy"))
                
                await _preview_then_publish(bot, cfg, c.message.chat.id,
                                            thread_id=c.message.message_thread_id, **pub_kwargs)
            except Exception as e:
                await c.message.answer(f"{em('cross')} Не удалось опубликовать: {e}")
                await c.answer()
                return
            try:
                await bot.send_message(sg.user_id, f"{em('party')} Ваш пост опубликован!")
            except Exception:
                pass
        else:
            try:
                await bot.send_message(sg.user_id, f"{em('cross')} Ваш пост отклонён.")
            except Exception:
                pass
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await c.answer("✅ Готово")

    return r
