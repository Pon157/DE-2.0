import asyncio
import json
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InputMediaPhoto, InputMediaVideo)
from sqlalchemy import select
from db.base import Session
from db.models import ChildBot, Suggestion, Post, MessageLog, Ticket, MsgMap
from services import moderation as mod
from child.common import (is_bot_admin, inject_extras, build_keyboards, send_with_keyboards,
                          handle_keyboard_button, open_ticket)
from utils.emoji import em, styled_button

ALBUM_DEBOUNCE = 0.8  # секунд ждём остальные части альбома, прежде чем обработать


def _media(m: Message):
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
    return None, None


# =========================================================================
# Альбомы (media group). Telegram присылает каждое фото/видео альбома ОТДЕЛЬНЫМ
# апдейтом с общим media_group_id — раньше бот обрабатывал их как N разных
# постов/предложек по отдельности вместо ОДНОГО поста с несколькими медиа
# ("медиа нигде не группируется"). Копим сообщения группы и обрабатываем всей
# пачкой через короткую паузу после последнего пришедшего сообщения группы.
# =========================================================================
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


def _group_from_messages(msgs: list[Message]) -> list[dict] | None:
    if len(msgs) < 2:
        return None
    items = []
    for mm in msgs:
        fid, mtype = _media(mm)
        if fid and mtype in ("photo", "video"):
            items.append({"file_id": fid, "type": mtype})
    return items or None


def _text_from_messages(msgs: list[Message]) -> str:
    for mm in msgs:
        if mm.html_text:
            return mm.html_text
    return ""


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


async def publish(bot: Bot, cfg: ChildBot, *, html_text: str = "",
                  file_id: str | None = None, media_type: str | None = None,
                  media_group: list[dict] | None = None,
                  origin_chat_id: int | None = None,
                  origin_message_id: int | None = None,
                  origin_message_ids: str | None = None,
                  use_template: bool = True, buttons_json: str | None = None):
    """Публикует пост в канал. Бросает исключение при ошибке Telegram —
    вызывающий код обязан её поймать и сообщить об этом человеку. РАНЬШЕ:
    если ссылка в кнопке была невалидной, Telegram отказывал, ошибка
    попадала только в логи контейнера, а пользователь/админ просто видел
    тишину и не понимал, что пошло не так."""
    markup = _buttons_markup(buttons_json, cfg.template_buttons_json)
    text = cfg.post_template.replace("{text}", html_text) if use_template else html_text

    # --- режим "оригинал" (copy_message/copy_messages) ---
    # Копирует сообщение(я) НАПРЯМУЮ через Telegram API — формат/энтити/медиа
    # сохраняются как есть, без ручной пересборки текста через html_text.
    if cfg.channel_delivery_mode == "copy" and origin_chat_id and (origin_message_id or origin_message_ids):
        if origin_message_ids:
            ids = [int(x) for x in origin_message_ids.split(",")]
            await bot.copy_messages(cfg.channel_id, origin_chat_id, ids)
            if markup:
                # copy_messages (альбом) в принципе не поддерживает reply_markup —
                # это ограничение самого Bot API. Кнопки шлём отдельным
                # сообщением сразу под альбомом.
                await bot.send_message(cfg.channel_id, "🔘", reply_markup=markup)
        else:
            await bot.copy_message(cfg.channel_id, origin_chat_id, origin_message_id,
                                   reply_markup=markup)
        return

    # --- режим "по шаблону" (реконструкция из html_text) ---
    if media_group:
        media_objs = []
        for i, item in enumerate(media_group):
            cls = InputMediaPhoto if item["type"] == "photo" else InputMediaVideo
            kwargs = {"caption": text, "parse_mode": "HTML"} if i == 0 and text else {}
            media_objs.append(cls(media=item["file_id"], **kwargs))
        await bot.send_media_group(cfg.channel_id, media_objs)
        if markup:
            await bot.send_message(cfg.channel_id, "🔘", reply_markup=markup)
        return

    if file_id and media_type == "photo":
        await bot.send_photo(cfg.channel_id, file_id, caption=text, reply_markup=markup)
    elif file_id and media_type == "video":
        await bot.send_video(cfg.channel_id, file_id, caption=text, reply_markup=markup)
    elif file_id and media_type == "animation":
        await bot.send_animation(cfg.channel_id, file_id, caption=text, reply_markup=markup)
    elif file_id and media_type == "audio":
        await bot.send_audio(cfg.channel_id, file_id, caption=text, reply_markup=markup)
    elif file_id:
        await bot.send_document(cfg.channel_id, file_id, caption=text, reply_markup=markup)
    else:
        await bot.send_message(cfg.channel_id, text, reply_markup=markup)


class PostSt(StatesGroup):
    editing = State()      # /newpost: ждём кнопку "текст|url" или /done
    scheduling = State()   # /newpost: ждём дату-время


def build_posting_router() -> Router:
    r = Router()

    async def _cfg(bot_db_id: int) -> ChildBot:
        async with Session() as s:
            return await s.get(ChildBot, bot_db_id)

    def _draft_kb(post_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [styled_button("✅ Опубликовать сейчас", callback_data=f"pub:{post_id}")],
            [styled_button("🕒 Отложить", callback_data=f"sched:{post_id}"),
             styled_button("🔗 Кнопки", callback_data=f"editbtn:{post_id}")],
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
        # БАГ: раньше здесь было жёстко use_template=False — шаблон поста,
        # настроенный владельцем, для постов из /newpost НЕ применялся
        # никогда, а для одобренных предложок (decide() ниже) применялся
        # ВСЕГДА, независимо от режима пересылки в канал. Из-за этого
        # поведение "шаблон/нет" зависело от того, откуда пришёл пост, а не
        # от настройки "📬 Публикация в канал" (copy/template). Теперь оба
        # места решают одинаково — по cfg.channel_delivery_mode.
        await publish(bot, cfg, html_text=p.html_text, file_id=p.media_file_id,
                      media_type=p.media_type, media_group=group,
                      origin_chat_id=p.origin_chat_id, origin_message_id=p.origin_message_id,
                      origin_message_ids=p.origin_message_ids,
                      use_template=(cfg.channel_delivery_mode != "copy"),
                      buttons_json=p.buttons_json)

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
    # РАНЬШЕ: любое сообщение от админа автоматически становилось черновиком
    # поста, а /suggest криво переключал его "в режим обычного юзера" через
    # сырой флаг без нормального состояния (мог теряться/путаться с другим
    # вводом). ТЕПЕРЬ: админ по умолчанию ведёт себя как обычный подписчик
    # (сообщения идут в предложку/чат с админами), и только явный /newpost
    # переводит следующее сообщение (текст/медиа/альбом) в создание поста.
    @r.message(Command("newpost"), F.chat.type == "private")
    async def newpost_cmd(m: Message, bot_db_id: int, state: FSMContext):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        cfg = await _cfg(bot_db_id)
        if not cfg.channel_id:
            await m.answer(f"{em('warn')} Сначала укажите канал в настройках бота.")
            return
        await state.update_data(newpost_pending=True)
        await m.answer(f"{em('pencil')} Пришлите пост (текст/фото/видео/альбом, "
                       "форматирование сохранится) — дальше предложу опубликовать "
                       "сразу или отложенно.")

    # -------- /newpost: ожидание содержимого (включая альбомы) --------
    def _not_command(m: Message) -> bool:
        return not (m.text and m.text.startswith("/"))

    @r.message(F.chat.type == "private", StateFilter(None), _not_command)
    async def maybe_newpost_content(m: Message, bot_db_id: int, state: FSMContext):
        data = await state.get_data()
        if not data.get("newpost_pending") or not await is_bot_admin(bot_db_id, m.from_user.id):
            raise SkipHandler  # не наш случай — уходит в incoming() ниже

        async def _process(msgs: list[Message]):
            await state.update_data(newpost_pending=False)
            p = await _create_draft(msgs, bot_db_id)
            await m.answer("Черновик поста создан. Что дальше?",
                           reply_markup=_draft_kb(p.id))

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
        if not (url.startswith("http://") or url.startswith("https://")):
            await m.answer(f"{em('warn')} Ссылка должна начинаться с http(s)://")
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
            post_id = p.id
        await state.clear()
        await m.answer(f"{em('check')} Кнопки сохранены.", reply_markup=_draft_kb(post_id))

    @r.callback_query(F.data.startswith("editbtn:"))
    async def editbtn(c: CallbackQuery, state: FSMContext, bot_db_id: int):
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
        # БАГ (недостающая фича): раньше кнопки на посте можно было только
        # добавлять, удалить уже добавленную было нельзя вообще.
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
        await editbtn(c_new, None, bot_db_id)

    @r.callback_query(F.data.startswith("backpost:"))
    async def backpost(c: CallbackQuery, bot_db_id: int):
        post_id = int(c.data.split(":")[1])
        await c.message.edit_text("Черновик поста. Что дальше?", reply_markup=_draft_kb(post_id))
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
            # БАГ: раньше ошибка публикации (например, невалидная ссылка в
            # кнопке) уходила молча в логи контейнера — админ просто видел
            # тишину. Теперь причина явно показывается в чате.
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
    async def incoming(m: Message, bot: Bot, bot_db_id: int):
        cfg = await _cfg(bot_db_id)
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id, direction="in"))
            await s.commit()
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        if await handle_keyboard_button(m, bot_db_id):
            return
        if not cfg.admin_chat_id:
            return

        async def _process(msgs: list[Message]):
            await _relay_to_admins(msgs, bot, cfg, bot_db_id)

        await buffer_or_process(m, _process)

    async def _relay_to_admins(msgs: list[Message], bot: Bot, cfg: ChildBot, bot_db_id: int):
        user = msgs[0].from_user
        # Тикет/топик — та же система двусторонней переписки, что и в
        # фидбек-ботах: раньше в постинг-ботах не было вообще никакой
        # возможности админу что-то ответить пользователю, только принять/
        # отклонить предложку.
        ticket = await open_ticket(bot, cfg, user.id)
        thread = ticket.topic_id if cfg.use_topics else None

        is_album = len(msgs) > 1
        sugg_kb = None
        if cfg.accept_suggestions:
            # создаём заявку на публикацию сразу, кнопки решения вешаем на
            # релей в чат админов
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

        header = (f"{em('envelope')} {user.full_name} (@{user.username or '—'}, "
                 f"<code>{user.id}</code>)")
        await bot.send_message(cfg.admin_chat_id, header, message_thread_id=thread)
        if is_album:
            ids = [mm.message_id for mm in msgs]
            copies = await bot.copy_messages(cfg.admin_chat_id, msgs[0].chat.id, ids,
                                             message_thread_id=thread)
            last_msg_id = copies[-1].message_id
        else:
            cp = await bot.copy_message(cfg.admin_chat_id, msgs[0].chat.id, msgs[0].message_id,
                                        message_thread_id=thread, reply_markup=sugg_kb)
            last_msg_id = cp.message_id
        async with Session() as s:
            s.add(MsgMap(bot_id=bot_db_id, admin_chat_msg_id=last_msg_id, user_id=user.id))
            await s.commit()
        if is_album and sugg_kb:
            # У копий альбома нет reply_markup (ограничение Bot API) — кнопки
            # решения шлём отдельным сообщением сразу под альбомом.
            await bot.send_message(cfg.admin_chat_id, "🔘 Что делаем с предложкой?",
                                   message_thread_id=thread, reply_markup=sugg_kb)

    # ================= админ-чат: ответы (двусторонний чат) =================
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
                # см. комментарий в _publish_post() выше — use_template теперь
                # зависит только от режима пересылки, а не от источника поста.
                await publish(bot, cfg, html_text=sg.html_text, file_id=sg.media_file_id,
                             media_type=sg.media_type, media_group=group,
                             origin_chat_id=sg.origin_chat_id,
                             origin_message_id=sg.origin_message_id,
                             origin_message_ids=sg.origin_message_ids,
                             use_template=(cfg.channel_delivery_mode != "copy"))
            except Exception as e:
                # БАГ: раньше ошибка публикации утекала только в логи, ни
                # админ, ни автор поста об этом не узнавали.
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
