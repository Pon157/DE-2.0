from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, BufferedInputFile, WebAppInfo)
from aiogram.utils.token import validate_token
from sqlalchemy import select, func
from db.base import Session
from db.models import (ChildBot, BotAdmin, BotButton, BotType, OpenMode, ForwardMode,
                       Advertisement, AdKind, AdStatus, PlatformUser, BotUser,
                       Survey, SurveyQuestion, Donation, AutoReply, AutoReplyKind)
from services.bot_manager import (manager, reupload_photo_for_bot as manager_reupload,
                                  reupload_media_for_bot as manager_reupload_media)
from services.broadcast import run_broadcast
from services.stats_image import build_stats_image
from services import ads as ads_service
from services import payments as pay_service
from services import referrals
from services import moderation as mod
from utils.emoji import em, styled_button
from utils import crypto
from utils.raw_api import get_star_balance
from config import (FLOW_MINIAPP_URL, SUPER_ADMIN_ID, MASTER_BOT_TOKEN,
                    BROADCAST_RATE, STATS_DAYS, AD_MAX_LEN,
                    AD_BROADCAST_COOLDOWN_DAYS, PRO_PRICE_RUB,
                    PLATFORM_BOT_USERNAME)
from child.common import RESERVED_COMMANDS, message_media
from master import legal
import config
import json
import base64
import logging
import re
from datetime import datetime

router = Router()
log = logging.getLogger("master.router")


async def _has_accepted_terms(user_id: int) -> bool:
    async with Session() as s:
        u = await s.get(PlatformUser, user_id)
    return bool(u and u.accepted_terms)


async def _mark_terms_accepted(user_id: int):
    from datetime import datetime
    async with Session() as s:
        u = await s.get(PlatformUser, user_id)
        if not u:
            u = PlatformUser(id=user_id)
            s.add(u)
        u.accepted_terms = True
        u.accepted_terms_at = datetime.utcnow()
        await s.commit()


# ---------------------------------------------------------------------------
# Антиспам и бан в САМОМ КОНСТРУКТОРЕ (master-боте).
# Раньше антиспам был только у дочерних ботов и НИКОГДА не применялся в
# master-роутере — если человек флудил в конструктор напрямую, его тут
# ничего не тормозило. Здесь — простой rate-limit в памяти процесса,
# применяется абсолютно ко всем, включая владельца/SUPER_ADMIN_ID (это
# сознательно: конструктором тоже можно флудить, тестировать нужно на всех).
# ---------------------------------------------------------------------------
from collections import deque
import time as _time

_ROUTER_RATE_MAX = 40       # сообщений/нажатий кнопок
_ROUTER_RATE_WINDOW = 15.0  # за столько секунд
# БАГ ("кнопка плохо прожимается" в /pro и вообще где угодно в конструкторе):
# порог 15 действий/10с был общим на сообщения+нажатия кнопок сразу — при
# обычной быстрой навигации по меню (несколько тапов подряд) он легко
# выбивался, и нажатие просто тихо игнорировалось (`await event.answer()`
# без изменения экрана — визуально неотличимо от "кнопка не сработала").
# Подняли порог заметно выше — он всё ещё призван ловить именно флуд-ботов,
# а не наказывать человека за то, что он быстро тыкает по меню.
_router_hits: dict[int, deque] = {}


async def _router_antispam_allowed(user_id: int) -> bool:
    now = _time.monotonic()
    dq = _router_hits.setdefault(user_id, deque())
    dq.append(now)
    while dq and now - dq[0] > _ROUTER_RATE_WINDOW:
        dq.popleft()
    return len(dq) <= _ROUTER_RATE_MAX


@router.message.outer_middleware()
async def _master_guard_message(handler, event: Message, data: dict):
    uid = event.from_user.id if event.from_user else None
    if uid is not None:
        if await mod.is_platform_banned(uid):
            return  # забанен в конструкторе — молча игнорируем
        if not await _router_antispam_allowed(uid):
            return  # флуд — молча игнорируем (без наказания, просто троттлинг)
        # Согласие с политикой конфиденциальности/соглашением/политикой
        # возвратов — показывается ОДИН РАЗ, до этого бот не отвечает вообще
        # ничем (кроме самого экрана согласия), даже на /start. /info
        # разрешён всегда — это как раз просмотр текстов документов.
        if event.text != "/info" and not await _has_accepted_terms(uid):
            await event.answer(legal.GATE_TEXT, reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Принимаю",
                                                       callback_data="accept_terms")]]))
            return
        # Капча — ПОКАЗЫВАЕТСЯ ВСЕМ, включая владельца/SUPER_ADMIN_ID, без
        # исключений (раньше в master-роутере капчи не было вообще).
        #
        # БАГ (по репорту: "сделал новый вопрос в анкете — следующий вопрос
        # нельзя сделать, пока не /start"): счётчик капчи растёт на КАЖДОЕ
        # текстовое сообщение, включая ввод текста внутри многошаговых FSM-
        # сценариев (текст вопроса анкеты, текст кнопки и т.п.). Когда
        # срабатывал показ капчи, ожидаемый ввод сценария молча "съедался"
        # этим guard'ом (return до вызова handler), FSM-состояние
        # оставалось прежним, а следующее сообщение пользователя (он не
        # понимал, что от него ждут решения примера) уходило уже как
        # "неверный ответ на капчу" — переписка зацикливалась, и реально
        # помогал только /start (сбрасывающий его в состояние без proверки,
        # либо совпадающий по времени с истечением капчи через
        # CAPTCHA_TIMEOUT_MINUTES). Исправление: пока у пользователя активно
        # состояние FSM (он на middle of a конкретного шага конструктора —
        # печатает текст вопроса, кнопки и т.п.), капчу не показываем и не
        # тратим на это тик счётчика — это одно связное действие
        # администратора, а не свободный флуд.
        state: FSMContext | None = data.get("state")
        in_fsm_flow = False
        if state is not None:
            in_fsm_flow = (await state.get_state()) is not None
        if not in_fsm_flow:
            notice = await mod.check_platform_captcha(uid, event.text)
            if notice is not None:
                await event.answer(notice)
                return
    return await handler(event, data)


@router.callback_query.outer_middleware()
async def _master_guard_callback(handler, event: CallbackQuery, data: dict):
    uid = event.from_user.id if event.from_user else None
    if uid is not None:
        if await mod.is_platform_banned(uid):
            await event.answer("Вы забанены в конструкторе.", show_alert=True)
            return
        if not await _router_antispam_allowed(uid):
            await event.answer()
            return
        if event.data != "accept_terms" and not await _has_accepted_terms(uid):
            await event.answer("Сначала примите условия использования (/start).", show_alert=True)
            return
    return await handler(event, data)


@router.callback_query(F.data == "accept_terms")
async def cb_accept_terms(c: CallbackQuery):
    await _mark_terms_accepted(c.from_user.id)
    await c.message.edit_text(f"{em('check')} Спасибо! Условия приняты.")
    try:
        await c.message.answer_sticker(WELCOME_STICKER_ID)
    except Exception:
        pass
    await show_main(c.message, user_id=c.from_user.id)
    await c.answer()


@router.message(Command("info"))
async def cmd_info(m: Message):
    await m.answer(legal.info_text())


class St(StatesGroup):
    add_token = State()
    add_type = State()
    bc_content = State()
    bc_target = State()
    set_welcome = State()
    set_admin_chat = State()
    set_header = State()
    set_template = State()
    set_topic_name = State()
    set_welcome_effect = State()
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
    # /ads (покупка рекламы) — работает в МАСТЕР-боте, а не в дочерних
    ads_pick_kind = State()
    ads_pick_bot = State()
    ads_text = State()
    ads_impr_custom = State()
    ads_confirm = State()
    ticket_btn_text = State()
    ticket_btn_style = State()
    ticket_btn_icon = State()
    tpl_btn_edit = State()
    tpl_btn_style = State()
    tpl_btn_icon = State()
    ap_ban_add = State()
    ban_by_id = State()
    antispam_cfg = State()
    set_survey_finish = State()
    survey_name = State()
    survey_q_text = State()
    survey_q_options = State()
    btn_pick_survey = State()
    ap_pro_user = State()
    ap_pro_days = State()
    set_close_notify = State()
    set_admin_reaction = State()
    btn_disown_text = State()
    close_btn_text = State()
    close_btn_style = State()
    close_btn_icon = State()
    donate_btn_text = State()
    donate_btn_style = State()
    donate_btn_icon = State()
    # автоответы
    ar_text = State()
    ar_param = State()
    ar_photo = State()


HEADER_MODE_LABELS = {
    "separate": "отдельным сообщением",
    "merge": "слитно с сообщением",
    "off": "выкл",
}


def kb(rows):
    def _btn(item):
        if len(item) == 3:
            text, kind, value = item
            if kind == "web_app":
                # Mini App кнопка — открывает URL прямо внутри Telegram
                return InlineKeyboardButton(text=text,
                                            web_app=WebAppInfo(url=value))
            # иначе третий элемент — обычная url-кнопка (старое поведение)
            return styled_button(text, url=value)
        text, data = item
        return styled_button(text, callback_data=data)
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(item) for item in row] for row in rows])


def nav_kb(bot_id: int, back_data: str | None = None):
    """Клавиатура ПОСЛЕ сохранения настройки.

    БАГ UX: раньше после каждого «Сохранено!» не было ни одной кнопки —
    чтобы продолжить настройку, приходилось заново заходить в меню бота с
    самого начала. Теперь после каждого сохранения есть быстрый возврат.
    """
    rows = []
    if back_data:
        rows.append([("⬅️ Назад", back_data)])
    rows.append([("⚙️ Настройки", f"cfg:{bot_id}")])
    rows.append([("🤖 Меню бота", f"bot:{bot_id}")])
    return kb(rows)


def capture_media(m: Message):
    """Достаёт (file_id, media_type) из сообщения — поддержка ВСЕХ основных
    типов медиа для рассылок."""
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


async def _download_media_b64(bot: Bot, file_id: str) -> tuple[str | None, str | None]:
    """Скачивает файл по file_id (полученному ЭТИМ ботом) и возвращает
    (base64-байты, имя файла) для последующей ПЕРЕЗАЛИВКИ в другого бота.

    БАГ (критичный, объясняет "0 доставлено"/массу ошибок при рассылке):
    медиа для рассылки захватывается в чате с МАСТЕР-ботом, а рассылать его
    нужно ДОЧЕРНИМИ ботами — file_id одного бота недействителен для
    другого ("wrong file identifier"). Раньше file_id передавался в
    run_broadcast как есть, и КАЖДАЯ отправка с медиа падала. Теперь байты
    скачиваются один раз через master-бота и передаются дальше, чтобы
    дочерний бот мог сам загрузить их и получить СВОЙ валидный file_id."""
    try:
        tg_file = await bot.get_file(file_id)
        buf = await bot.download_file(tg_file.file_path)
        data = buf.read()
        filename = (tg_file.file_path.rsplit("/", 1)[-1]
                   if tg_file.file_path else "file")
        return base64.b64encode(data).decode(), filename
    except Exception:
        log.exception("Не удалось скачать медиа file_id=%s для рассылки", file_id)
        return None, None


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _plain_preview(html_text: str, limit: int) -> str:
    """Укороченный текст БЕЗ html-тегов — для превью в списках (например,
    'Мои кампании'). По запросу текст объявлений теперь поддерживает
    форматирование (жирный/курсив/ссылки), поэтому наивная обрезка по
    символам могла бы разрезать тег пополам и сломать рендер сообщения —
    для превью просто убираем разметку целиком."""
    plain = _HTML_TAG_RE.sub("", html_text or "")
    return plain[:limit] + ("…" if len(plain) > limit else "")


async def _access(bot_id: int, user_id: int) -> tuple[ChildBot | None, bool]:
    """(бот, is_owner). None если нет доступа вообще."""
    async with Session() as s:
        cb = await s.get(ChildBot, bot_id)
        if not cb:
            return None, False
        if cb.owner_id == user_id or (SUPER_ADMIN_ID and user_id == SUPER_ADMIN_ID):
            # Супер-админ платформы получает полный доступ к любому боту.
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


async def _child_bot_can_access(token: str, chat_id: int) -> bool:
    """Проверяем, что ДОЧЕРНИЙ бот реально имеет доступ к чату/каналу, ПЕРЕД
    тем как сохранить id.

    БАГ: id сохранялся вслепую — опечатка или «бот не добавлен в чат»
    обнаруживались только когда сообщения молча не доходили."""
    test = Bot(token)
    try:
        await test.get_chat(chat_id)
        return True
    except Exception:
        return False
    finally:
        try:
            await test.session.close()
        except Exception:
            pass


# ================== главное меню ==================
WELCOME_STICKER_ID = "CAACAgEAAxkBAAEFhKhqWilcvpZc4woyhjArQLamLF7lcgACdQIAAutt2EfwJl7czJ5z4z0E"


@router.message(CommandStart())
async def start(m: Message, command: CommandObject):
    if await mod.is_platform_banned(m.from_user.id):
        await m.answer(f"{em('no_entry')} Вы забанены в конструкторе и не можете им пользоваться.")
        return
    await referrals.register_start(m.from_user.id, command.args)
    try:
        await m.answer_sticker(WELCOME_STICKER_ID)
    except Exception:
        pass  # стикер не критичен — не роняем /start, если он вдруг недоступен
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
                       "рекламы — её нельзя ни купить, ни разослать в них. "
                       "Также доступны: иконки топиков, эффекты приветствия, "
                       "Stars-подписки на донат и рич-текст приветствия.")
        return
    await m.answer(
        f"{em('sparkles')} <b>Dialogue Engine Pro</b> — {config.PRO_PRICE_RUB} ₽/мес\n\n"
        "В ваших ботах не будет рекламы: её нельзя будет купить для показа, и "
        "в них не попадут глобальные рекламные рассылки.\n\n"
        "Плюс Pro-функции для всех ваших ботов:\n"
        "• 🧵 Иконка форум-топика (premium-эмодзи)\n"
        "• 🎆 Эффект на приветственном сообщении\n"
        "• 🔁 Ежемесячная Stars-подписка на донат (не только разово)\n"
        "• 🎨 Рич-текст приветствия (новый формат Bot API — заголовки, "
        "разделители, врезки-цитаты)\n\n"
        "Также Pro можно получить бесплатно за приглашённых — см. /ref",
        reply_markup=kb([[("💳 Купить Pro", "buy_pro")]]))


async def _require_pro(c: CallbackQuery, owner_id: int) -> bool:
    """Общий гейт для Pro-фич (иконка топика/эффект приветствия/рич-текст/
    Stars-подписка) — доступны, только если у ВЛАДЕЛЬЦА бота активен Pro
    (та же логика, что уже используется для отключения рекламы, см.
    services/ads.py::bot_is_pro_protected). Возвращает True, если можно
    продолжать."""
    if await referrals.is_pro(owner_id):
        return True
    await c.answer(f"{em('sparkles')} Это Pro-функция — оформите Pro командой /pro "
                   "в главном меню (или бесплатно за рефералов, см. /ref).",
                   show_alert=True)
    return False


@router.callback_query(F.data == "buy_pro")
async def buy_pro(c: CallbackQuery):
    try:
        payment_id, url = await pay_service.create_pro_payment(c.from_user.id, months=1)
    except RuntimeError as e:
        await c.answer(str(e), show_alert=True)
        return
    try:
        await c.message.edit_text(
            f"{em('sparkles')} Оплата Pro-подписки ({config.PRO_PRICE_RUB} ₽/мес). "
            "После оплаты Pro активируется автоматически в течение минуты.",
            reply_markup=kb([[(f"💳 Оплатить {config.PRO_PRICE_RUB} ₽", None, url)]]))
    except TelegramBadRequest:
        # Двойное нажатие ("кнопка плохо прожимается") — редактирование тем
        # же текстом падает с "message is not modified" и раньше просто
        # падало необработанным исключением, будто кнопка не сработала.
        pass
    await c.answer()


async def show_main(m: Message, edit: bool = False, user_id: int | None = None):
    # user_id передаётся явно: c.message.from_user — это бот, а не человек.
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
        [("📣 Постинг в канал", "type:posting")],
        [("📝 Анкета", "type:survey")],
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

    # БАГ: при не-текстовом сообщении (фото/стикер) m.text is None ->
    # AttributeError и молчание.
    if not m.text:
        msg = await m.answer(f"{em('cross')} Нужен токен текстом. Попробуйте снова.")
        await state.update_data(last_msg_id=msg.message_id)
        return

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
        # БАГ: с шифрованием токена (Fernet, не детерминированное) сравнение
        # `ChildBot.token == token` больше никогда не совпадёт с уже
        # сохранённой (по-другому зашифрованной) записью — дубликаты
        # перестали бы отлавливаться молча. Ищем по token_fingerprint
        # (детерминированный HMAC) вместо самого токена.
        fp = crypto.token_fingerprint(token)
        exists = await s.scalar(select(ChildBot).where(ChildBot.token_fingerprint == fp))
        if exists:
            msg = await m.answer("Этот бот уже добавлен.")
            await state.update_data(last_msg_id=msg.message_id)
            return

        cb = ChildBot(owner_id=m.from_user.id, token=token, token_fingerprint=fp,
                      bot_tg_id=me.id, username=me.username, bot_type=BotType(data["bot_type"]))
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
        [("📣 Рассылка", f"bc:{bot_id}"), ("📊 Статистика", f"stats:{bot_id}")],
        [("📖 Методичка по этому боту", f"guide:{bot_id}")]]
    if is_owner:
        rows += [
            [("⚙️ Настройки", f"cfg:{bot_id}")],
            [("🔘 Кнопки и команды", f"btns:{bot_id}"),
             ("👥 Админы", f"admins:{bot_id}")],
            [("⏯ Вкл/выкл", f"toggle:{bot_id}"), ("🔄 Перезапустить", f"restartbot:{bot_id}")],
            [("🗑 Удалить", f"del:{bot_id}")]]
    rows.append([("⬅️ Назад", "main")])
    status = "🟢 работает" if cb.is_active else "🔴 остановлен"
    await c.message.edit_text(
        f"🤖 <b>@{cb.username}</b> · {cb.bot_type.value} · {status}",
        reply_markup=kb(rows))
    await c.answer()


# ================== методичка по типу бота (по запросу) ==================
# Показывается прямо в меню бота — доступна и владельцу, и админам, чтобы не
# держать инструкцию где-то отдельно. Текст зависит от cb.bot_type.
def _pro_guide_block() -> str:
    return (
        f"\n\n{em('sparkles')} <b>Pro-функции этого бота</b> (/pro у владельца):\n"
        f"• {em('star')} Эффект на приветственном сообщении\n"
        f"• {em('sparkles')} Рич-текст приветствия (заголовки/разделители/врезки — новый формат, "
        "тумблер «Рич-текст» в настройках)\n"
    )

def _bot_guide(bot_type: str) -> str:
    guides: dict[str, str] = {
    "feedback": (
        f"{em('bookmark')} <b>Бот-обращения (feedback)</b>\n\n"
        "Подписчик пишет боту → создаётся обращение и пересылается в чат "
        "админов (⚙️ Настройки → «Чат админов»). Админ отвечает <b>реплаем</b> "
        "на пересланное сообщение — ответ уходит подписчику автоматически.\n\n"
        "• <b>Режим открытия</b>: по первому сообщению / по /start / по кнопке.\n"
        "• <b>Топики</b>: если чат админов — форум, каждому подписчику можно "
        "выделять свой топик (⚙️ → «Топики»). Имя топика настраивается шаблоном "
        "с {name}/{username}/{id}.\n"
        "• <b>«1 пользователь = 1 обращение»</b> — новое сообщение не открывает "
        "второе обращение, пока не закрыто текущее (/close или кнопка).\n"
        "• <b>Антиспам</b>: капча + лимит сообщений в окно времени, настраивается "
        "в «Пороги».\n"
        "• <b>Донат</b>: кнопка доната через Telegram Stars, включается отдельно.\n"
        "• Кнопки конструктора (🔘) поддерживают HTML-стили и premium-эмодзи."
        + _pro_guide_block()
    ),
    "posting": (
        f"{em('bookmark')} <b>Бот-постинг (предложка + публикация в канал)</b>\n\n"
        "Подписчики присылают посты в бота → они попадают в чат админов на "
        "модерацию (или сразу в канал, если предложка выключена). Админ "
        "публикует пост кнопкой под пересланным сообщением.\n\n"
        "• <b>Канал</b> и <b>чат админов</b> настраиваются в ⚙️ Настройки.\n"
        "• <b>Шаблон поста</b> — оформление публикации (шапка/подпись), "
        "поддерживает HTML и premium-эмодзи; переменные — в разделе шаблона.\n"
        "• <b>Публикация</b>: пересылкой («Forwarded from») или копией без "
        "пометки исходного автора — переключается тумблером.\n"
        "• <b>Топики</b> в чате админов — предложки от разных подписчиков "
        "уходят в свои топики.\n"
        "• Альбомы (несколько фото/видео одним постом) публикуются одним "
        "постом, а не по одному файлу."
        + _pro_guide_block()
    ),
    "survey": (
        f"{em('bookmark')} <b>Бот-анкета (survey)</b>\n\n"
        "Владелец собирает одну или несколько анкет — наборов вопросов "
        "(свободный текст или варианты-кнопки). Респондент проходит анкету по "
        "кнопке из меню, заполненная анкета уходит в чат админов.\n\n"
        "• <b>«📋 Анкеты»</b> в настройках — создание анкет и вопросов. Вопрос "
        "может содержать HTML-форматирование, premium-эмодзи и медиа-вложение "
        "(фото/видео/файл/гиф/аудио) — присылайте их прямо при добавлении вопроса.\n"
        "• <b>«✅ Текст после заполнения»</b> — тоже поддерживает форматирование "
        "и медиа.\n"
        "• <b>Топики</b>: включите в настройках — каждому респонденту будет "
        "выделяться свой топик в чате админов, чтобы не путать анкеты разных "
        "людей.\n"
        "• <b>Диалог с респондентами</b> — если включить, админ может ответить "
        "реплаем на присланную анкету, респондент увидит ответ в боте и сможет "
        "продолжить переписку.\n"
        "• Респондент может отменить текущее незавершённое прохождение "
        "командой /close и начать заново."
        + _pro_guide_block()
    ),
    }
    return guides.get(bot_type, "Методичка для этого типа бота пока не готова.")


@router.callback_query(F.data.startswith("guide:"))
async def bot_guide(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb:
        await c.answer("Нет доступа", show_alert=True); return
    text = _bot_guide(cb.bot_type.value)
    await c.message.edit_text(text, reply_markup=kb([[("⬅️ Назад", f"bot:{bot_id}")]]))
    await c.answer()


# ================== настройки ==================
@router.callback_query(F.data.startswith("cfg:"))
async def cfg_menu(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return

    # Проверяем Pro — кнопка сценариев показывается только Pro-владельцам
    owner_is_pro = await referrals.is_pro(cb.owner_id)

    header_label = HEADER_MODE_LABELS.get(cb.header_mode or "separate", "отдельным сообщением")
    if cb.bot_type == BotType.feedback:
        rows = [
            [("✉️ Открытие обращений: " + cb.open_mode.value, f"cyc_open:{bot_id}")],
            [(f"📨 Пересылка сообщений в чат админов: {cb.forward_mode.value}", f"cyc_fwd:{bot_id}")],
            [("🧵 Топики: " + ("вкл" if cb.use_topics else "выкл"), f"cyc_topics:{bot_id}"),
             ("🧵 Имя топика", f"topicname:{bot_id}"), ("Цвет топика", f"topicicon:{bot_id}")],
            [(f"📌 Закреп 1-го сообщения (без топиков): {'вкл' if cb.pin_first_message else 'выкл'}",
              f"cyc_pinfirst:{bot_id}")],
            [("👋 Приветствие", f"welcome:{bot_id}"),
             ("🏷 Шаблон шапки", f"header:{bot_id}")],
            [("🎆 Эффект приветствия", f"welcomefx:{bot_id}")],
            [(f"🏷 Шапка: {header_label}", f"cyc_header:{bot_id}")],
            [("🏠 Чат админов", f"admchat:{bot_id}"),
             (f"⚠️ Лимит варнов: {cb.warn_limit}", f"warnlim:{bot_id}")],
            [("⭐️ Донат: " + ("вкл" if cb.donate_enabled else "выкл"), f"cyc_donate:{bot_id}")],
            [("⭐️ Тип кнопки доната: " + cb.donate_button_type, f"cyc_donbtn:{bot_id}")],
            [("✉️ Кнопка обращения", f"ticketbtn:{bot_id}")],
            [("❌ Кнопка «Закрыть обращение» (текст/цвет/эмодзи/удаление)", f"closebtn:{bot_id}")],
            [("🔒 Кнопка закрытия в admin-чате (inline)", f"admin_closebtn:{bot_id}")],
            [("⭐️ Текст/цвет/эмодзи кнопки доната", f"donatebtn:{bot_id}")],
            [("🔄 Restart/кнопка: " + ("новый тикет" if cb.always_new_ticket else "тот же тикет"),
              f"cyc_newticket:{bot_id}")],
            [(f"🛡 Антиспам: {'вкл' if cb.antispam_enabled else 'выкл'}", f"cyc_antispam:{bot_id}"),
             ("🛡 Пороги", f"antispamcfg:{bot_id}")],
            [(f"🛡 Антиспам трогает владельца: {'нет' if cb.antispam_ignore_owner else 'да'}",
              f"cyc_aspown:{bot_id}")],
            [("🔒 Текст закрытия обращения", f"closenotify:{bot_id}")],
            [(f"👍 Реакция на ответ админа: {cb.admin_reply_reaction or 'выкл'}",
              f"adminreaction:{bot_id}")],
            [("💬 Автоответы", f"ar_list:{bot_id}")],
        ]
    elif cb.bot_type == BotType.survey:
        rows = [
            [("👋 Приветствие", f"welcome:{bot_id}"),
             ("🎆 Эффект приветствия", f"welcomefx:{bot_id}")],
            [("📋 Анкеты (вопросы)", f"surveys:{bot_id}")],
            [("✅ Текст после заполнения анкеты", f"surveyfinish:{bot_id}")],
            [("🏠 Чат админов (куда приходят заполненные анкеты)", f"admchat:{bot_id}")],
            [(f"💬 Диалог с респондентами: {'вкл' if cb.survey_dialog_enabled else 'выкл'}",
              f"cyc_surveydialog:{bot_id}")],
            [("🧵 Топики: " + ("вкл" if cb.use_topics else "выкл"), f"cyc_topics:{bot_id}"),
             ("🧵 Имя топика", f"topicname:{bot_id}"), ("🧵 Иконка топика", f"topicicon:{bot_id}")],
            [(f"⚠️ Лимит варнов: {cb.warn_limit}", f"warnlim:{bot_id}")],
            [(f"🛡 Антиспам: {'вкл' if cb.antispam_enabled else 'выкл'}", f"cyc_antispam:{bot_id}"),
             ("🛡 Пороги", f"antispamcfg:{bot_id}")],
            [(f"🛡 Антиспам трогает владельца: {'нет' if cb.antispam_ignore_owner else 'да'}",
              f"cyc_aspown:{bot_id}")],
            [(f"👍 Реакция на ответ админа: {cb.admin_reply_reaction or 'выкл'}",
              f"adminreaction:{bot_id}")],
        ]
    else:
        rows = [
            [("📮 Предложка: " + ("вкл" if cb.accept_suggestions else "выкл"),
              f"cyc_sugg:{bot_id}")],
            [("👋 Приветствие", f"welcome:{bot_id}"),
             ("🎆 Эффект приветствия", f"welcomefx:{bot_id}")],
            [("🎨 Шаблон поста", f"template:{bot_id}"), ("🔘 Кнопки шаблона", f"tplbtn:{bot_id}")],
            [("📡 Канал", f"channel:{bot_id}"), ("🏠 Чат админов", f"admchat:{bot_id}")],
            [(f"📨 Пересылка предложки в чат админов: {cb.forward_mode.value}", f"cyc_fwd:{bot_id}")],
            [(f"🏷 Шапка: {header_label}", f"cyc_header:{bot_id}"),
             ("🏷 Шаблон шапки", f"header:{bot_id}")],
            [("🧵 Топики в чате админов: " + ("вкл" if cb.use_topics else "выкл"),
              f"cyc_topics:{bot_id}"), ("🧵 Имя топика", f"topicname:{bot_id}"),
             ("🧵 Иконка топика", f"topicicon:{bot_id}")],
            [("📬 Контент поста: " + ("по шаблону" if cb.channel_delivery_mode == "template" else "оригинал"),
              f"cyc_delivery:{bot_id}")],
            [(f"📤 ПУБЛИКАЦИЯ В КАНАЛ: {'пересылка (Forwarded from)' if cb.channel_publish_mode == 'forward' else 'копия (без пометки)'}",
              f"cyc_pubmode:{bot_id}")],
            [(f"⚠️ Лимит варнов: {cb.warn_limit}", f"warnlim:{bot_id}")],
            [(f"🛡 Антиспам: {'вкл' if cb.antispam_enabled else 'выкл'}", f"cyc_antispam:{bot_id}"),
             ("🛡 Пороги", f"antispamcfg:{bot_id}")],
            [(f"🛡 Антиспам трогает владельца: {'нет' if cb.antispam_ignore_owner else 'да'}",
              f"cyc_aspown:{bot_id}")],
        ]

    # Кнопка «⚡ Сценарии» — только Pro.
    # Передаём bot_id как query-параметр; Mini App читает его из
    # window.location.search и передаёт в каждый API-запрос.
    # Кнопка типа web_app открывает Mini App прямо внутри Telegram без
    # отдельного браузера — пользователь не покидает мессенджер.
    if owner_is_pro:
        flow_url = f"{FLOW_MINIAPP_URL}?bot_id={bot_id}"
        rows.append([("⚡ Сценарии (Pro)", "web_app", flow_url)])
    else:
        # Не-Pro видят заблокированную кнопку — тапнув, получат пояснение
        rows.append([("⚡ Сценарии (только Pro)", f"scenarios_nopro:{bot_id}")])

    rows.append([("⬅️ Назад", f"bot:{bot_id}")])
    await c.message.edit_text(f"⚙️ Настройки @{cb.username}", reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("scenarios_nopro:"))
async def scenarios_nopro(c: CallbackQuery):
    """Заглушка для не-Pro владельцев — объясняем что нужна подписка."""
    bot_id = int(c.data.split(":")[1])
    await c.answer(
        "⚡ Сценарии — функция Pro-подписки.\n"
        "Оформите Pro в главном меню чтобы получить доступ к визуальному "
        "редактору флоу для этого бота.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("antispamcfg:"))
async def antispamcfg_start(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    await state.set_state(St.antispam_cfg)
    await state.update_data(bot_id=bot_id, last_msg_id=c.message.message_id)
    await c.message.edit_text(
        f"{em('shield')} Текущие пороги: не больше <b>{cb.rate_limit_max}</b> сообщений за "
        f"<b>{cb.rate_limit_window}</b> сек, капча каждые <b>{cb.captcha_every}</b> сообщений.\n\n"
        "Пришлите три числа через пробел: <code>max_сообщений окно_сек капча_каждые</code>\n"
        "Например: <code>6 10 20</code>")
    await c.answer()


@router.message(St.antispam_cfg)
async def antispamcfg_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    parts = (m.text or "").split()
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        msg = await m.answer(f"{em('warn')} Нужно три числа через пробел, например "
                             "<code>6 10 20</code>. Попробуйте ещё раз или /cancel.")
        await state.update_data(last_msg_id=msg.message_id)
        return
    rate_max, rate_window, captcha_every = (int(p) for p in parts)
    data = await state.get_data()
    bot_id = data["bot_id"]
    cb, is_owner = await _access(bot_id, m.from_user.id)
    if not cb or not is_owner:
        await state.clear()
        return
    async with Session() as s:
        obj = await s.get(ChildBot, bot_id)
        obj.rate_limit_max = max(1, rate_max)
        obj.rate_limit_window = max(1, rate_window)
        obj.captcha_every = captcha_every  # 0 = выключить капчу
        await s.commit()
    await state.clear()
    await m.answer(f"{em('check')} Пороги антиспама сохранены!", reply_markup=nav_kb(bot_id))


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
    try:
        await m.delete()
    except Exception:
        pass

    file_id, media_type = capture_media(m)
    media_b64 = media_filename = None
    if file_id:
        media_b64, media_filename = await _download_media_b64(m.bot, file_id)
        if media_b64 is None:
            # Скачать не удалось — отправляем без медиа, чтобы не потерять
            # рассылку целиком, но предупреждаем администратора.
            media_type = None
            await m.answer(f"{em('warn')} Не удалось скачать медиа для рассылки, "
                           "разошлю только текст (если он есть).")

    await state.update_data(html_text=m.html_text if (m.text or m.caption) else None,
                            media_b64=media_b64, media_filename=media_filename,
                            media_type=media_type)
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
        try:
            await m.bot.delete_message(m.chat.id, data["last_msg_id"])
        except Exception:
            pass
        msg = await m.answer(text, reply_markup=markup)
        await state.update_data(last_msg_id=msg.message_id)


@router.callback_query(St.bc_target, F.data.startswith("bct:"))
async def bc_go(c: CallbackQuery, state: FSMContext):
    target = c.data.split(":")[1]
    data = await state.get_data()
    bot_id = data["bot_id"]
    await state.clear()
    async with Session() as s:
        cb = await s.get(ChildBot, bot_id)

    msg = await c.message.edit_text(f"{em('hourglass')} Рассылка запущена...")

    async def progress(done, total):
        try:
            await msg.edit_text(f"{em('hourglass')} Рассылка: {done}/{total}")
        except Exception:
            pass

    media_b64 = data.get("media_b64")
    result = await run_broadcast(
        cb.token, cb.id, target=target,
        html_text=data["html_text"],
        media_bytes=base64.b64decode(media_b64) if media_b64 else None,
        media_filename=data.get("media_filename"),
        media_type=data["media_type"],
        rich=(cb.rich_welcome and await referrals.is_pro(cb.owner_id)),
        progress_cb=progress)
    await msg.edit_text(
        f"{em('check')} <b>Рассылка завершена</b>\n\n"
        f"Всего: {result['total']}\n✅ Доставлено: {result['sent']}\n"
        f"🚫 Заблокировали бота: {result['blocked']}\n❌ Ошибки: {result['failed']}",
        reply_markup=nav_kb(bot_id))
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
    # НОВОЕ (по запросу): та же ключевая статистика ПРОСТЫМ ТЕКСТОМ, отдельным
    # сообщением — без статистики по админам, только пользователи/блокировки/
    # баны. Удобно копировать/пересылать, в отличие от картинки.
    user_stats = await mod.user_stats_text(bot_id)
    await c.message.answer(f"{em('chart')} <b>Статистика (кратко)</b>\n\n{user_stats}")
    # Статистика по админам — отдельным текстовым блоком.
    admin_stats = await mod.admin_stats_text(bot_id)
    # НОВОЕ: баланс Telegram Stars бота (getMyStarBalance, Bot API 9.6) —
    # видно сразу, сколько накопилось через донаты, без похода в @BotFather.
    star_line = ""
    if cb.donate_enabled:
        child_bot = manager.bots.get(bot_id)
        if child_bot:
            balance = await get_star_balance(child_bot)
            if balance is not None:
                star_line = f"\n\n⭐️ Баланс Stars: <b>{balance}</b>"
    await c.message.answer(f"{em('crown')} <b>Статистика по админам</b>\n\n{admin_stats}{star_line}",
                           reply_markup=kb([[("🚫 Кто заблокировал бота", f"blockedlist:{bot_id}:0")],
                                            [("⬅️ Назад", f"bot:{bot_id}")]]))


@router.callback_query(F.data.startswith("sublist:"))
async def sub_list(c: CallbackQuery):
    """НОВОЕ (докрутка подписок): список активных Stars-подписок на донат в
    этом боте, с возможностью отменить подписку конкретного пользователя
    вручную (editUserStarSubscription) — например, если человек просит
    отменить, а сам не может/не хочет разбираться в настройках Telegram."""
    _, bot_id_s, page_s = c.data.split(":")
    bot_id, page = int(bot_id_s), int(page_s)
    cb, _ = await _access(bot_id, c.from_user.id)
    if not cb:
        await c.answer("Нет доступа", show_alert=True); return
    per_page = 10
    async with Session() as s:
        rows = (await s.scalars(select(Donation).where(
            Donation.bot_id == bot_id, Donation.is_subscription == True,  # noqa: E712
            Donation.subscription_state == "active",
        ).order_by(Donation.id.desc()))).all()
    chunk = rows[page * per_page:(page + 1) * per_page]
    if not rows:
        text = f"{em('info')} Активных подписок нет."
        rows_kb = [[("⬅️ Назад", f"stats:{bot_id}")]]
    else:
        text = f"{em('star')} <b>Активные подписки: {len(rows)}</b>"
        rows_kb = [[(f"❌ Отменить {d.user_id} ({d.stars}⭐️/мес)", f"subcancel:{bot_id}:{d.id}")]
                  for d in chunk]
        nav = []
        if page > 0:
            nav.append((f"⬅️ Стр. {page}", f"sublist:{bot_id}:{page-1}"))
        if (page + 1) * per_page < len(rows):
            nav.append((f"Стр. {page+2} ➡️", f"sublist:{bot_id}:{page+1}"))
        if nav:
            rows_kb.append(nav)
        rows_kb.append([("⬅️ Назад", f"stats:{bot_id}")])
    await c.message.edit_text(text, reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith("subcancel:"))
async def sub_cancel(c: CallbackQuery):
    _, bot_id_s, don_id_s = c.data.split(":")
    bot_id, don_id = int(bot_id_s), int(don_id_s)
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    async with Session() as s:
        don = await s.get(Donation, don_id)
        if not don or don.bot_id != bot_id or not don.telegram_payment_charge_id:
            await c.answer("Подписка не найдена", show_alert=True); return
        user_id, charge_id = don.user_id, don.telegram_payment_charge_id
    child_bot = manager.bots.get(bot_id)
    if not child_bot:
        await c.answer("Бот сейчас не запущен", show_alert=True); return
    try:
        await child_bot.edit_user_star_subscription(user_id, charge_id, is_canceled=True)
    except Exception as e:
        await c.answer(f"Не удалось отменить: {e}", show_alert=True)
        return
    async with Session() as s:
        obj = await s.get(Donation, don_id)
        obj.subscription_state = "canceled"
        await s.commit()
    await c.answer(f"{em('check')} Подписка отменена")
    await sub_list(c.model_copy(update={"data": f"sublist:{bot_id}:0"}))


@router.callback_query(F.data.startswith("blockedlist:"))
async def blocked_list(c: CallbackQuery):
    """БАГ (по запросу): люди, заблокировавшие бота, учитывались только в
    ОБЩЕМ числе на графике — конкретный список, кто именно, нигде не был
    виден. Теперь отдельная страница с ID/username каждого."""
    _, bot_id_s, page_s = c.data.split(":")
    bot_id, page = int(bot_id_s), int(page_s)
    cb, _ = await _access(bot_id, c.from_user.id)
    if not cb:
        await c.answer("Нет доступа", show_alert=True); return
    per_page = 15
    async with Session() as s:
        rows = (await s.scalars(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.is_blocked_bot.is_(True)
        ).order_by(BotUser.last_active.desc()))).all()
    chunk = rows[page * per_page:(page + 1) * per_page]
    if not rows:
        text = f"{em('check')} Никто не блокировал бота."
    else:
        lines = [f"• <code>{u.user_id}</code>" + (f" @{u.username}" if u.username else "")
                for u in chunk]
        text = (f"{em('no_entry')} <b>Заблокировали бота: {len(rows)}</b>\n\n"
               + "\n".join(lines))
    nav = []
    if page > 0:
        nav.append((f"⬅️ Стр. {page}", f"blockedlist:{bot_id}:{page-1}"))
    if (page + 1) * per_page < len(rows):
        nav.append((f"Стр. {page+2} ➡️", f"blockedlist:{bot_id}:{page+1}"))
    rows_kb = ([nav] if nav else []) + [[("⬅️ Назад", f"stats:{bot_id}")]]
    await c.message.edit_text(text, reply_markup=kb(rows_kb))
    await c.answer()


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


@router.callback_query(F.data.startswith("restartbot:"))
async def restartbot(c: CallbackQuery):
    """Полный перезапуск бота (новый Bot()/Dispatcher(), сброс webhook и
    т.п.) — раньше manager.restart_bot() существовал, но нигде не
    вызывался: единственным способом "перезапустить" было дважды нажать
    Вкл/выкл, и это не помогало при зависшем поллинге (см. фикс
    _stop_bot_locked в services/bot_manager.py).

    БАГ "конфликты при перезапуске выключенного бота": manager.restart_bot()
    сам по себе рестартует только УЖЕ включённого бота (is_active=True) —
    если бот выключен, старая версия просто тихо ничего не делала, и было
    неочевидно, что произошло (человек жал ещё раз, слал новые команды —
    как раз тут и рождались гонки/конфликты). Теперь "Перезапустить" ведёт
    себя однозначно: бот ВСЕГДА гарантированно включается и поднимает
    чистый поллинг, независимо от того, был он включён или выключен."""
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        return
    await c.answer(f"{em('refresh')} Перезапускаю…")
    if not cb.is_active:
        async with Session() as s:
            obj = await s.get(ChildBot, bot_id)
            obj.is_active = True
            await s.commit()
    await manager.restart_bot(bot_id)
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


# --- бан пользователя в САМОМ КОНСТРУКТОРЕ (не в дочерних ботах) ---
@router.message(Command("banconstructor"))
async def cmd_ban_constructor(m: Message, command: CommandObject):
    """/banconstructor 123456 Причина — банит человека в master-боте, он не
    сможет создавать/управлять ботами вообще. Только SUPER_ADMIN_ID."""
    if not _is_super(m.from_user.id):
        return
    args = (command.args or "").split(maxsplit=1)
    if not args or not args[0].isdigit():
        await m.answer(f"{em('warn')} Формат: <code>/banconstructor 123456 Причина</code>")
        return
    user_id = int(args[0])
    reason = args[1] if len(args) > 1 else "Не указана"
    text = await mod.ban_platform_user(user_id, reason)
    await m.answer(f"{em('no_entry')} " + text)


@router.message(Command("unbanconstructor"))
async def cmd_unban_constructor(m: Message, command: CommandObject):
    if not _is_super(m.from_user.id):
        return
    if not command.args or not command.args.split()[0].isdigit():
        await m.answer(f"{em('warn')} Формат: <code>/unbanconstructor 123456</code>")
        return
    user_id = int(command.args.split()[0])
    text = await mod.unban_platform_user(user_id)
    await m.answer(f"{em('check')} " + text)


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
            [("🌟 Активные Pro / выдать", "ap_pro_list:0")],
            [("🚫 Бан в конструкторе", "ap_ban:0")],
            [("⬅️ Назад", "main")],
        ]))
    await c.answer()


@router.callback_query(F.data.startswith("ap_ban:"))
async def ap_ban_menu(c: CallbackQuery):
    """Раньше бан в конструкторе был доступен ТОЛЬКО командами
    /banconstructor и /unbanconstructor без единой кнопки в интерфейсе —
    теперь полноценный раздел в админ-панели: список забаненных + кнопка
    забанить по ID."""
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    page = int(c.data.split(":")[1])
    per_page = 10
    async with Session() as s:
        banned = (await s.scalars(select(PlatformUser).where(
            PlatformUser.is_banned == True).order_by(PlatformUser.banned_at.desc()))).all()
    chunk = banned[page * per_page:(page + 1) * per_page]
    rows = [[(f"🚫 {u.id} — {(u.ban_reason or 'без причины')[:20]}", f"ap_unban:{u.id}")]
            for u in chunk]
    nav = []
    if page > 0:
        nav.append((f"⬅️ Стр. {page}", f"ap_ban:{page-1}"))
    if (page + 1) * per_page < len(banned):
        nav.append((f"Стр. {page+2} ➡️", f"ap_ban:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([("➕ Забанить по ID", "ap_ban_add")])
    rows.append([("⬅️ Назад", "ap")])
    text = (f"{em('no_entry')} <b>Бан в конструкторе</b>\n"
           f"Забанено: {len(banned)}. Нажмите на запись, чтобы разбанить.")
    await c.message.edit_text(text, reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data == "ap_ban_add")
async def ap_ban_add_start(c: CallbackQuery, state: FSMContext):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await state.set_state(St.ap_ban_add)
    msg = await c.message.edit_text(
        f"{em('info')} Пришлите: <code>ID Причина</code> (причина необязательна)",
        reply_markup=kb([[("⬅️ Назад", "ap_ban:0")]]))
    await state.update_data(last_msg_id=msg.message_id)
    await c.answer()


@router.message(St.ap_ban_add)
async def ap_ban_add_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    if not _is_super(m.from_user.id):
        return
    parts = (m.text or "").split(maxsplit=1)
    if not parts or not parts[0].lstrip("-").isdigit():
        msg = await m.answer(f"{em('warn')} Формат: <code>ID Причина</code>")
        await state.update_data(last_msg_id=msg.message_id)
        return
    user_id = int(parts[0])
    reason = parts[1] if len(parts) > 1 else "Не указана"
    await mod.ban_platform_user(user_id, reason)
    await state.clear()
    c_fake_data = "ap_ban:0"
    msg = await m.answer(f"{em('check')} Пользователь {user_id} забанен в конструкторе.",
                         reply_markup=kb([[("⬅️ К списку", c_fake_data)]]))


@router.callback_query(F.data.startswith("ap_unban:"))
async def ap_unban(c: CallbackQuery):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    user_id = int(c.data.split(":")[1])
    await mod.unban_platform_user(user_id)
    await c.answer(f"{em('check')} Разбанен")
    c_new = c.model_copy(update={"data": "ap_ban:0"})
    await ap_ban_menu(c_new)


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
    # БАГ (по запросу): рассылка из админ-панели раньше уходила АБСОЛЮТНО во
    # все активные боты, включая боты владельцев с Pro-подпиской — хотя Pro
    # как раз и продаётся как "без рекламы/рассылок в моих ботах" (см.
    # services/ads.py::bot_is_pro_protected — платная реклама это правило
    # уже соблюдает). Теперь при каждом запуске рассылки явно спрашиваем.
    await state.update_data(last_msg_id=c.message.message_id)
    await c.message.edit_text(
        f"{em('megaphone')} Включать в рассылку боты, чьи владельцы — "
        "Pro-подписчики? (обычно у Pro реклама/рассылки в ботах отключены)",
        reply_markup=kb([
            [("✅ Включая Pro-ботов", "ap_bc_pro:1")],
            [("🚫 Без Pro-ботов", "ap_bc_pro:0")],
            [("❌ Отмена", "ap")],
        ]))
    await c.answer()


@router.callback_query(F.data.startswith("ap_bc_pro:"))
async def ap_bc_pro_choice(c: CallbackQuery, state: FSMContext):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    include_pro = c.data.split(":")[1] == "1"
    await state.set_state(St.ap_bc_content)
    await state.update_data(include_pro=include_pro, last_msg_id=c.message.message_id)
    await c.message.edit_text(
        f"{em('megaphone')} Пришлите сообщение, которое будет разослано "
        f"пользователям {'ВСЕХ' if include_pro else 'ВСЕХ (кроме Pro-ботов)'} "
        "активных ботов платформы (текст/медиа/форматирование сохранятся):")
    await c.answer()


@router.message(St.ap_bc_content)
async def ap_bc_content(m: Message, state: FSMContext):
    if not _is_super(m.from_user.id):
        return
    data = await state.get_data()
    include_pro = data.get("include_pro", False)
    file_id, media_type = capture_media(m)
    await state.clear()

    media_bytes = media_filename = None
    if file_id:
        # Скачиваем ОДИН раз через master-бота — раньше сюда передавался
        # "чужой" file_id мастер-бота напрямую в каждый из дочерних ботов,
        # из-за чего ЛЮБАЯ отправка с медиа падала (см. _download_media_b64).
        media_b64, media_filename = await _download_media_b64(m.bot, file_id)
        if media_b64 is None:
            media_type = None
            await m.answer(f"{em('warn')} Не удалось скачать медиа для рассылки, "
                           "разошлю только текст (если он есть).")
        else:
            media_bytes = base64.b64decode(media_b64)

    async with Session() as s:
        all_bots = (await s.scalars(select(ChildBot).where(ChildBot.is_active))).all()
    bots = []
    skipped_pro = 0
    if include_pro:
        bots = all_bots
    else:
        for cb in all_bots:
            if await referrals.is_pro(cb.owner_id):
                skipped_pro += 1
            else:
                bots.append(cb)
    msg = await m.answer(f"{em('hourglass')} Рассылка запущена по {len(bots)} ботам"
                         f"{f' (пропущено Pro: {skipped_pro})' if skipped_pro else ''}...")
    html_text = m.html_text if (m.text or m.caption) else None
    total = sent = failed = 0
    for cb in bots:
        try:
            res = await run_broadcast(cb.token, cb.id, target="all",
                                      html_text=html_text, media_bytes=media_bytes,
                                      media_filename=media_filename,
                                      media_type=media_type)
            total += res["total"]; sent += res["sent"]; failed += res["failed"]
        except Exception:
            failed += 1
            log.exception("Рассылка по всем ботам: сбой на боте %s (@%s)",
                          cb.id, cb.username)
    await msg.edit_text(
        f"{em('check')} <b>Готово</b>\nБотов: {len(bots)}"
        f"{f' (пропущено Pro-ботов: {skipped_pro})' if skipped_pro else ''}\n"
        f"Получателей всего: {total}\n✅ Доставлено: {sent}\n❌ Ошибки: {failed}")


@router.callback_query(F.data == "ap_pro")
async def ap_pro_start(c: CallbackQuery, state: FSMContext):
    """По запросу: возможность выдать/продлить Pro-подписку пользователю
    прямо из админ-панели (без покупки/рефералки) — например, в качестве
    бонуса или компенсации."""
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await state.set_state(St.ap_pro_user)
    await state.update_data(last_msg_id=c.message.message_id)
    await c.message.edit_text(
        f"{em('sparkles')} Введите Telegram ID пользователя, которому нужно "
        "выдать/продлить Pro:",
        reply_markup=kb([[("❌ Отмена", "ap")]]))
    await c.answer()


@router.message(St.ap_pro_user)
async def ap_pro_user_save(m: Message, state: FSMContext):
    if not _is_super(m.from_user.id):
        return
    if not m.text or not m.text.strip().lstrip("-").isdigit():
        await m.answer(f"{em('warn')} Нужен числовой Telegram ID. Попробуйте ещё раз:")
        return
    user_id = int(m.text.strip())
    pu = await referrals.get_or_create(user_id)
    status = (f"Pro активен до {pu.pro_until.strftime('%d.%m.%Y %H:%M')}"
             if pu.pro_until and pu.pro_until > datetime.utcnow()
             else "Pro сейчас не активен")
    await state.update_data(pro_user_id=user_id)
    await state.set_state(St.ap_pro_days)
    await m.answer(f"Пользователь <code>{user_id}</code>: {status}\n\n"
                   "На сколько дней продлить Pro? Пришлите целое число "
                   "(отрицательное — чтобы, наоборот, сократить/отключить).")


@router.message(St.ap_pro_days)
async def ap_pro_days_save(m: Message, state: FSMContext):
    if not _is_super(m.from_user.id):
        return
    if not m.text or not m.text.strip().lstrip("-").isdigit():
        await m.answer(f"{em('warn')} Нужно целое число дней. Попробуйте ещё раз:")
        return
    days = int(m.text.strip())
    data = await state.get_data()
    user_id = data.get("pro_user_id")
    await state.clear()
    if not user_id:
        await m.answer(f"{em('warn')} Сессия истекла, начните заново: /start → "
                       "Админ-панель → Выдать/продлить Pro.")
        return
    await referrals.grant_pro_days(user_id, days)
    pu = await referrals.get_or_create(user_id)
    status = (f"Pro активен до {pu.pro_until.strftime('%d.%m.%Y %H:%M')}"
             if pu.pro_until and pu.pro_until > datetime.utcnow()
             else "Pro не активен")
    await m.answer(f"{em('check')} Готово. Пользователь <code>{user_id}</code>: {status}",
                   reply_markup=kb([[("⬅️ В админ-панель", "ap")]]))
    # уведомляем самого пользователя, если это возможно (не роняем, если он
    # заблокировал master-бота или ещё ни разу не запускал его)
    if days > 0:
        try:
            notifier = Bot(MASTER_BOT_TOKEN)
            try:
                # БАГ: тут не был указан parse_mode="HTML" — em('sparkles')
                # отдаёт сырой <tg-emoji ...> тег, и без parse_mode он
                # уходил пользователю ЛИТЕРАЛЬНО как текст тега, а не как
                # эмодзи. Остальные HTML-теги (<b>, <code> и т.п.) по той же
                # причине никогда бы не рендерились.
                await notifier.send_message(
                    user_id,
                    f"{em('sparkles')} Вам продлена Pro-подписка на {days} дн. "
                    f"({status}).", parse_mode="HTML")
            finally:
                await notifier.session.close()
        except Exception:
            pass


@router.callback_query(F.data.startswith("ap_pro_list:"))
async def ap_pro_list(c: CallbackQuery):
    """По запросу: просмотр всех сейчас активных Pro-подписок в админ-панели."""
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    page = int(c.data.split(":")[1])
    per_page = 10
    now = datetime.utcnow()
    async with Session() as s:
        users = (await s.scalars(
            select(PlatformUser)
            .where(PlatformUser.pro_until.is_not(None), PlatformUser.pro_until > now)
            .order_by(PlatformUser.pro_until.desc()))).all()
    chunk = users[page * per_page:(page + 1) * per_page]
    rows = [[(f"👤 {u.id} — до {u.pro_until.strftime('%d.%m.%Y %H:%M')}",
             f"ap_pro_edit:{u.id}")] for u in chunk]
    nav = []
    if page > 0:
        nav.append((f"⬅️ Стр. {page}", f"ap_pro_list:{page-1}"))
    if (page + 1) * per_page < len(users):
        nav.append((f"Стр. {page+2} ➡️", f"ap_pro_list:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([("➕ Выдать/продлить Pro", "ap_pro")])
    rows.append([("⬅️ Назад", "ap")])
    text = (f"{em('sparkles')} <b>Активные Pro-подписки</b>\n\n"
           f"Сейчас активна у {len(users)} пользователей."
           if users else f"{em('sparkles')} Сейчас ни у кого нет активной Pro-подписки.")
    await c.message.edit_text(text, reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("ap_pro_edit:"))
async def ap_pro_edit(c: CallbackQuery, state: FSMContext):
    """Быстрый переход к продлению/сокращению Pro прямо из списка активных —
    без повторного ввода ID."""
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    user_id = int(c.data.split(":")[1])
    pu = await referrals.get_or_create(user_id)
    status = (f"Pro активен до {pu.pro_until.strftime('%d.%m.%Y %H:%M')}"
             if pu.pro_until and pu.pro_until > datetime.utcnow()
             else "Pro сейчас не активен")
    await state.update_data(pro_user_id=user_id, last_msg_id=c.message.message_id)
    await state.set_state(St.ap_pro_days)
    await c.message.edit_text(
        f"Пользователь <code>{user_id}</code>: {status}\n\n"
        "На сколько дней продлить Pro? Пришлите целое число (отрицательное — "
        "чтобы сократить/отключить).")
    await c.answer()


# ================== модерация рекламы (/ads в мастер-боте) ==================
async def _notify_ad_buyer(ad: Advertisement, text: str, reply_markup=None):
    # /ads покупается прямо в master-боте — покупатель уже переписывается с ним.
    bot = Bot(MASTER_BOT_TOKEN)
    try:
        await bot.send_message(ad.buyer_id, text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        pass
    finally:
        await bot.session.close()


async def _append_to_message(c: CallbackQuery, suffix: str):
    """Добавляет строку к сообщению заявки.

    БАГ: для заявки С ФОТО c.message.text is None, и edit_text(None + ...)
    падал с TypeError — одобрить/отклонить такую заявку было невозможно.
    Для медиа-сообщений редактируем подпись.
    """
    try:
        if c.message.photo or c.message.video or c.message.animation or c.message.document:
            await c.message.edit_caption(caption=(c.message.caption or "") + suffix)
        else:
            await c.message.edit_text((c.message.text or "") + suffix)
    except Exception:
        pass


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
        await _append_to_message(c, "\n\n✅ Одобрено, ссылка на оплату отправлена.")
    except RuntimeError as e:
        # ЮKassa не настроена (нет ключей) — сообщаем админу прямо в чате
        await _append_to_message(c, f"\n\n⚠️ {e}")
    await c.answer()


@router.callback_query(F.data.startswith("ad_no:"))
async def ad_reject(c: CallbackQuery, state: FSMContext):
    if not _is_super(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    ad_id = int(c.data.split(":")[1])
    await state.set_state(St.ad_reject_reason)
    await state.update_data(ad_id=ad_id, last_msg_id=c.message.message_id)
    await _append_to_message(c, "\n\n✏️ Укажите причину отклонения (или отправьте «-»):")
    await c.answer()


@router.message(St.ad_reject_reason)
async def ad_reject_reason(m: Message, state: FSMContext):
    if not _is_super(m.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    reason = "" if not m.text or m.text.strip() == "-" else m.text.strip()
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
@router.message(Command("ads"))
async def ads_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(St.ads_pick_kind)
    rows = [[("🎯 Показы в конкретном боте", "adk:impr")],
           [("🌐 Показы ВО ВСЕХ ботах платформы", "adk:allbots")]]
    cd = await ads_service.cooldown_remaining(m.from_user.id)
    if cd:
        days = cd.days + (1 if cd.seconds else 0)
        rows.append([(f"📢 Рассылка (доступна через {days} дн.)", "adk:cd")])
    else:
        rows.append([(f"📢 Рассылка во все боты ({ads_service.BROADCAST_PRICE_RUB} ₽)", "adk:bcast")])
    rows.append([("🎯 Мои кампании", "adcab:0")])
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


@router.callback_query(St.ads_pick_kind, F.data == "adk:allbots")
async def ads_kind_allbots(c: CallbackQuery, state: FSMContext):
    # БАГ (по запросу): раньше показ рекламы можно было купить ТОЛЬКО в
    # одном конкретном боте — не было формата "показывать во всех ботах
    # платформы сразу" (это НЕ разовая рассылка, а обычный, длящийся показ
    # в /start каждого активного бота, пока не закончатся показы).
    await state.update_data(kind="impressions", target_bot_id=None)
    await state.set_state(St.ads_text)
    await c.message.edit_text(
        f"{em('pencil')} Пришлите ТЕКСТ объявления (до {AD_MAX_LEN} символов). "
        "Реклама только текстовая, без фото/видео — но форматирование "
        "(жирный, курсив, подчёркнутый, ссылки) поддерживается.")
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
        f"{em('pencil')} Пришлите ТЕКСТ объявления (до {AD_MAX_LEN} символов). "
        "Реклама только текстовая, без фото/видео — но форматирование "
        "(жирный, курсив, подчёркнутый, ссылки) поддерживается.")
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
        f"{em('pencil')} Пришлите ТЕКСТ объявления (до {AD_MAX_LEN} символов). "
        "Реклама только текстовая, без фото/видео — но форматирование "
        "(жирный, курсив, подчёркнутый, ссылки) поддерживается.")
    await c.answer()


@router.message(St.ads_text)
async def ads_text(m: Message, state: FSMContext):
    # БАГ (по запросу "убери медиа у рекламных постов"): раньше сюда же
    # принималось фото/видео/гифка — реклама теперь принципиально ТОЛЬКО
    # текстовая, любое вложение просто игнорируется (берём m.text/caption).
    #
    # По запросу: теперь сохраняем текст С ФОРМАТИРОВАНИЕМ (m.html_text —
    # жирный/курсив/подчёркивание/ссылки и т.п., как в самом сообщении),
    # а не голый m.text/caption как раньше. Лимит символов считаем по
    # ВИДИМОМУ тексту (без html-тегов) — иначе разметка несправедливо
    # съедала бы часть лимита.
    plain = (m.text or m.caption or "").strip()
    if not plain:
        await m.answer(f"{em('warn')} Нужен текст объявления (только текст, без вложений).")
        return
    if len(plain) > AD_MAX_LEN:
        await m.answer(f"{em('warn')} Слишком длинно ({len(plain)}/{AD_MAX_LEN}). "
                       "Сократите текст и пришлите ещё раз.")
        return
    text = (m.html_text or plain).strip()
    await state.update_data(text=text)
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
    n = int(m.text.strip())
    data = await state.get_data()
    ext_id = data.get("extend_ad_id")
    if ext_id:
        # Пришли сюда через "🔁 Продлить" в рекламном кабинете, а не через
        # обычную покупку — тут число показов докупается к существующей
        # кампании, а не создаёт новую.
        await state.clear()
        ad = await ads_service.extend_ad(ext_id, m.from_user.id, n)
        if not ad:
            await m.answer(f"{em('warn')} Не удалось продлить — кампания уже недоступна.")
            return
        await m.answer(f"{em('check')} Заявка на продление кампании №{ad.id} "
                       f"({n} показов, +{ads_service.price_for_impressions(n)} ₽) "
                       "отправлена на модерацию.")
        await _notify_super_admin(ad)
        return
    await _ads_confirm_impressions(m, state, n)


@router.callback_query(St.ads_confirm, F.data.startswith("adi:"))
async def ads_impr_preset(c: CallbackQuery, state: FSMContext):
    n = int(c.data.split(":")[1])
    await _ads_confirm_impressions(c.message, state, n, edit=True)
    await c.answer()


async def _ads_confirm_impressions(m: Message, state: FSMContext, n: int, edit=False):
    price = ads_service.price_for_impressions(n)
    await state.update_data(impressions=n, price=price)
    await state.set_state(St.ads_confirm)
    data = await state.get_data()
    scope = "ВО ВСЕХ ботах платформы" if data.get("target_bot_id") is None else "выбранного бота"
    text = (f"{em('info')} Объявление: {n} показов в стартовых сообщениях "
           f"{scope}.\nЦена: <b>{price} ₽</b>\n\nОтправить на модерацию?")
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
        ad = await ads_service.create_broadcast_ad(c.from_user.id, 0, data["text"])
        if not ad:
            await c.message.edit_text(f"{em('warn')} Кулдаун ещё не истёк, попробуйте позже.")
            await c.answer()
            return
    else:
        ad = await ads_service.create_impressions_ad(
            c.from_user.id, data.get("target_bot_id"), data["text"], data["impressions"])
        if not ad:
            await c.message.edit_text(f"{em('warn')} Владелец этого бота — Pro-подписчик, "
                                      "реклама в нём недоступна. Выберите другой бот через /ads.")
            await c.answer()
            return
    await c.message.edit_text(f"{em('check')} Заявка №{ad.id} отправлена суперадмину "
                              "на модерацию. Мы напишем, когда решение будет принято.")
    await c.answer()
    await _notify_super_admin(ad)


# =========================================================================
# ======================   Рекламный кабинет   ===========================
# =========================================================================
_AD_STATUS_LABEL = {
    "pending": "⏳ на модерации", "rejected": "❌ отклонено",
    "awaiting_payment": "💳 ждёт оплаты", "active": "🟢 активна",
    "finished": "🏁 показы закончились",
}


@router.callback_query(F.data.startswith("adcab:"))
async def ads_cabinet(c: CallbackQuery):
    """БАГ: кнопка "🎯 Мои кампании" в /ads вела на callback_data, для
    которого не было ни одного обработчика — нажатие просто ничего не
    делало ("not handled" в логах). Теперь показывает список кампаний
    покупателя, сколько потрачено всего, и позволяет продлить исчерпанную
    impressions-кампанию."""
    page = int(c.data.split(":")[1])
    ads = await ads_service.list_my_ads(c.from_user.id)
    spent = await ads_service.my_spend_total(c.from_user.id)
    per_page = 6
    chunk = ads[page * per_page:(page + 1) * per_page]
    if not ads:
        text = f"{em('info')} У вас пока нет рекламных кампаний. Создать — /ads."
        rows = [[("⬅️ Назад", "adcab_back")]]
    else:
        lines = [f"{em('coin')} Всего потрачено: <b>{spent} ₽</b>\n"]
        rows = []
        for ad in chunk:
            kind = "🌐 все боты" if (ad.kind == AdKind.impressions and ad.source_bot_id is None) \
                else ("📢 рассылка" if ad.kind == AdKind.broadcast else "🎯 конкретный бот")
            progress = f" ({ad.shown_count}/{ad.target_impressions})" if ad.kind == AdKind.impressions else ""
            lines.append(f"№{ad.id} · {kind}{progress} · {_AD_STATUS_LABEL.get(ad.status.value, ad.status.value)}\n"
                        f"«{_plain_preview(ad.text, 60)}» · {ad.price_rub} ₽")
            row = []
            if ad.kind == AdKind.impressions and ad.status == AdStatus.finished:
                row.append((f"🔁 Продлить №{ad.id}", f"adext:{ad.id}"))
            if row:
                rows.append(row)
        text = "\n\n".join(lines)
        nav = []
        if page > 0:
            nav.append((f"⬅️ Стр. {page}", f"adcab:{page-1}"))
        if (page + 1) * per_page < len(ads):
            nav.append((f"Стр. {page+2} ➡️", f"adcab:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([("⬅️ Назад", "adcab_back")])
    await c.message.edit_text(text, reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data == "adcab_back")
async def ads_cabinet_back(c: CallbackQuery, state: FSMContext):
    await ads_start(c.message, state)
    await c.answer()


@router.callback_query(F.data.startswith("adext:"))
async def ads_extend_start(c: CallbackQuery, state: FSMContext):
    ad_id = int(c.data.split(":")[1])
    ad = await ads_service.get_ad_for_owner(ad_id, c.from_user.id)
    if not ad or ad.status != AdStatus.finished:
        await c.answer("Эту кампанию сейчас нельзя продлить.", show_alert=True)
        return
    await state.set_state(St.ads_impr_custom)
    await state.update_data(extend_ad_id=ad_id)
    await c.message.edit_text(
        f"{em('pencil')} Сколько ещё показов докупить к кампании №{ad_id}? "
        "Введите целое число (минимум 1):")
    await c.answer()




# ================== автоответы ==================

async def _show_ar_list(target, bot_id: int, edit: bool):
    async with Session() as s:
        rules = (await s.scalars(
            select(AutoReply)
            .where(AutoReply.bot_id == bot_id)
            .order_by(AutoReply.position, AutoReply.id)
        )).all()
    lines = []
    for r in rules:
        status = "✅" if r.is_active else "⏸"
        if r.kind == AutoReplyKind.first_message:
            label = "На первое сообщение"
        elif r.kind == AutoReplyKind.every_n:
            label = f"Каждые {r.param} сообщ."
        else:
            label = f"Слово «{r.param}»"
        preview = (r.text or "")[:40].replace("\n", " ")
        lines.append(f"{status} <b>{label}</b>: {preview}")
    rows = []
    for r in rules:
        rows.append([
            (f"✏️ {r.id} вкл/выкл", f"ar_toggle:{r.id}:{bot_id}"),
            (f"🗑 Удалить", f"ar_del:{r.id}:{bot_id}"),
        ])
    rows.append([("➕ Добавить автоответ", f"ar_add:{bot_id}")])
    rows.append([("⬅️ Назад", f"cfg:{bot_id}")])
    text = ("💬 <b>Автоответы</b>\n\n" + "\n".join(lines)) if lines else            "💬 <b>Автоответы</b>\n\nПравил пока нет — добавьте первое."
    if edit:
        await target.edit_text(text, reply_markup=kb(rows))
    else:
        await target.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("ar_list:"))
async def ar_list(c: CallbackQuery):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    await _show_ar_list(c.message, bot_id, edit=True)
    await c.answer()


@router.callback_query(F.data.startswith("ar_add:"))
async def ar_add(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    await state.update_data(ar_bot_id=bot_id, last_msg_id=c.message.message_id)
    await c.message.edit_text(
        "💬 <b>Тип автоответа:</b>",
        reply_markup=kb([
            [("📨 На первое сообщение", "ar_kind:first_message")],
            [("🔢 Каждые N сообщений",  "ar_kind:every_n")],
            [("🔍 По ключевому слову",  "ar_kind:keyword")],
            [("⬅️ Отмена", f"ar_list:{bot_id}")],
        ]))
    await c.answer()


@router.callback_query(F.data.startswith("ar_kind:"))
async def ar_kind(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[1]
    await state.update_data(ar_kind=kind)
    if kind == "first_message":
        await state.update_data(ar_param=None)
        await state.set_state(St.ar_text)
        await c.message.edit_text(
            f"{em('pencil')} Введите текст автоответа (HTML-форматирование поддерживается):\n"
            "<i>Или пришлите сообщение с фото — текст станет подписью.</i>")
    elif kind == "every_n":
        await state.set_state(St.ar_param)
        await c.message.edit_text(f"{em('pencil')} Введите <b>число N</b> — автоответ сработает "
                                  "каждые N входящих сообщений от пользователя (целое ≥ 1):")
    else:
        await state.set_state(St.ar_param)
        await c.message.edit_text(f"{em('pencil')} Введите <b>ключевое слово или фразу</b> "
                                  "(регистр не важен — срабатывает при вхождении в текст):")
    await c.answer()


@router.message(St.ar_param)
async def ar_param_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    data = await state.get_data()
    kind = data.get("ar_kind")
    text = m.text.strip() if m.text else ""
    if kind == "every_n":
        if not text.isdigit() or int(text) < 1:
            msg = await m.answer(f"{em('cross')} Нужно целое число ≥ 1. Попробуйте снова:")
            await state.update_data(last_msg_id=msg.message_id)
            return
    elif not text:
        msg = await m.answer(f"{em('cross')} Нужно ввести слово или фразу. Попробуйте снова:")
        await state.update_data(last_msg_id=msg.message_id)
        return
    await state.update_data(ar_param=text)
    await state.set_state(St.ar_text)
    msg = await m.answer(
        f"{em('pencil')} Введите текст автоответа (HTML поддерживается):\n"
        "<i>Или пришлите сообщение с фото — текст станет подписью.</i>")
    await state.update_data(last_msg_id=msg.message_id)


@router.message(St.ar_text)
async def ar_text_save(m: Message, state: FSMContext):
    await delete_previous(m, state)
    # принимаем текст или фото с подписью
    text = m.html_text or m.html_caption or ""
    photo = m.photo[-1].file_id if m.photo else None
    if not text and not photo:
        msg = await m.answer(f"{em('cross')} Нужен текст или фото с подписью. Попробуйте снова:")
        await state.update_data(last_msg_id=msg.message_id)
        return
    # Перезалить фото через дочернего бота (file_id мастер-бота невалиден для дочернего)
    reuploaded_photo = None
    if photo:
        data_now = await state.get_data()
        reuploaded_photo = await manager_reupload(m.bot, data_now["ar_bot_id"], photo, m.from_user.id)
        if reuploaded_photo is None:
            await m.answer(f"{em('warn')} Не удалось прикрепить фото (напишите что-нибудь "
                           "дочернему боту и попробуйте снова) — сохраняю только текст.")
    await state.update_data(ar_text=text, ar_photo=reuploaded_photo)
    # Сохраняем правило
    data = await state.get_data()
    async with Session() as s:
        rule = AutoReply(
            bot_id=data["ar_bot_id"],
            kind=AutoReplyKind(data["ar_kind"]),
            param=data.get("ar_param"),
            text=data.get("ar_text", ""),
            photo=data.get("ar_photo"),
            is_active=True,
        )
        s.add(rule)
        await s.commit()
    await state.clear()
    bot_id = data["ar_bot_id"]
    await m.answer(f"{em('check')} Автоответ добавлен!",
                   reply_markup=kb([[("💬 К списку автоответов", f"ar_list:{bot_id}"),
                                     ("⚙️ Настройки", f"cfg:{bot_id}")]]))


@router.callback_query(F.data.startswith("ar_toggle:"))
async def ar_toggle(c: CallbackQuery):
    _, rule_id_s, bot_id_s = c.data.split(":")
    rule_id, bot_id = int(rule_id_s), int(bot_id_s)
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    async with Session() as s:
        rule = await s.get(AutoReply, rule_id)
        if not rule or rule.bot_id != bot_id:
            await c.answer("Не найдено", show_alert=True); return
        rule.is_active = not rule.is_active
        await s.commit()
    await c.answer("✅ Включено" if rule.is_active else "⏸ Выключено")
    await _show_ar_list(c.message, bot_id, edit=True)


@router.callback_query(F.data.startswith("ar_del:"))
async def ar_del(c: CallbackQuery):
    _, rule_id_s, bot_id_s = c.data.split(":")
    rule_id, bot_id = int(rule_id_s), int(bot_id_s)
    cb, is_owner = await _access(bot_id, c.from_user.id)
    if not cb or not is_owner:
        await c.answer("Только владелец", show_alert=True); return
    async with Session() as s:
        rule = await s.get(AutoReply, rule_id)
        if not rule or rule.bot_id != bot_id:
            await c.answer("Не найдено", show_alert=True); return
        await s.delete(rule)
        await s.commit()
    await c.answer(f"{em('trash')} Удалено")
    await _show_ar_list(c.message, bot_id, edit=True)

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
        # БАГ: медиа заявки с видео/гифкой терялись — суперадмин видел только
        # текст, а одобрить/отклонить фото-заявку было нельзя (см. _append_to_message).
        if ad.media_file_id and ad.media_type == "photo":
            await master.send_photo(SUPER_ADMIN_ID, ad.media_file_id, caption=text,
                                    parse_mode="HTML", reply_markup=markup)
        elif ad.media_file_id and ad.media_type == "video":
            await master.send_video(SUPER_ADMIN_ID, ad.media_file_id, caption=text,
                                    parse_mode="HTML", reply_markup=markup)
        elif ad.media_file_id and ad.media_type == "animation":
            await master.send_animation(SUPER_ADMIN_ID, ad.media_file_id, caption=text,
                                        parse_mode="HTML", reply_markup=markup)
        else:
            await master.send_message(SUPER_ADMIN_ID, text, parse_mode="HTML",
                                      reply_markup=markup)
    except Exception:
        pass
    finally:
        await master.session.close()
