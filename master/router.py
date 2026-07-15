from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, BufferedInputFile)
from aiogram.utils.token import validate_token
from sqlalchemy import select
from db.base import Session
from db.models import (ChildBot, BotAdmin, BotButton, BotType, OpenMode, ForwardMode,
                       Advertisement, AdKind)
from services.bot_manager import manager
from services.broadcast import run_broadcast
from services.stats_image import build_stats_image
from services import ads as ads_service
from services import payments as pay_service
from services import referrals
from services import moderation as mod
from utils.emoji import em, styled_button
import config
import json
from config import SUPER_ADMIN_ID, MASTER_BOT_TOKEN, AD_MAX_LEN, AD_BROADCAST_COOLDOWN_DAYS

router = Router()


class St(StatesGroup):
    add_token = State()
    add_type = State()
    bc_content = State()
    bc_target = State()
    set_welcome = State()
    set_admin_chat = State()
    set_header = State()
    set_template = State()
    add_admin = State()
    btn_kind = State()
    btn_text = State()
    btn_url = State()
    btn_response = State()
    btn_style = State()
    btn_icon = State()
    set_warn_limit = State()
    ad_reject_reason = State()
    ap_bc_content = State()
    # /ads (покупка рекламы) — теперь работает в МАСТЕР-боте, а не в дочерних
    ads_pick_kind = State()
    ads_pick_bot = State()
    ads_text = State()
    ads_impr_custom = State()
    ads_confirm = State()
    ticket_btn_text = State()
    ticket_btn_style = State()
    ticket_btn_icon = State()
    tpl_btn_edit = State()


def kb(rows):
    def _btn(item):
        if len(item) == 3:
            text, _, url = item
            return styled_button(text, url=url)
        text, data = item
        return styled_button(text, callback_data=data)
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(item) for item in row] for row in rows])


def capture_media(m: Message):
    """Достаёт (file_id, media_type) из сообщения — поддержка ВСЕХ основных
    типов медиа для рассылок (раньше поддерживались только photo/video/
    animation/document, из-за чего голосовые/аудио/кружки/стикеры в рассылке
    просто терялись)."""
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


async def _access(bot_id: int, user_id: int) -> tuple[ChildBot | None, bool]:
    """(бот, is_owner). None если нет доступа вообще."""
    async with Session() as s:
        cb = await s.get(ChildBot, bot_id)
        if not cb:
            return None, False
        if cb.owner_id == user_id or (SUPER_ADMIN_ID and user_id == SUPER_ADMIN_ID):
            # Супер-админ платформы получает полный доступ к любому боту —
            # нужно для админ-панели (вкл/выкл, статистика, настройки).
            return cb, True
        adm = await s.scalar(select(BotAdmin).where(
            BotAdmin.bot_id == bot_id, BotAdmin.user_id == user_id))
        return (cb, False) if adm else (None, False)


async def delete_previous(m: Message, state: FSMContext):
    """Вспомогательная функция для удаления сообщения пользователя и прошлого промпта бота"""
    data = await state.get_data()
    try: 
        await m.delete()
    except Exception: 
        pass
    
    if "last_msg_id" in data:
        try: 
            await m.bot.delete_message(m.chat.id, data["last_msg_id"])
        except Exception: 
            pass


# ================== главное меню ==================
@router.message(CommandStart())
async def start(m: Message, command: CommandObject):
    await referrals.register_start(m.from_user.id, command.args)
    await show_main(m, user_id=m.from_user.id)


@router.message(Command("ref"))
async def cmd_ref(m: Message):
    me = await m.bot.get_me()
    await m.answer(f"{em('gift')} <b>Реферальная программа</b>\n\n"
                   f"За каждые {referrals.REFERRALS_PER_BONUS} приглашённых, кто "
                   f"запустит бота по вашей ссылке — {referrals.BONUS_DAYS} дней "
                   f"Pro в подарок.\n\n" + await referrals.status_text(m.from_user.id, me.username))


@router.message(Command("pro"))
async def cmd_pro(m: Message):
    is_pro = await referrals.is_pro(m.from_user.id)
    if is_pro:
        pu = await referrals.get_or_create(m.from_user.id)
        await m.answer(f"{em('sparkles')} У вас уже активен Pro до "
                       f"{pu.pro_until.strftime('%d.%m.%Y')}.\nВ ваших ботах нет "
                       "рекламы — её нельзя ни купить, ни разослать в них.")
        return
    await m.answer(
        f"{em('sparkles')} <b>Dialogue Engine Pro</b> — {config.PRO_PRICE_RUB} ₽/мес\n\n"
        "В ваших ботах не будет рекламы: её нельзя будет купить для показа, и "
        "в них не попадут глобальные рекламные рассылки.\n\n"
        "Также Pro можно получить бесплатно за приглашённых — см. /ref",
        reply_markup=kb([[("💳 Купить Pro", "buy_pro")]]))


@router.callback_query(F.data == "buy_pro")
async def buy_pro(c: CallbackQuery):
    try:
        payment_id, url = await pay_service.create_pro_payment(c.from_user.id, months=1)
    except RuntimeError as e:
        await c.answer(str(e), show_alert=True)
        return
    await c.message.edit_text(
        f"{em('sparkles')} Оплата Pro-подписки ({config.PRO_PRICE_RUB} ₽/мес). "
        "После оплаты Pro активируется автоматически в течение минуты.",
        reply_markup=kb([[(f"💳 Оплатить {config.PRO_PRICE_RUB} ₽", None, url)]]))
    await c.answer()


async def show_main(m: Message, edit: bool = False, user_id: int | None = None):
    # БАГ: раньше сюда передавали c.message при возврате "⬅️ Назад", а
    # c.message.from_user — это САМ БОТ, а не человек, который нажал кнопку.
    # Из-за этого список ботов владельца обнулялся, а проверка супер-админа
    # никогда не срабатывала при переходе через кнопку "Назад". Теперь id
    # пользователя передаётся явным параметром.
    uid = user_id if user_id is not None else m.from_user.id
    async with Session() as s:
        own = (await s.scalars(select(ChildBot).where(
            ChildBot.owner_id == uid))).all()
        adm_ids = (await s.scalars(select(BotAdmin.bot_id).where(
            BotAdmin.user_id == uid))).all()
        admined = (await s.scalars(select(ChildBot).where(
            ChildBot.id.in_(adm_ids)))).all() if adm_ids else []
    rows = [[(f"🤖 @{b.username}", f"bot:{b.id}")] for b in {b.id: b for b in own + admined}.values()]
    rows.append([(f"➕ Создать бота", "newbot")])
    if uid == SUPER_ADMIN_ID and SUPER_ADMIN_ID:
        rows.append([(f"{em('gear')} Админ-панель", "ap")])
    
    text = (f"{em('sparkles')} <b>Dialogue Engine — конструктор ботов</b>\n\n"
            f"{em('speech')} Фидбек-боты — обращения, ответы, модерация\n"
            f"{em('megaphone')} Постинг-боты — каналы, предложка, посты\n\n"
            "Выберите бота или создайте нового:")
    markup = kb(rows)
    
    if edit:
        await m.edit_text(text, reply_markup=markup)
    else:
        await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "main")
async def cb_main(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main(c.message, edit=True, user_id=c.from_user.id)
    await c.answer()


# ================== создание бота ==================
@router.callback_query(F.data == "newbot")
async def newbot(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.add_type)
    await c.message.edit_text("Тип бота?", reply_markup=kb([
        [("💬 Обратная связь", "type:feedback")],
        [("📣 Постинг в канал", "type:posting")]
    ]))
    await c.answer()


@router.callback_query(St.add_type, F.data.startswith("type:"))
async def newbot_type(c: CallbackQuery, state: FSMContext):
    await state.update_data(bot_type=c.data.split(":")[1], last_msg_id=c.message.message_id)
    await state.set_state(St.add_token)
    await c.message.edit_text(f"{em('lock')} Пришлите токен бота от @BotFather:")
    await c.answer()


@router.message(St.add_token)
async def newbot_token(m: Message, state: FSMContext):
    await delete_previous(m, state)
    
    token = m.text.strip()
    try:
        validate_token(token)
        test = Bot(token)
        me = await test.get_me()
        await test.session.close()
    except Exception:
        msg = await m.answer(f"{em('cross')} Неверный токен, попробуйте снова.")
        await state.update_data(last_msg_id=msg.message_id)
        return
        
    data = await state.get_data()
    async with Session() as s:
        exists = await s.scalar(select(ChildBot).where(ChildBot.token == token))
        if exists:
            msg = await m.answer("Этот бот уже добавлен.")
            await state.update_data(last_msg_id=msg.message_id)
            return
            
        cb = ChildBot(owner_id=m.from_user.id, token=token, bot_tg_id=me.id,
                      username=me.username, bot_type=BotType(data["bot_type"]))
        s.add(cb)
        await s.commit()
        await s.refresh(cb)
        
    await manager.start_bot(cb)
    await state.clear()
    await m.answer(f"{em('party')} Бот @{me.username} создан и запущен!\n"
                   f"Теперь настройте его в меню.")
    await show_main(m, edit=False)


# ================== меню бота ==================
@router.callback_query(F.data.startswith("bot:"))
async def bot_menu(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb:
        await c.answer("Нет доступа", show_alert=True); return
    rows = [
        [("📣 Рассылка", f"bc:{bot_id}"), ("📊 Статистика", f"stats:{bot_id}")]]
    if is_owner:
        rows += [
            [("⚙️ Настройки", f"cfg:{bot_id}")],
            [("🔘 Кнопки и команды", f"btns:{bot_id}"),
             ("👥 Админы", f"admins:{bot_id}")],
            [("⏯ Вкл/выкл", f"toggle:{bot_id}"), ("🗑 Удалить", f"del:{bot_id}")]]
    rows.append([("⬅️ Назад", "main")])
    status = "🟢 работает" if cb.is_active else "🔴 остановлен"
    await c.message.edit_text(
        f"🤖 <b>@{cb.username}</b> · {cb.bot_type.value} · {status}",
        reply_markup=kb(rows))
    await c.answer()


# ================== настройки ==================
@router.callback_query(F.data.startswith("cfg:"))
async def cfg_menu(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    if cb.bot_type == BotType.feedback:
        rows = [
            [("✉️ Открытие обращений: " + cb.open_mode.value, f"cyc_open:{bot_id}")],
            [("📨 Метод: " + cb.forward_mode.value, f"cyc_fwd:{bot_id}")],
            [("🧵 Топики: " + ("вкл" if cb.use_topics else "выкл"), f"cyc_topics:{bot_id}")],
            [("👋 Приветствие", f"welcome:{bot_id}"),
             ("🏷 Шапка copy", f"header:{bot_id}")],
            [("🏠 Чат админов", f"admchat:{bot_id}"),
             (f"⚠️ Лимит варнов: {cb.warn_limit}", f"warnlim:{bot_id}")],
            [("⭐️ Донат: " + ("вкл" if cb.donate_enabled else "выкл"), f"cyc_donate:{bot_id}")],
            [("⭐️ Тип кнопки доната: " + cb.donate_button_type, f"cyc_donbtn:{bot_id}")],
            [("✉️ Кнопка обращения", f"ticketbtn:{bot_id}")],
            [("🔄 Restart/кнопка: " + ("новый тикет" if cb.always_new_ticket else "тот же тикет"),
              f"cyc_newticket:{bot_id}")],
        ]
    else:
        rows = [
            [("📮 Предложка: " + ("вкл" if cb.accept_suggestions else "выкл"),
              f"cyc_sugg:{bot_id}")],
            [("🎨 Шаблон поста", f"template:{bot_id}"), ("🔘 Кнопки шаблона", f"tplbtn:{bot_id}")],
            [("📡 Канал", f"channel:{bot_id}"), ("🏠 Чат админов", f"admchat:{bot_id}")],
            [("🧵 Топики в чате админов: " + ("вкл" if cb.use_topics else "выкл"),
              f"cyc_topics:{bot_id}")],
            [("📬 Публикация в канал: " + cb.channel_delivery_mode, f"cyc_delivery:{bot_id}")],
            [(f"⚠️ Лимит варнов: {cb.warn_limit}", f"warnlim:{bot_id}")],
        ]
    rows.append([("⬅️ Назад", f"bot:{bot_id}")])
    await c.message.edit_text(f"⚙️ Настройки @{cb.username}", reply_markup=kb(rows))
    await c.answer()


# --- циклические переключатели ---
CYCLES = {
    "cyc_open": ("open_mode", [OpenMode.first_message, OpenMode.start_command, OpenMode.button]),
    "cyc_fwd": ("forward_mode", [ForwardMode.forward, ForwardMode.copy]),
    "cyc_topics": ("use_topics", [False, True]),
    "cyc_donate": ("donate_enabled", [False, True]),
    "cyc_donbtn": ("donate_button_type", ["inline", "keyboard"]),
    "cyc_sugg": ("accept_suggestions", [False, True]),
    "cyc_newticket": ("always_new_ticket", [False, True]),
    "cyc_delivery": ("channel_delivery_mode", ["template", "copy"]),
}


@router.callback_query(F.data.regexp(r"^cyc_\w+:\d+$"))
async def cycle(c: CallbackQuery):
    key, bot_id = c.data.split(":")
    bot_id = int(bot_id)
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        return
    attr, values = CYCLES[key]
    async with Session() as s:
        obj = await s.get(ChildBot, bot_id)
        cur = getattr(obj, attr)
        setattr(obj, attr, values[(values.index(cur) + 1) % len(values)])
        await s.commit()
    
    # ИСПРАВЛЕНИЕ: Копируем модель, чтобы избежать ошибки Frozen Instance
    c_new = c.model_copy(update={"data": f"cfg:{bot_id}"})
    await cfg_menu(c_new)


# --- приветствие (с фото и премиум-эмодзи!) ---
@router.callback_query(F.data.startswith("welcome:"))
async def welcome(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.set_welcome)
    await state.update_data(bot_id=int(c.data.split(":")[1]), last_msg_id=c.message.message_id)
    await c.message.edit_text(
        f"{em('pencil')} Пришлите приветственный текст (можно с фото).\n"
        "Форматирование и премиум-эмодзи сохранятся как есть!")
    await c.answer()


@router.message(St.set_welcome)
async def welcome_save(m: Message, state: FSMContext):
    data = await state.get_data()
    await delete_previous(m, state)
    
    async with Session() as s:
        obj = await s.get(ChildBot, data["bot_id"])
        obj.welcome_text = m.html_text or ""          # html_text сохраняет tg-emoji!
        obj.welcome_photo = m.photo[-1].file_id if m.photo else None
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Приветствие сохранено!")


# --- чат админов / канал / шапка / шаблон / лимит варнов ---
@router.callback_query(F.data.startswith(("admchat:", "channel:")))
async def set_chat(c: CallbackQuery, state: FSMContext):
    kind, bot_id = c.data.split(":")
    await state.set_state(St.set_admin_chat)
    await state.update_data(bot_id=int(bot_id), kind=kind, last_msg_id=c.message.message_id)
    what = "чата для обращений/предложки" if kind == "admchat" else "канала"
    await c.message.edit_text(
        f"{em('home')} Пришлите ID {what} (напр. <code>-1001234567890</code>).\n"
        "Бот должен быть добавлен туда админом!")
    await c.answer()


@router.message(St.set_admin_chat)
async def set_chat_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    try:
        chat_id = int(m.text.strip())
    except ValueError:
        msg = await m.answer("Нужно число. Попробуйте еще раз.")
        await state.update_data(last_msg_id=msg.message_id)
        return
        
    data = await state.get_data()
    async with Session() as s:
        obj = await s.get(ChildBot, data["bot_id"])
        if data["kind"] == "admchat":
            obj.admin_chat_id = chat_id
        else:
            obj.channel_id = chat_id
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Сохранено!")


@router.callback_query(F.data.startswith("header:"))
async def header(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.set_header)
    await state.update_data(bot_id=int(c.data.split(":")[1]), last_msg_id=c.message.message_id)
    await c.message.edit_text(
        "Пришлите шаблон шапки для режима copy.\n"
        "Переменные: <code>{name}</code>, <code>{username}</code>, <code>{id}</code>")
    await c.answer()


@router.message(St.set_header)
async def header_save(m: Message, state: FSMContext):
    data = await state.get_data()
    await delete_previous(m, state)
    
    async with Session() as s:
        obj = await s.get(ChildBot, data["bot_id"])
        obj.copy_header = m.html_text
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Шапка сохранена!")


@router.callback_query(F.data.startswith("template:"))
async def template(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.set_template)
    await state.update_data(bot_id=int(c.data.split(":")[1]), last_msg_id=c.message.message_id)
    await c.message.edit_text(
        "Пришлите шаблон оформления постов из предложки.\n"
        "Обязательная переменная: <code>{text}</code>\n\n"
        "Пример: <i>Привет, нам написал подписчик:\n\n{text}</i>")
    await c.answer()


@router.message(St.set_template)
async def template_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    if "{text}" not in (m.html_text or ""):
        msg = await m.answer(f"{em('warn')} В шаблоне должна быть переменная {{text}}!")
        await state.update_data(last_msg_id=msg.message_id)
        return
        
    data = await state.get_data()
    async with Session() as s:
        obj = await s.get(ChildBot, data["bot_id"])
        obj.post_template = m.html_text
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Шаблон сохранён!")


@router.callback_query(F.data.startswith("warnlim:"))
async def warnlim(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.set_warn_limit)
    await state.update_data(bot_id=int(c.data.split(":")[1]), last_msg_id=c.message.message_id)
    await c.message.edit_text("Сколько варнов до автобана? (число)")
    await c.answer()


@router.message(St.set_warn_limit)
async def warnlim_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    if not m.text.isdigit():
        msg = await m.answer("Нужно число. Попробуйте еще раз.")
        await state.update_data(last_msg_id=msg.message_id)
        return
        
    data = await state.get_data()
    async with Session() as s:
        obj = await s.get(ChildBot, data["bot_id"])
        obj.warn_limit = int(m.text)
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Лимит варнов: {m.text}")


# ================== кнопки и команды ==================
@router.callback_query(F.data.startswith("btns:"))
async def btns_menu(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    async with Session() as s:
        btns = (await s.scalars(select(BotButton).where(
            BotButton.bot_id == bot_id).order_by(BotButton.position))).all()
    rows = [[(f"🗑 [{b.kind}] {b.text}", f"btndel:{bot_id}:{b.id}")] for b in btns]
    rows.append([("➕ Добавить", f"btnadd:{bot_id}")])
    rows.append([("⬅️ Назад", f"bot:{bot_id}")])
    await c.message.edit_text("🔘 Кнопки и триггер-команды\n(нажмите чтобы удалить)",
                              reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("btndel:"))
async def btndel(c: CallbackQuery):
    _, bot_id, btn_id = c.data.split(":")
    bot_id = int(bot_id)
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    async with Session() as s:
        b = await s.get(BotButton, int(btn_id))
        if b:
            await s.delete(b)
            await s.commit()
    c_new = c.model_copy(update={"data": f"btns:{bot_id}"})
    await btns_menu(c_new)


@router.callback_query(F.data.startswith("btnadd:"))
async def btnadd(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    await state.set_state(St.btn_kind)
    await state.update_data(bot_id=bot_id, last_msg_id=c.message.message_id)
    await c.message.edit_text("Тип элемента?", reply_markup=kb([
        [("🔗 Инлайн-ссылка", "bk:inline_url"), ("⚡️ Инлайн-триггер", "bk:inline_trigger")],
        [("⌨️ Кейборд-кнопка", "bk:keyboard"), ("/ Триггер-команда", "bk:command")]
    ]))
    await c.answer()


@router.callback_query(St.btn_kind, F.data.startswith("bk:"))
async def btn_kind(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[1]
    await state.update_data(kind=kind, last_msg_id=c.message.message_id)
    await state.set_state(St.btn_text)
    hint = "имя команды без /" if kind == "command" else "текст кнопки"
    await c.message.edit_text(f"Пришлите {hint}:")
    await c.answer()


@router.message(St.btn_text)
async def btn_text(m: Message, state: FSMContext):
    data = await state.get_data()
    try: await m.delete()
    except Exception: pass
    
    await state.update_data(text=m.text.strip().lstrip("/"))
    if data["kind"] == "inline_url":
        await state.set_state(St.btn_url)
        next_text = "Пришлите URL:"
    else:
        await state.set_state(St.btn_response)
        next_text = "Пришлите ответ триггера (текст/фото, форматирование сохранится):"

    # Редактируем прошлое сообщение бота, чтобы форма плавно перетекала в следующий шаг
    try:
        await m.bot.edit_message_text(next_text, m.chat.id, data["last_msg_id"])
    except Exception:
        msg = await m.answer(next_text)
        await state.update_data(last_msg_id=msg.message_id)


@router.message(St.btn_url)
async def btn_url(m: Message, state: FSMContext):
    await delete_previous(m, state)
    await state.update_data(url=m.text.strip())
    await _ask_style(m, state)


@router.message(St.btn_response)
async def btn_response(m: Message, state: FSMContext):
    data = await state.get_data()
    await delete_previous(m, state)
    await state.update_data(response_text=m.html_text or "",
                            response_photo=m.photo[-1].file_id if m.photo else None)
    if data["kind"] in ("keyboard", "command"):
        # Reply-клавиатура и команды — обычные Telegram-объекты без поддержки
        # цвета/premium-иконки (это фича именно inline-кнопок), сохраняем сразу.
        await _save_button(m, state)
    else:
        await _ask_style(m, state)


STYLES = [
    ("⬜️ Обычная", "-"), ("🟦 Primary", "primary"),
    ("🟩 Success", "success"), ("🟥 Danger", "danger"),
]


async def _ask_style(m: Message, state: FSMContext):
    await state.set_state(St.btn_style)
    msg = await m.answer(
        "Выберите цвет кнопки (Bot API 9.4):",
        reply_markup=kb([[(t, f"style:{v}")] for t, v in STYLES]))
    await state.update_data(last_msg_id=msg.message_id)


@router.callback_query(St.btn_style, F.data.startswith("style:"))
async def btn_style(c: CallbackQuery, state: FSMContext):
    style = c.data.split(":", 1)[1]
    await state.update_data(style=None if style == "-" else style)
    await state.set_state(St.btn_icon)
    await c.message.edit_text(
        f"{em('sparkles')} Хотите добавить premium-эмодзи на кнопку?\n"
        "Пришлите сообщение с ОДНИМ premium-эмодзи (просто отправьте его как "
        "текст), или отправьте «-», чтобы пропустить.")
    await state.update_data(last_msg_id=c.message.message_id)
    await c.answer()


@router.message(St.btn_icon)
async def btn_icon(m: Message, state: FSMContext):
    await delete_previous(m, state)
    icon_id = None
    if m.text and m.text.strip() != "-" and m.entities:
        for e in m.entities:
            if e.type == "custom_emoji":
                icon_id = e.custom_emoji_id
                break
        if icon_id is None:
            msg = await m.answer(
                f"{em('warn')} Не нашёл premium-эмодзи в сообщении. Пришлите "
                "его ещё раз (именно эмодзи из платного набора) или «-» чтобы пропустить.")
            await state.update_data(last_msg_id=msg.message_id)
            return
    await state.update_data(icon_emoji_id=icon_id)
    await _save_button(m, state)


async def _save_button(m: Message, state: FSMContext):
    data = await state.get_data()
    async with Session() as s:
        s.add(BotButton(
            bot_id=data["bot_id"], kind=data["kind"], text=data["text"],
            url=data.get("url"),
            response_text=data.get("response_text"),
            response_photo=data.get("response_photo"),
            style=data.get("style"), icon_emoji_id=data.get("icon_emoji_id")))
        await s.commit()
    await state.clear()
    # Кнопки читаются из БД "живьём" при каждом апдейте (см. child/common.py::
    # build_keyboards) — рестарт бота здесь был не нужен вообще и только
    # добавлял лишнюю нагрузку/риск (лишний Bot(), delete_webhook,
    # переустановка long-poll) без всякой пользы.
    await m.answer(f"{em('check')} Кнопка добавлена!")


# --- кнопка "Открыть обращение" (текст/цвет/premium-эмодзи) ---
@router.callback_query(F.data.startswith("ticketbtn:"))
async def ticketbtn_start(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    await state.set_state(St.ticket_btn_text)
    await state.update_data(bot_id=bot_id, last_msg_id=c.message.message_id)
    await c.message.edit_text(f"Текущий текст кнопки: «{cb.ticket_button_text}»\n"
                              "Пришлите новый текст кнопки:")
    await c.answer()


@router.message(St.ticket_btn_text)
async def ticketbtn_text(m: Message, state: FSMContext):
    await delete_previous(m, state)
    await state.update_data(text=m.text.strip()[:64])
    await state.set_state(St.ticket_btn_style)
    msg = await m.answer("Выберите цвет кнопки (Bot API 9.4):",
                         reply_markup=kb([[(t, f"tbstyle:{v}")] for t, v in STYLES]))
    await state.update_data(last_msg_id=msg.message_id)


@router.callback_query(St.ticket_btn_style, F.data.startswith("tbstyle:"))
async def ticketbtn_style(c: CallbackQuery, state: FSMContext):
    style = c.data.split(":", 1)[1]
    await state.update_data(style=None if style == "-" else style)
    await state.set_state(St.ticket_btn_icon)
    await c.message.edit_text(
        f"{em('sparkles')} Пришлите premium-эмодзи для кнопки (просто отправьте "
        "его как текст), или «-» чтобы пропустить.")
    await c.answer()


@router.message(St.ticket_btn_icon)
async def ticketbtn_icon(m: Message, state: FSMContext):
    icon_id = None
    if m.text and m.text.strip() != "-" and m.entities:
        for e in m.entities:
            if e.type == "custom_emoji":
                icon_id = e.custom_emoji_id
                break
    data = await state.get_data()
    async with Session() as s:
        obj = await s.get(ChildBot, data["bot_id"])
        obj.ticket_button_text = data["text"]
        obj.ticket_button_style = data.get("style")
        obj.ticket_button_icon = icon_id
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Кнопка обращения обновлена!")


# --- кнопки шаблона (появляются на КАЖДОМ посте постинг-бота) ---
@router.callback_query(F.data.startswith("tplbtn:"))
async def tplbtn_menu(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    rows_data = json.loads(cb.template_buttons_json) if cb.template_buttons_json else []
    flat = [b for row in rows_data for b in row]
    rows = [[(f"🗑 {b['text']}", f"tpldel:{bot_id}:{i}")] for i, b in enumerate(flat)]
    rows.append([("➕ Добавить кнопку", f"tpladd:{bot_id}")])
    rows.append([("⬅️ Назад", f"cfg:{bot_id}")])
    await c.message.edit_text(
        f"{em('link')} Кнопки шаблона — добавляются к КАЖДОМУ посту "
        "автоматически (поверх кнопок конкретного поста).", reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("tpladd:"))
async def tpladd(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    await state.set_state(St.tpl_btn_edit)
    await state.update_data(bot_id=bot_id, last_msg_id=c.message.message_id)
    await c.message.edit_text("Формат: <code>Текст кнопки | https://ссылка</code>")
    await c.answer()


@router.message(St.tpl_btn_edit)
async def tpladd_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    if not m.text or "|" not in m.text:
        msg = await m.answer(f"{em('warn')} Формат: <code>Текст кнопки | https://ссылка</code>")
        await state.update_data(last_msg_id=msg.message_id)
        return
    text, url = [p.strip() for p in m.text.split("|", 1)]
    if not (url.startswith("http://") or url.startswith("https://")):
        msg = await m.answer(f"{em('warn')} Ссылка должна начинаться с http(s)://")
        await state.update_data(last_msg_id=msg.message_id)
        return
    data = await state.get_data()
    async with Session() as s:
        obj = await s.get(ChildBot, data["bot_id"])
        rows_data = json.loads(obj.template_buttons_json) if obj.template_buttons_json else []
        rows_data.append([{"text": text[:64], "url": url}])
        obj.template_buttons_json = json.dumps(rows_data)
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Кнопка шаблона добавлена!")


@router.callback_query(F.data.startswith("tpldel:"))
async def tpldel(c: CallbackQuery):
    _, bot_id, idx = c.data.split(":")
    bot_id, idx = int(bot_id), int(idx)
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    async with Session() as s:
        obj = await s.get(ChildBot, bot_id)
        rows_data = json.loads(obj.template_buttons_json) if obj.template_buttons_json else []
        flat = [b for row in rows_data for b in row]
        if 0 <= idx < len(flat):
            flat.pop(idx)
        obj.template_buttons_json = json.dumps([[b] for b in flat]) if flat else None
        await s.commit()
    c_new = c.model_copy(update={"data": f"tplbtn:{bot_id}"})
    await tplbtn_menu(c_new)



# ================== админы ==================
@router.callback_query(F.data.startswith("admins:"))
async def admins_menu(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    # БАГ БЕЗОПАСНОСТИ: раньше тут не было проверки владельца — любой, кто
    # подобрал/увидел callback_data "admins:<id>", мог открыть список админов
    # чужого бота и (через admadd/admdel) добавлять или удалять их.
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    async with Session() as s:
        admins = (await s.scalars(select(BotAdmin).where(
            BotAdmin.bot_id == bot_id))).all()
    rows = [[(f"🗑 {a.user_id}", f"admdel:{bot_id}:{a.user_id}")] for a in admins]
    rows.append([("➕ Добавить админа", f"admadd:{bot_id}")])
    rows.append([("⬅️ Назад", f"bot:{bot_id}")])
    await c.message.edit_text("👥 Доверенные администраторы\n"
                              "(доступ к рассылке, статистике и модерации)",
                              reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("admadd:"))
async def admadd(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    await state.set_state(St.add_admin)
    await state.update_data(bot_id=bot_id, last_msg_id=c.message.message_id)
    await c.message.edit_text("Пришлите Telegram ID администратора:")
    await c.answer()


@router.message(St.add_admin)
async def admadd_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    if not m.text.strip().isdigit():
        msg = await m.answer("Нужен числовой ID. Попробуйте еще раз.")
        await state.update_data(last_msg_id=msg.message_id)
        return

    data = await state.get_data()
    cb, is_owner = await _access(data["bot_id"], m.from_user.id)
    if not cb or not is_owner:
        await state.clear()
        return
    async with Session() as s:
        s.add(BotAdmin(bot_id=data["bot_id"], user_id=int(m.text.strip())))
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Админ добавлен! Ему доступно меню бота в конструкторе.")


@router.callback_query(F.data.startswith("admdel:"))
async def admdel(c: CallbackQuery):
    _, bot_id, uid = c.data.split(":")
    bot_id = int(bot_id)
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    async with Session() as s:
        a = await s.scalar(select(BotAdmin).where(
            BotAdmin.bot_id == bot_id, BotAdmin.user_id == int(uid)))
        if a:
            await s.delete(a)
            await s.commit()
    c_new = c.model_copy(update={"data": f"admins:{bot_id}"})
    await admins_menu(c_new)


# ================== рассылка ==================
@router.callback_query(F.data.startswith("bc:"))
async def bc_start(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    cb, _ = await _access(bot_id, c.from_user.id)
    if not cb:
        await c.answer("Нет доступа", show_alert=True); return
    await state.set_state(St.bc_content)
    await state.update_data(bot_id=bot_id, last_msg_id=c.message.message_id)
    await c.message.edit_text(
        f"{em('megaphone')} Пришлите сообщение для рассылки:\n"
        "текст / текст+медиа / медиа. Форматирование и премиум-эмодзи сохранятся.")
    await c.answer()


@router.message(St.bc_content)
async def bc_content(m: Message, state: FSMContext):
    data = await state.get_data()
    try: await m.delete()
    except Exception: pass
    
    file_id, media_type = capture_media(m)

    await state.update_data(html_text=m.html_text if (m.text or m.caption) else None,
                            file_id=file_id, media_type=media_type)
    await state.set_state(St.bc_target)
    
    text = "Кому разослать?"
    markup = kb([
        [("👥 Всем пользователям", "bct:all")],
        [("🔥 Активным (7 дней)", "bct:active")],
        [("❌ Отмена", "main")]
    ])
    
    try:
        await m.bot.edit_message_text(text, m.chat.id, data["last_msg_id"], reply_markup=markup)
    except Exception:
        # Если вдруг редактирование не удалось (например из-за медиа), удаляем и шлем заново
        try: await m.bot.delete_message(m.chat.id, data["last_msg_id"])
        except Exception: pass
        msg = await m.answer(text, reply_markup=markup)
        await state.update_data(last_msg_id=msg.message_id)


@router.callback_query(St.bc_target, F.data.startswith("bct:"))
async def bc_go(c: CallbackQuery, state: FSMContext):
    target = c.data.split(":")[1]
    data = await state.get_data()
    await state.clear()
    async with Session() as s:
        cb = await s.get(ChildBot, data["bot_id"])
        
    msg = await c.message.edit_text(f"{em('hourglass')} Рассылка запущена...")

    async def progress(done, total):
        try:
            await msg.edit_text(f"{em('hourglass')} Рассылка: {done}/{total}")
        except Exception:
            pass

    result = await run_broadcast(cb.token, cb.id, target=target,
                                 html_text=data["html_text"],
                                 media_file_id=data["file_id"],
                                 media_type=data["media_type"],
                                 progress_cb=progress)
    await msg.edit_text(
        f"{em('check')} <b>Рассылка завершена</b>\n\n"
        f"Всего: {result['total']}\n✅ Доставлено: {result['sent']}\n"
        f"🚫 Заблокировали бота: {result['blocked']}\n❌ Ошибки: {result['failed']}")
    await c.answer()


# ================== статистика ==================
@router.callback_query(F.data.startswith("stats:"))
async def stats(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, _ = await _access(bot_id, c.from_user.id)
    if not cb:
        await c.answer("Нет доступа", show_alert=True); return
    await c.answer("Рисую график...")
    buf = await build_stats_image(bot_id)
    await c.message.answer_photo(
        BufferedInputFile(buf.read(), filename="stats.png"),
        caption=f"{em('chart')} Статистика @{cb.username}")
    # Статистика по админам — отдельным текстовым блоком (не смешана с
    # графиком), как и просили: кто из админов сколько ответил/забанил/варнил.
    admin_stats = await mod.admin_stats_text(bot_id)
    await c.message.answer(f"{em('crown')} <b>Статистика по админам</b>\n\n{admin_stats}")


# ================== вкл/выкл, удаление ==================
@router.callback_query(F.data.startswith("toggle:"))
async def toggle(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        return
    async with Session() as s:
        obj = await s.get(ChildBot, bot_id)
        obj.is_active = not obj.is_active
        await s.commit()
    if obj.is_active:
        await manager.start_bot(obj)
    else:
        await manager.stop_bot(bot_id)
        
    c_new = c.model_copy(update={"data": f"bot:{bot_id}"})
    await bot_menu(c_new)


@router.callback_query(F.data.startswith("del:"))
async def delete(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        return
    await manager.stop_bot(bot_id)
    async with Session() as s:
        obj = await s.get(ChildBot, bot_id)
        await s.delete(obj)
        await s.commit()
    await c.message.edit_text(f"{em('trash')} Бот @{cb.username} удалён.")
    await c.answer()


# ================== админ-панель (только SUPER_ADMIN_ID) ==================
def _is_super(user_id: int) -> bool:
    return bool(SUPER_ADMIN_ID) and user_id == SUPER_ADMIN_ID


@router.callback_query(F.data == "ap")
async def ap_menu(c: CallbackQuery):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await c.message.edit_text(
        f"{em('gear')} <b>Админ-панель платформы</b>",
        reply_markup=kb([
            [("🤖 Все боты", "ap_bots:0")],
            [("📊 Общая статистика", "ap_stats")],
            [("📢 Разослать во все боты", "ap_bc")],
            [("⬅️ Назад", "main")],
        ]))
    await c.answer()


@router.callback_query(F.data.startswith("ap_bots:"))
async def ap_bots(c: CallbackQuery):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    page = int(c.data.split(":")[1])
    per_page = 10
    async with Session() as s:
        all_bots = (await s.scalars(select(ChildBot).order_by(ChildBot.id))).all()
    chunk = all_bots[page * per_page:(page + 1) * per_page]
    rows = [[(f"{'🟢' if b.is_active else '🔴'} @{b.username} ({b.bot_type.value})",
             f"bot:{b.id}")] for b in chunk]
    nav = []
    if page > 0:
        nav.append((f"⬅️ Стр. {page}", f"ap_bots:{page-1}"))
    if (page + 1) * per_page < len(all_bots):
        nav.append((f"Стр. {page+2} ➡️", f"ap_bots:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([("⬅️ Назад", "ap")])
    await c.message.edit_text(f"🤖 Все боты платформы ({len(all_bots)}):",
                              reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data == "ap_stats")
async def ap_stats(c: CallbackQuery):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    async with Session() as s:
        bots = (await s.scalars(select(ChildBot))).all()
    active = sum(1 for b in bots if b.is_active)
    feedback_n = sum(1 for b in bots if b.bot_type == BotType.feedback)
    posting_n = sum(1 for b in bots if b.bot_type == BotType.posting)
    await c.message.edit_text(
        f"{em('chart')} <b>Статистика платформы</b>\n\n"
        f"Всего ботов: {len(bots)}\n🟢 Активны: {active}\n🔴 Остановлены: {len(bots) - active}\n\n"
        f"💬 Фидбек-ботов: {feedback_n}\n📣 Постинг-ботов: {posting_n}",
        reply_markup=kb([[("⬅️ Назад", "ap")]]))
    await c.answer()


@router.callback_query(F.data == "ap_bc")
async def ap_bc_start(c: CallbackQuery, state: FSMContext):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await state.set_state(St.ap_bc_content)
    await state.update_data(last_msg_id=c.message.message_id)
    await c.message.edit_text(
        f"{em('megaphone')} Пришлите сообщение, которое будет разослано ВСЕМ "
        "пользователям ВСЕХ активных ботов платформы (текст/медиа/форматирование "
        "сохранятся):")
    await c.answer()


@router.message(St.ap_bc_content)
async def ap_bc_content(m: Message, state: FSMContext):
    if not _is_super(m.from_user.id):
        return
    file_id, media_type = capture_media(m)
    await state.clear()

    async with Session() as s:
        bots = (await s.scalars(select(ChildBot).where(ChildBot.is_active))).all()
    msg = await m.answer(f"{em('hourglass')} Рассылка запущена по {len(bots)} ботам...")
    html_text = m.html_text if (m.text or m.caption) else None
    total = sent = failed = 0
    for cb in bots:
        try:
            res = await run_broadcast(cb.token, cb.id, target="all",
                                      html_text=html_text, media_file_id=file_id,
                                      media_type=media_type)
            total += res["total"]; sent += res["sent"]; failed += res["failed"]
        except Exception:
            failed += 1
    await msg.edit_text(f"{em('check')} <b>Готово</b>\nБотов: {len(bots)}\n"
                        f"Получателей всего: {total}\n✅ Доставлено: {sent}\n❌ Ошибки: {failed}")


# ================== модерация рекламы (/ads в мастер-боте) ==================
async def _notify_ad_buyer(ad: Advertisement, text: str, reply_markup=None):
    # /ads теперь покупается прямо в master-боте, поэтому покупатель уже
    # переписывается именно с ним — не нужно поднимать отдельный Bot()
    # для дочернего бота (это раньше и было лишним источником нестабильности:
    # частое создание/закрытие сессий сторонних Bot() объектов).
    bot = Bot(MASTER_BOT_TOKEN)
    try:
        await bot.send_message(ad.buyer_id, text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        pass
    finally:
        await bot.session.close()


@router.callback_query(F.data.startswith("ad_ok:"))
async def ad_approve(c: CallbackQuery):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    ad_id = int(c.data.split(":")[1])
    ad = await ads_service.approve(ad_id)
    if not ad:
        await c.answer("Заявка уже обработана", show_alert=True); return
    try:
        kind_label = ("Рассылку во все боты" if ad.kind == AdKind.broadcast
                     else f"{ad.target_impressions} показов")
        payment_id, url = await pay_service.create_ad_payment(
            ad.id, ad.price_rub, f"Реклама #{ad.id}: {kind_label}")
        async with Session() as s:
            obj = await s.get(Advertisement, ad.id)
            obj.payment_id = payment_id
            await s.commit()
        pay_kb = InlineKeyboardMarkup(inline_keyboard=[[
            styled_button(f"💳 Оплатить {ad.price_rub} ₽", url=url)]])
        await _notify_ad_buyer(
            ad, f"{em('check')} Ваша заявка №{ad.id} одобрена!\n"
               f"Формат: {kind_label}\nК оплате: {ad.price_rub} ₽\n\n"
               "После оплаты реклама автоматически запустится.", pay_kb)
        await c.message.edit_text(c.message.text + "\n\n✅ Одобрено, ссылка на оплату отправлена.")
    except RuntimeError as e:
        # ЮKassa не настроена (нет ключей) — сообщаем админу прямо в чате
        await c.message.edit_text(c.message.text + f"\n\n⚠️ {e}")
    await c.answer()


@router.callback_query(F.data.startswith("ad_no:"))
async def ad_reject(c: CallbackQuery, state: FSMContext):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    ad_id = int(c.data.split(":")[1])
    await state.set_state(St.ad_reject_reason)
    await state.update_data(ad_id=ad_id, last_msg_id=c.message.message_id)
    await c.message.edit_text(c.message.text + "\n\n✏️ Укажите причину отклонения "
                              "(или отправьте «-»):")
    await c.answer()


@router.message(St.ad_reject_reason)
async def ad_reject_reason(m: Message, state: FSMContext):
    if not _is_super(m.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    reason = "" if m.text.strip() == "-" else m.text.strip()
    ad = await ads_service.reject(data["ad_id"], reason)
    if not ad:
        await m.answer("Заявка уже обработана.")
        return
    await m.answer(f"{em('cross')} Заявка №{ad.id} отклонена.")
    text = f"{em('cross')} Ваша заявка на рекламу №{ad.id} отклонена."
    if reason:
        text += f"\nПричина: {reason}"
    await _notify_ad_buyer(ad, text)


# =========================================================================
# ==================   /ads — покупка рекламы В МАСТЕР-БОТЕ   ===========
# =========================================================================
# РАНЬШЕ /ads жил в каждом дочернем боте и показ рекламы не был привязан ни
# к какому конкретному боту — одна купленная кампания светилась сразу во
# ВСЕХ ботах платформы. Теперь /ads работает только здесь, в master-боте;
# первым шагом покупатель явно выбирает КОНКРЕТНЫЙ бот, в котором реклама
# будет показываться (или тариф "рассылка во все боты" — он платформенный
# по своей сути и выбора бота не требует).
@router.message(Command("ads"))
async def ads_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(St.ads_pick_kind)
    rows = [[("🎯 Показы в конкретном боте", "adk:impr")]]
    cd = await ads_service.cooldown_remaining(m.from_user.id)
    if cd:
        days = cd.days + (1 if cd.seconds else 0)
        rows.append([(f"📢 Рассылка (доступна через {days} дн.)", "adk:cd")])
    else:
        rows.append([(f"📢 Рассылка во все боты ({ads_service.BROADCAST_PRICE_RUB} ₽)", "adk:bcast")])
    await m.answer(
        f"{em('megaphone')} <b>Покупка рекламы</b>\n\nВыберите формат:",
        reply_markup=kb(rows))


@router.callback_query(St.ads_pick_kind, F.data == "adk:cd")
async def ads_kind_cooldown(c: CallbackQuery):
    await c.answer(f"Рассылку во все боты можно покупать не чаще "
                   f"раза в {AD_BROADCAST_COOLDOWN_DAYS} дней.", show_alert=True)


@router.callback_query(St.ads_pick_kind, F.data == "adk:impr")
async def ads_kind_impr(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.ads_pick_bot)
    await state.update_data(kind="impressions", page=0)
    await _show_bot_picker(c, 0)
    await c.answer()


async def _show_bot_picker(c: CallbackQuery, page: int):
    bots = await ads_service.list_active_bots()
    per_page = 8
    chunk = bots[page * per_page:(page + 1) * per_page]
    if not bots:
        await c.message.edit_text(f"{em('warn')} Пока нет ни одного активного бота "
                                  "на платформе для размещения рекламы.")
        return
    rows = [[(f"🤖 @{b.username}", f"adbot:{b.id}")] for b in chunk]
    nav = []
    if page > 0:
        nav.append(("⬅️", f"adbotpage:{page-1}"))
    if (page + 1) * per_page < len(bots):
        nav.append(("➡️", f"adbotpage:{page+1}"))
    if nav:
        rows.append(nav)
    await c.message.edit_text(
        f"{em('info')} В каком боте разместить рекламу? Показы будут "
        "только в нём.", reply_markup=kb(rows))


@router.callback_query(St.ads_pick_bot, F.data.startswith("adbotpage:"))
async def ads_bot_page(c: CallbackQuery, state: FSMContext):
    page = int(c.data.split(":")[1])
    await state.update_data(page=page)
    await _show_bot_picker(c, page)
    await c.answer()


@router.callback_query(St.ads_pick_bot, F.data.startswith("adbot:"))
async def ads_pick_bot(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    await state.update_data(target_bot_id=bot_id)
    await state.set_state(St.ads_text)
    await c.message.edit_text(
        f"{em('pencil')} Пришлите текст объявления (до {AD_MAX_LEN} символов). "
        "Можно приложить фото/видео/гифку — текст тогда идёт подписью.")
    await c.answer()


@router.callback_query(St.ads_pick_kind, F.data == "adk:bcast")
async def ads_kind_bcast(c: CallbackQuery, state: FSMContext):
    cd = await ads_service.cooldown_remaining(c.from_user.id)
    if cd:
        await c.answer("Кулдаун ещё не истёк.", show_alert=True)
        return
    await state.update_data(kind="broadcast")
    await state.set_state(St.ads_text)
    await c.message.edit_text(
        f"{em('pencil')} Пришлите текст объявления (до {AD_MAX_LEN} символов). "
        "Можно приложить фото/видео/гифку — текст тогда идёт подписью.")
    await c.answer()


def _ad_media(m: Message):
    if m.photo:
        return m.photo[-1].file_id, "photo"
    if m.video:
        return m.video.file_id, "video"
    if m.animation:
        return m.animation.file_id, "animation"
    return None, None


@router.message(St.ads_text)
async def ads_text(m: Message, state: FSMContext):
    text = (m.text or m.caption or "").strip()
    if not text:
        await m.answer(f"{em('warn')} Нужен текст объявления.")
        return
    if len(text) > AD_MAX_LEN:
        await m.answer(f"{em('warn')} Слишком длинно ({len(text)}/{AD_MAX_LEN}). "
                       "Сократите текст и пришлите ещё раз.")
        return
    file_id, media_type = _ad_media(m)
    await state.update_data(text=text, media_file_id=file_id, media_type=media_type)
    data = await state.get_data()
    if data["kind"] == "broadcast":
        await state.set_state(St.ads_confirm)
        text_conf = (f"{em('info')} Разовая рассылка во ВСЕ боты платформы, всем "
                    f"их пользователям.\nЦена: <b>{ads_service.BROADCAST_PRICE_RUB} ₽</b>\n"
                    f"Повторная покупка — не раньше чем через "
                    f"{AD_BROADCAST_COOLDOWN_DAYS} дней.\n\nОтправить на модерацию?")
        await m.answer(text_conf, reply_markup=kb([[
            ("✅ Отправить", "adc:send"), ("❌ Отмена", "adc:cancel")]]))
        return
    await state.set_state(St.ads_confirm)
    rows = [[(f"{n} показов — {ads_service.price_for_impressions(n)} ₽", f"adi:{n}")]
           for n in ads_service.TARIFF_PRESETS]
    rows.append([("✏️ Своё число показов", "adi:custom")])
    await m.answer("Выберите тариф (чем больше показов — тем дешевле цена за сотню):",
                   reply_markup=kb(rows))


@router.callback_query(St.ads_confirm, F.data == "adi:custom")
async def ads_impr_custom(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.ads_impr_custom)
    await c.message.edit_text("Введите желаемое число показов (целое число, минимум 1):")
    await c.answer()


@router.message(St.ads_impr_custom)
async def ads_impr_custom_save(m: Message, state: FSMContext):
    if not m.text or not m.text.strip().isdigit() or int(m.text.strip()) < 1:
        await m.answer(f"{em('warn')} Нужно целое число больше 0.")
        return
    await _ads_confirm_impressions(m, state, int(m.text.strip()))


@router.callback_query(St.ads_confirm, F.data.startswith("adi:"))
async def ads_impr_preset(c: CallbackQuery, state: FSMContext):
    n = int(c.data.split(":")[1])
    await _ads_confirm_impressions(c.message, state, n, edit=True)
    await c.answer()


async def _ads_confirm_impressions(m: Message, state: FSMContext, n: int, edit=False):
    price = ads_service.price_for_impressions(n)
    await state.update_data(impressions=n, price=price)
    await state.set_state(St.ads_confirm)
    text = (f"{em('info')} Объявление: {n} показов в стартовых сообщениях "
           f"выбранного бота.\nЦена: <b>{price} ₽</b>\n\nОтправить на модерацию?")
    markup = kb([[("✅ Отправить", "adc:send"), ("❌ Отмена", "adc:cancel")]])
    if edit:
        await m.edit_text(text, reply_markup=markup)
    else:
        await m.answer(text, reply_markup=markup)


@router.callback_query(St.ads_confirm, F.data == "adc:cancel")
async def ads_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Отменено.")
    await c.answer()


@router.callback_query(St.ads_confirm, F.data == "adc:send")
async def ads_send(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if data.get("kind") == "broadcast":
        ad = await ads_service.create_broadcast_ad(
            c.from_user.id, 0, data["text"],
            data.get("media_file_id"), data.get("media_type"))
        if not ad:
            await c.message.edit_text(f"{em('warn')} Кулдаун ещё не истёк, попробуйте позже.")
            await c.answer()
            return
    else:
        ad = await ads_service.create_impressions_ad(
            c.from_user.id, data["target_bot_id"], data["text"],
            data.get("media_file_id"), data.get("media_type"), data["impressions"])
        if not ad:
            await c.message.edit_text(f"{em('warn')} Владелец этого бота — Pro-подписчик, "
                                      "реклама в нём недоступна. Выберите другой бот через /ads.")
            await c.answer()
            return
    await c.message.edit_text(f"{em('check')} Заявка №{ad.id} отправлена суперадмину "
                              "на модерацию. Мы напишем, когда решение будет принято.")
    await c.answer()
    await _notify_super_admin(ad)


async def _notify_super_admin(ad: Advertisement):
    if not SUPER_ADMIN_ID:
        return
    if ad.kind == AdKind.broadcast:
        kind_label = "Рассылка во все боты"
    else:
        async with Session() as s:
            target = await s.get(ChildBot, ad.source_bot_id)
        kind_label = f"{ad.target_impressions} показов в @{target.username if target else '?'}"
    text = (f"{em('megaphone')} <b>Новая заявка на рекламу №{ad.id}</b>\n\n"
           f"От: <code>{ad.buyer_id}</code>\nФормат: {kind_label}\n"
           f"Цена: {ad.price_rub} ₽\n\n<b>Текст:</b>\n{ad.text}")
    markup = kb([[("✅ Принять", f"ad_ok:{ad.id}"), ("❌ Отклонить", f"ad_no:{ad.id}")]])
    master = Bot(MASTER_BOT_TOKEN)
    try:
        if ad.media_file_id and ad.media_type == "photo":
            await master.send_photo(SUPER_ADMIN_ID, ad.media_file_id, caption=text,
                                    parse_mode="HTML", reply_markup=markup)
        else:
            await master.send_message(SUPER_ADMIN_ID, text, parse_mode="HTML",
                                      reply_markup=markup)
    except Exception:
        pass
    finally:
        await master.session.close()
