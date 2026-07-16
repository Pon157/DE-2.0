import json
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InputMediaPhoto, InputMediaVideo)
from sqlalchemy import select
from db.base import Session
from db.models import ChildBot, Suggestion, Post, MessageLog
from services import moderation as mod
from child.common import (is_bot_admin, inject_extras, build_keyboards, send_with_keyboards,
                          handle_keyboard_button, buffer_or_process, message_media,
                          group_from_messages, text_from_messages, relay_to_admin_chat,
                          get_cfg)
from utils.emoji import em, styled_button


def _media(m: Message):
    # алиас обратной совместимости — вся логика в child/common.py
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
        [styled_button(b["text"], url=b["url"]) for b in row] for row in rows])


# Источники кнопок поста (переключатель в редакторе черновика)
BTN_MODES = [("both", "шаблон + свои"), ("template", "только из шаблона"),
             ("custom", "только свои"), ("none", "без кнопок")]


def btn_mode_label(mode: str | None) -> str:
    return dict(BTN_MODES).get(mode or "both", "шаблон + свои")


_MEDIA_SENDERS = {
    "photo": "send_photo", "video": "send_video", "animation": "send_animation",
    "audio": "send_audio", "document": "send_document", "voice": "send_voice",
}

CAPTION_LIMIT = 1024


async def publish(bot: Bot, cfg: ChildBot, *, html_text: str = "",
                  file_id: str | None = None, media_type: str | None = None,
                  media_group: list[dict] | None = None,
                  origin_chat_id: int | None = None,
                  origin_message_id: int | None = None,
                  origin_message_ids: str | None = None,
                  use_template: bool = True, buttons_json: str | None = None,
                  buttons_mode: str = "both"):
    """Публикует пост в канал. Бросает исключение при ошибке Telegram —
    вызывающий код обязан её поймать и сообщить человеку.

    buttons_mode: откуда брать кнопки — "both" (шаблон+свои), "template"
    (только кнопки шаблона), "custom" (только кнопки поста), "none" (без).
    Раньше кнопки шаблона лепились к посту ВСЕГДА, выбора не было.
    """
    if buttons_mode == "template":
        markup = _buttons_markup(cfg.template_buttons_json)
    elif buttons_mode == "custom":
        markup = _buttons_markup(buttons_json)
    elif buttons_mode == "none":
        markup = None
    else:  # both
        markup = _buttons_markup(buttons_json, cfg.template_buttons_json)
    text = cfg.post_template.replace("{text}", html_text) if use_template else html_text

    if not text and not file_id and not media_group \
            and not (origin_message_id or origin_message_ids):
        # БАГ: пустой пост (например, стикер без текста в template-режиме
        # раньше) падал глубоко в Telegram с "message text is empty".
        raise ValueError("пустой пост — нечего публиковать")

    # --- режим "оригинал" (copy_message/copy_messages) ---
    if cfg.channel_delivery_mode == "copy" and origin_chat_id \
            and (origin_message_id or origin_message_ids):
        if origin_message_ids:
            ids = [int(x) for x in origin_message_ids.split(",")]
            await bot.copy_messages(cfg.channel_id, origin_chat_id, ids)
            if markup:
                # copy_messages (альбом) не поддерживает reply_markup —
                # ограничение Bot API. Кнопки шлём отдельным сообщением.
                await bot.send_message(cfg.channel_id, "🔘", reply_markup=markup)
        else:
            await bot.copy_message(cfg.channel_id, origin_chat_id, origin_message_id,
                                   reply_markup=markup)
        return

    # --- режим "по шаблону" (реконструкция из html_text) ---
    if media_group:
        caption = text if text and len(text) <= CAPTION_LIMIT else None
        media_objs = []
        for i, item in enumerate(media_group):
            cls = InputMediaPhoto if item["type"] == "photo" else InputMediaVideo
            kwargs = {"caption": caption, "parse_mode": "HTML"} if i == 0 and caption else {}
            media_objs.append(cls(media=item["file_id"], **kwargs))
        await bot.send_media_group(cfg.channel_id, media_objs)
        if text and caption is None:
            # БАГ: текст длиннее 1024 в caption альбома раньше ронял публикацию
            await bot.send_message(cfg.channel_id, text)
        if markup:
            await bot.send_message(cfg.channel_id, "🔘", reply_markup=markup)
        return

    async def _send_text_or_buttons():
        # для типов без caption (кружок/стикер): текст и кнопки отдельным сообщением
        if text:
            await bot.send_message(cfg.channel_id, text, reply_markup=markup)
        elif markup:
            await bot.send_message(cfg.channel_id, "🔘", reply_markup=markup)

    if file_id and media_type in _MEDIA_SENDERS:
        sender = getattr(bot, _MEDIA_SENDERS[media_type])
        if text and len(text) > CAPTION_LIMIT:
            # БАГ: caption > 1024 -> TelegramBadRequest, пост молча не публиковался
            await sender(cfg.channel_id, file_id)
            await bot.send_message(cfg.channel_id, text, reply_markup=markup)
            return
        await sender(cfg.channel_id, file_id, caption=text or None, reply_markup=markup)
        return
    if file_id and media_type == "video_note":
        await bot.send_video_note(cfg.channel_id, file_id)
        await _send_text_or_buttons()
        return
    if file_id and media_type == "sticker":
        await bot.send_sticker(cfg.channel_id, file_id)
        await _send_text_or_buttons()
        return

    if not text:
        raise ValueError("пустой пост — нечего публиковать")
    await bot.send_message(cfg.channel_id, text, reply_markup=markup)


class PostSt(StatesGroup):
    composing = State()    # /newpost: ждём содержимое поста
    editing = State()      # /newpost: ждём кнопку "текст|url" или /done
    scheduling = State()   # /newpost: ждём дату-время


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

    async def _publish_post(bot: Bot, cfg: ChildBot, p: Post):
        group = json.loads(p.media_group_json) if p.media_group_json else None
        await publish(bot, cfg, html_text=p.html_text, file_id=p.media_file_id,
                      media_type=p.media_type, media_group=group,
                      origin_chat_id=p.origin_chat_id, origin_message_id=p.origin_message_id,
                      origin_message_ids=p.origin_message_ids,
                      use_template=(cfg.channel_delivery_mode != "copy"),
                      buttons_json=p.buttons_json,
                      buttons_mode=p.buttons_mode or "both")

    # ================= /start =================
    @r.message(CommandStart(), F.chat.type == "private")
    async def start(m: Message, bot_db_id: int):
        cfg = await _cfg(bot_db_id)
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            await s.commit()
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

    # ================= /newpost — единственный способ опубликовать пост =================
    @r.message(Command("newpost"), F.chat.type == "private")
    async def newpost_cmd(m: Message, bot_db_id: int, state: FSMContext):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        cfg = await _cfg(bot_db_id)
        if not cfg.channel_id:
            await m.answer(f"{em('warn')} Сначала укажите канал в настройках бота.")
            return
        # БАГ (главный по репорту "newpost шлёт мои сообщения в чат админов"):
        # ожидание поста хранилось флагом newpost_pending В ДАННЫХ при пустом
        # состоянии — сообщение при определённых условиях проваливалось мимо
        # фильтра StateFilter(None) и улетало в incoming() -> релей в чат
        # админов. Теперь это ЯВНОЕ FSM-состояние PostSt.composing: пока оно
        # активно, содержимое поста точно не уйдёт в предложку.
        await state.clear()
        await state.set_state(PostSt.composing)
        await m.answer(f"{em('pencil')} Пришлите пост (текст/фото/видео/альбом, "
                       "форматирование сохранится) — дальше предложу опубликовать "
                       "сразу или отложенно. Отмена: /cancel")

    # -------- /newpost: ожидание содержимого (включая альбомы) --------
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

    # -------- /newpost: кнопки на черновике --------
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
        data = await state.get_data()
        rows = data.get("buttons", [])
        rows.append([{"text": text[:64], "url": url}])
        await state.update_data(buttons=rows)
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

    # -------- переключатель источника кнопок: шаблон / свои / оба / без --------
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
        async with Session() as s:
            cfg = await s.get(ChildBot, bot_db_id)
            p = await s.get(Post, int(c.data.split(":")[1]))
            if not p or p.published:
                await c.answer("Уже опубликовано"); return
            if not cfg.channel_id:
                await c.answer("Не задан канал в настройках бота!", show_alert=True); return
        try:
            await _publish_post(bot, cfg, p)
        except Exception as e:
            await c.message.answer(f"{em('cross')} Не удалось опубликовать: {e}")
            await c.answer()
            return
        async with Session() as s:
            obj = await s.get(Post, p.id)
            obj.published = True
            await s.commit()
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

    # ================= обычные сообщения (подписчики И админы вне /newpost) =================
    @r.message(F.chat.type == "private")
    async def incoming(m: Message, bot: Bot, bot_db_id: int, state: FSMContext):
        cfg = await _cfg(bot_db_id)
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id, direction="in"))
            await s.commit()
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        # Идёт FSM-ввод (/newpost, кнопки, расписание, донат) — НЕ релеим.
        # Это страховка от бага "сообщения улетают в чат админов посреди
        # диалога" — сюда такие сообщения доходить не должны вообще.
        if await state.get_state() is not None:
            return
        if await handle_keyboard_button(m, bot_db_id):
            return
        # Неизвестные команды — не предложка, в админ-чат не релеим.
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
            # создаём заявку на публикацию сразу, кнопки решения вешаем на
            # релей в чат админов
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
        # Сам релей (шапка/топики/reply-контекст/маппинг/кнопка закрытия) —
        # общий с фидбек-ботами, см. child/common.py::relay_to_admin_chat.
        await relay_to_admin_chat(msgs, bot, cfg, extra_kb=sugg_kb)

    # ================= модерация предложки =================
    @r.callback_query(F.data.startswith(("sg_ok:", "sg_no:")))
    async def decide(c: CallbackQuery, bot: Bot, bot_db_id: int):
        if not await is_bot_admin(bot_db_id, c.from_user.id):
            await c.answer("Нет доступа", show_alert=True); return
        sg_id = int(c.data.split(":")[1])
        approve = c.data.startswith("sg_ok")
        async with Session() as s:
            cfg = await s.get(ChildBot, bot_db_id)
            sg = await s.get(Suggestion, sg_id)
            if not sg or sg.status != "pending":
                await c.answer("Уже обработано"); return
            sg.status = "approved" if approve else "rejected"
            sg.decided_by, sg.decided_by_username = c.from_user.id, c.from_user.username
            sg.decided_at = datetime.utcnow()
            await s.commit()
        if approve:
            if not cfg.channel_id:
                await c.answer("Не задан канал в настройках бота!", show_alert=True)
                return
            try:
                group = json.loads(sg.media_group_json) if sg.media_group_json else None
                await publish(bot, cfg, html_text=sg.html_text, file_id=sg.media_file_id,
                             media_type=sg.media_type, media_group=group,
                             origin_chat_id=sg.origin_chat_id,
                             origin_message_id=sg.origin_message_id,
                             origin_message_ids=sg.origin_message_ids,
                             use_template=(cfg.channel_delivery_mode != "copy"))
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
