from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, PreCheckoutQuery, LabeledPrice,
                           CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton)
from sqlalchemy import select
from db.base import Session
from db.models import ChildBot, BotAdmin, Donation, BotButton, OpenMode, BotUser, Ticket
from services import moderation as mod
from services import ads as ads_service
from services import referrals
from utils.emoji import em, styled_button
from config import PLATFORM_BOT_USERNAME


class DonateSt(StatesGroup):
    amount = State()


async def is_bot_admin(bot_db_id: int, user_id: int) -> bool:
    async with Session() as s:
        cfg = await s.get(ChildBot, bot_db_id)
        if cfg.owner_id == user_id:
            return True
        return bool(await s.scalar(select(BotAdmin).where(
            BotAdmin.bot_id == bot_db_id, BotAdmin.user_id == user_id)))


async def inject_ad(bot_db_id: int, text: str) -> str:
    """Добавляет активную оплаченную рекламу в конец стартового сообщения и
    засчитывает показ. Реклама показывается ТОЛЬКО в том боте, в котором она
    куплена (см. services/ads.py::get_active_ad_for_display) — раньше показ
    не был привязан к боту и реклама светилась сразу везде. Автоматически
    отключена, если владелец бота — Pro-подписчик."""
    ad = await ads_service.get_active_ad_for_display(bot_db_id)
    if not ad:
        return text
    await ads_service.register_impression(ad.id)
    return text + f"\n\n— — — — —\n{em('megaphone')} <i>{ad.text}</i>"


async def inject_footer(bot_db_id: int, text: str) -> str:
    """Приписка 'создано на платформе @Dialogue_Enginebot' — не показывается
    у Pro-владельцев (часть привилегий подписки, наравне с отсутствием рекламы)."""
    async with Session() as s:
        cfg = await s.get(ChildBot, bot_db_id)
    if not cfg:
        return text
    if await referrals.is_pro(cfg.owner_id):
        return text
    return text + f"\n\n<i>🤖 создано на платформе @{PLATFORM_BOT_USERNAME}</i>"


async def inject_extras(bot_db_id: int, text: str) -> str:
    """Реклама + приписка — единая точка вызова для приветственных сообщений."""
    text = await inject_ad(bot_db_id, text)
    text = await inject_footer(bot_db_id, text)
    return text


# =========================================================================
# Кнопки (inline-ссылки/триггеры, reply-клавиатура) — ОБЩИЙ билдер для
# фидбек- и постинг-ботов. Раньше это жило только в child/feedback.py, из-за
# чего в постинг-ботах BotButton вообще ни на что не влиял — кнопки, которые
# владелец добавлял в конструкторе, там просто никогда не рендерились.
# Теперь оба типа ботов используют одну и ту же логику.
# =========================================================================
async def build_keyboards(bot_db_id: int, cfg: ChildBot, extra_inline: list | None = None):
    """Возвращает (InlineKeyboardMarkup|None, ReplyKeyboardMarkup|None)."""
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
    ikb = InlineKeyboardMarkup(inline_keyboard=inline_rows) if inline_rows else None
    rkb = ReplyKeyboardMarkup(keyboard=kb_rows, resize_keyboard=True) if kb_rows else None
    return ikb, rkb


async def send_with_keyboards(m: Message, text: str, ikb, rkb, photo: str | None = None):
    """Отправляет сообщение с клавиатурами.

    БАГ: Telegram не позволяет прикрепить одновременно inline и reply
    клавиатуру к одному сообщению — если слать `reply_markup=ikb or rkb`,
    reply-кнопки (kind="keyboard") ПОЛНОСТЬЮ переставали появляться, как
    только у бота была хоть одна инлайн-кнопка. Если нужны оба вида —
    отправляем инлайн с текстом/фото, а следом отдельным сообщением
    выставляем reply-клавиатуру.

    БАГ: у caption к фото лимит Telegram — 1024 символа (у обычного текста —
    4096). Приветствие с фото + реклама/приписка платформы легко превышали
    1024 символа, и `answer_photo` падал с TelegramBadRequestError —
    "стартовое сообщение с фото падает с ошибкой". Теперь если текст не
    влезает в caption — шлём фото отдельно, а текст отдельным сообщением.
    """
    PHOTO_CAPTION_LIMIT = 1024
    if photo:
        if len(text) <= PHOTO_CAPTION_LIMIT:
            msg = await m.answer_photo(photo, caption=text, reply_markup=ikb or rkb)
        else:
            await m.answer_photo(photo)
            msg = await m.answer(text, reply_markup=ikb or rkb)
    else:
        msg = await m.answer(text, reply_markup=ikb or rkb)
    if ikb and rkb:
        await m.answer(f"{em('gear')} Меню", reply_markup=rkb)
    return msg


async def handle_keyboard_button(m: Message, bot_db_id: int) -> bool:
    """Если m.text совпадает с текстом reply-кнопки — отвечает и возвращает
    True (значит, апдейт обработан и дальше по цепочке идти не нужно)."""
    if not m.text:
        return False
    async with Session() as s:
        b = await s.scalar(select(BotButton).where(
            BotButton.bot_id == bot_db_id, BotButton.kind == "keyboard",
            BotButton.text == m.text))
    if b and b.response_text is not None:
        if b.response_photo:
            await m.answer_photo(b.response_photo, caption=b.response_text or "")
        else:
            await m.answer(b.response_text or "")
        return True
    return False


async def _notify_user(bot: Bot, user_id: int, text: str):
    """Best-effort ЛС пользователю о варне/бане — если он заблокировал бота,
    просто молча игнорируем ошибку."""
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass


async def open_ticket(bot: Bot, cfg: ChildBot, user_id: int, force_new: bool = False) -> Ticket:
    """Открывает (или переиспользует) тикет-переписку с пользователем — общая
    логика для фидбек- И постинг-ботов (топики/реплаи для двусторонней связи
    с админами)."""
    async with Session() as s:
        t = await s.scalar(select(Ticket).where(
            Ticket.bot_id == cfg.id, Ticket.user_id == user_id, Ticket.is_open))
        if t and not force_new:
            return t
        if t and force_new:
            if not getattr(cfg, "always_new_ticket", False):
                return t
            t.is_open = False
            await s.commit()
        topic_id = None
        if cfg.use_topics and cfg.admin_chat_id:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == cfg.id, BotUser.user_id == user_id))
            name = (u.full_name if u else str(user_id))[:80]
            topic = await bot.create_forum_topic(cfg.admin_chat_id, f"✉️ {name} · {user_id}")
            topic_id = topic.message_thread_id
        t = Ticket(bot_id=cfg.id, user_id=user_id, topic_id=topic_id)
        s.add(t)
        await s.commit()
        return t


def build_common_router() -> Router:
    r = Router()

    # ---------- модерация ----------
    @r.message(Command("ban"))
    async def cmd_ban(m: Message, command: CommandObject, bot_db_id: int, bot: Bot):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        parsed = mod.parse_ban_args(command.args or "")
        if not parsed:
            await m.answer(f"{em('info')} Формат: <code>/ban 123456 Причина 7d</code>\n"
                           "Сроки: m/h/d/w/y/perm")
            return
        uid, reason, dur = parsed
        text = await mod.ban_user(bot_db_id, uid, reason, dur,
                                  m.from_user.id, m.from_user.username)
        await m.answer(f"{em('no_entry')} " + text)
        until = "навсегда" if dur == "perm" else dur
        await _notify_user(bot, uid, f"{em('no_entry')} Вы забанены в этом боте "
                           f"({until}).\nПричина: {reason}")

    @r.message(Command("unban"))
    async def cmd_unban(m: Message, command: CommandObject, bot_db_id: int, bot: Bot):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        if not command.args or not command.args.split()[0].isdigit():
            await m.answer("Формат: <code>/unban 123456</code>"); return
        uid = int(command.args.split()[0])
        text = await mod.unban_user(bot_db_id, uid, m.from_user.id, m.from_user.username)
        await m.answer(f"{em('check')} " + text)
        await _notify_user(bot, uid, f"{em('check')} Вы разбанены в этом боте, снова можно писать.")

    @r.message(Command("warn"))
    async def cmd_warn(m: Message, command: CommandObject, bot_db_id: int, bot: Bot):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        parts = (command.args or "").split(maxsplit=1)
        if not parts or not parts[0].isdigit():
            await m.answer("Формат: <code>/warn 123456 Причина</code>"); return
        uid = int(parts[0])
        reason = parts[1] if len(parts) > 1 else "Не указана"
        text, autoban = await mod.warn_user(bot_db_id, uid, reason,
                                            m.from_user.id, m.from_user.username)
        await m.answer(f"{em('warn')} " + text)
        note = f"{em('warn')} Вам выдано предупреждение.\nПричина: {reason}"
        if autoban:
            note += f"\n{em('no_entry')} Достигнут лимит предупреждений — вы забанены."
        await _notify_user(bot, uid, note)

    @r.message(Command("unwarn"))
    async def cmd_unwarn(m: Message, command: CommandObject, bot_db_id: int, bot: Bot):
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        if not command.args or not command.args.split()[0].isdigit():
            await m.answer("Формат: <code>/unwarn 123456</code>"); return
        uid = int(command.args.split()[0])
        text = await mod.unwarn_user(bot_db_id, uid, m.from_user.id, m.from_user.username)
        await m.answer(f"{em('check')} " + text)
        await _notify_user(bot, uid, f"{em('check')} С вас снято предупреждение.")

    # ---------- донат в Stars ----------
    # БАГ: раньше "сумма доната" ловилась голым регэкспом "число из 1-5 цифр"
    # без привязки к состоянию — он матчил ЛЮБОЕ такое сообщение когда угодно
    # в будущем, а не только "следующее сообщение сразу после нажатия доната".
    # Из-за этого один раз нажав донат, пользователь потом получал инвойс на
    # КАЖДОЕ короткое число, которое когда-либо отправлял в бота. Теперь это
    # явное FSM-состояние: слушаем число только пока пользователь реально в
    # процессе доната (после /donate или кнопки), и сбрасываем состояние сразу
    # после отправки инвойса.
    @r.message(Command("donate"), F.chat.type == "private")
    @r.message(F.text.regexp(r"⭐️ Донат"), F.chat.type == "private")
    async def donate_start(m: Message, bot_db_id: int, state: FSMContext):
        async with Session() as s:
            cfg = await s.get(ChildBot, bot_db_id)
        if not cfg.donate_enabled:
            return
        await state.set_state(DonateSt.amount)
        await m.answer(f"{em('star')} Введите количество звёзд для доната (1–10000):")

    @r.message(DonateSt.amount, F.chat.type == "private")
    async def donate_amount(m: Message, bot: Bot, bot_db_id: int, state: FSMContext):
        await state.clear()
        if not m.text or not m.text.strip().isdigit():
            await m.answer(f"{em('warn')} Нужно целое число звёзд (1–10000). "
                           "Попробуйте /donate ещё раз.")
            return
        async with Session() as s:
            cfg = await s.get(ChildBot, bot_db_id)
        if not cfg.donate_enabled:
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
    async def cb_donate(c: CallbackQuery, bot_db_id: int):
        # БАГ: инлайн-кнопка доната генерировалась с callback_data="donate_btn",
        # но обработчик регистрировался только в фидбек-ботах — в постинг-ботах
        # его не было вообще, хотя настройка доната общая для обоих типов.
        async with Session() as s:
            cfg = await s.get(ChildBot, bot_db_id)
        if not cfg.donate_enabled:
            await c.answer()
            return
        await c.message.answer(f"{em('star')} Введите количество звёзд для доната (1–10000):")
        await c.answer()

    # ---------- триггер-кнопки и кнопка "открыть обращение" ----------
    # БАГ: раньше эти два хендлера жили только в child/feedback.py, хотя
    # клавиатуру с этими же callback_data строит ОБЩИЙ build_keyboards()
    # (выше в этом файле) для ОБОИХ типов ботов. bot_manager подключает на
    # бота либо feedback-роутер, либо posting-роутер — никогда оба сразу.
    # В итоге в постинг-ботах кнопка рисовалась, а обработчика на её
    # callback_data не было вообще ни у одного роутера — Telegram видел
    # "необработанный" callback (эффект "0 мс / ошибка"). Перенесены сюда,
    # в build_common_router(), который подключается ко ВСЕМ ботам.
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

    @r.callback_query(F.data == "open_ticket")
    async def cb_open_ticket(c: CallbackQuery, bot: Bot, bot_db_id: int):
        async with Session() as s:
            cfg = await s.get(ChildBot, bot_db_id)
        await open_ticket(bot, cfg, c.from_user.id, force_new=True)
        await c.answer("Обращение открыто! Напишите сообщение.", show_alert=True)

    return r
