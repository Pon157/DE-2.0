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
                           MessageReactionUpdated, ReactionTypeEmoji, ReplyParameters,
                           InputRichMessage, BotSubscriptionUpdated)
from sqlalchemy import select
from db.base import Session
from db.models import (ChildBot, BotAdmin, Donation, BotButton, OpenMode, ForwardMode,
                       BotUser, Ticket, MsgMap, MessageLog, BotType)
from services import moderation as mod
from services import ads as ads_service
from services import referrals
from utils.emoji import em, styled_button
from config import PLATFORM_BOT_USERNAME

log = logging.getLogger("child.common")


async def safe_call(coro_func, *args, retries: int = 2, **kwargs):
    """Вызывает Telegram-метод (aiogram coroutine factory) с автоповтором при
    flood control (TelegramRetryAfter).

    БАГ из прод-логов: при вспышке сообщений от подписчиков (несколько
    альбомов/сообщений подряд) Telegram отвечает "Flood control exceeded...
    Retry in N seconds" на send_message в чат админов. Раньше это исключение
    нигде не ловилось — апдейт падал необработанным (видно в логах пачками
    "is not handled"), сообщение молча терялось и админ его не видел вообще.
    Теперь ждём подсказанное Telegram время и повторяем (до `retries` раз).

    coro_func — вызываемое, возвращающее awaitable (например
    `lambda: bot.send_message(...)`), НЕ уже awaited-корутина — её нельзя
    было бы повторно await'нуть после первой попытки.
    """
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


# Команды, которые нельзя переопределить триггер-командой из конструктора.
RESERVED_COMMANDS = {"start", "restart", "cancel", "donate", "newpost", "done",
                     "ads", "ban", "unban", "warn", "unwarn", "ref", "pro"}


async def get_cfg(bot_db_id: int) -> ChildBot | None:
    async with Session() as s:
        return await s.get(ChildBot, bot_db_id)

async def is_bot_admin(bot_db_id: int, user_id: int, bot: Bot | None = None) -> bool:
    """БАГ (по запросу): раньше админом считался ТОЛЬКО владелец бота или
    запись в таблице BotAdmin (добавленная явно через конструктор). Если
    сотрудник просто состоит в группе admin_chat_id (обычная практика —
    его туда добавили руками в Telegram, а не через /admins в конструкторе),
    его личные сообщения боту распознавались как сообщения ОБЫЧНОГО
    пользователя — из-за этого им начинала показываться капча антиспама
    прямо в рабочих чатах. Теперь дополнительно проверяем фактическое
    членство в group/supergroup admin_chat_id через Bot API — если bot не
    передан (сохранена обратная совместимость старых вызовов), поведение
    как раньше."""
    async with Session() as s:
        cfg = await s.get(ChildBot, bot_db_id)
        if not cfg:
            return False
        if cfg.owner_id == user_id:
            return True
        if await s.scalar(select(BotAdmin).where(
                BotAdmin.bot_id == bot_db_id, BotAdmin.user_id == user_id)):
            return True
        admin_chat_id = cfg.admin_chat_id
    if bot is not None and admin_chat_id:
        try:
            member = await bot.get_chat_member(admin_chat_id, user_id)
            return member.status in ("administrator", "creator", "member")
        except Exception:
            return False
    return False


async def should_apply_antispam(bot_db_id: int, cfg: ChildBot, user_id: int,
                                bot: Bot | None = None) -> bool:
    """Раньше антиспам ВСЕГДА пропускал и админов, и владельца — из-за этого
    при проверке настроек владельцем казалось, что антиспам "не работает".
    Теперь: обычные админы по-прежнему не проверяются (иначе они не смогут
    модерировать во время наплыва сообщений), а владелец проверяется или нет
    в зависимости от `cfg.antispam_ignore_owner` (тоггл в настройках —
    удобно, чтобы протестировать антиспам на себе). `bot` передаётся, чтобы
    is_bot_admin() мог свериться с реальным составом admin_chat_id (см. её
    докстринг) — без этого сотрудники, добавленные в группу руками, ловили
    капчу в личных сообщениях боту."""
    if not cfg.antispam_enabled:
        return False
    is_admin = await is_bot_admin(bot_db_id, user_id, bot)
    if not is_admin:
        return True
    if cfg.owner_id == user_id and not cfg.antispam_ignore_owner:
        return True
    return False


# =========================================================================
# Медиа и альбомы — перенесено сюда из child/posting.py, т.к. теперь ЭТИМ ЖЕ
# пользуется и фидбек-бот (альбомы там раньше релеились по одному фото,
# каждое с ОТДЕЛЬНОЙ шапкой — "с фотками всё сложно").
# =========================================================================
ALBUM_DEBOUNCE = 0.8  # секунд ждём остальные части альбома, прежде чем обработать


def message_media(m: Message):
    """(file_id, media_type) для ВСЕХ типов медиа, которые умеем релеить и
    публиковать. Раньше voice/video_note/sticker молча терялись."""
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
    """Копит сообщения альбома (media_group_id) и обрабатывает всей пачкой
    через короткую паузу после последнего пришедшего сообщения группы."""
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


# =========================================================================
# Анон-id и шаблоны шапки/топика
# =========================================================================
def anon_id_for(bot_id: int, user_id: int) -> str:
    """Стабильный анонимный короткий id пользователя в рамках конкретного
    бота — админам есть на что ссылаться, не светя лишний раз настоящий id."""
    h = hashlib.md5(f"de:{bot_id}:{user_id}".encode()).hexdigest()[:8]
    return f"#{h}"


def _tpl_vars(bot_id: int, user_id: int, full_name: str | None, username: str | None) -> dict:
    return {
        "name": full_name or str(user_id),
        "username": username or "—",
        "id": user_id,
        "anon_id": anon_id_for(bot_id, user_id),
    }


def build_header(cfg: ChildBot, user, subject: str | None = None) -> str:
    """Шапка сообщения в админ-чате по шаблону владельца. Переменные:
    {name}, {username}, {id}, {anon_id}. При любой ошибке в шаблоне —
    безопасный дефолт (раньше битый шаблон ронял весь релей с исключением).

    subject — тема обращения (см. п.3, несколько параллельных обращений по
    разным темам через inline_ticket/keyboard_ticket кнопки). Если задана —
    добавляется префиксом, чтобы админы сразу видели, к какой теме
    относится сообщение (особенно важно вне режима топиков, где все
    обращения идут одной лентой в один чат)."""
    try:
        header = cfg.copy_header.format(**_tpl_vars(cfg.id, user.id, user.full_name, user.username))
    except Exception:
        header = (f"{user.full_name} | @{user.username or '—'} | <code>{user.id}</code> "
                 f"· {anon_id_for(cfg.id, user.id)}")
    if subject:
        header = f"🏷 {subject}\n{header}"
    return header


_TAG_RE = re.compile(r"<[^>]+>")


def build_topic_name(cfg: ChildBot, user_id: int,
                     full_name: str | None, username: str | None,
                     subject: str | None = None) -> str:
    """Имя форум-топика по шаблону владельца ({name}/{username}/{id}/{anon_id}
    — можно всё вместе или по одной). HTML-теги вырезаются — имя топика в
    Telegram всегда чистый текст. subject (тема обращения, см. п.3) —
    добавляется префиксом к имени топика, чтобы различать несколько
    параллельно открытых топиков одного пользователя по разным темам."""
    tpl = cfg.topic_name_template or "✉️ {name} · {id}"
    try:
        name = tpl.format(**_tpl_vars(cfg.id, user_id, full_name, username))
    except Exception:
        name = f"✉️ {full_name or user_id} · {user_id}"
    name = _TAG_RE.sub("", name).strip()
    name = name or f"✉️ {user_id}"
    if subject:
        name = f"{subject} · {name}"
    return name[:120]


async def inject_ad(bot_db_id: int, text: str) -> str:
    """Добавляет активную оплаченную рекламу в конец стартового сообщения и
    засчитывает показ. Реклама показывается ТОЛЬКО в том боте, в котором она
    куплена (см. services/ads.py::get_active_ad_for_display). Автоматически
    отключена, если владелец бота — Pro-подписчик."""
    ad = await ads_service.get_active_ad_for_display(bot_db_id)
    if not ad:
        return text
    await ads_service.register_impression(ad.id)
    return text + f"\n\n— — — — —\n{em('megaphone')} <i>{ad.text}</i>"


async def inject_footer(bot_db_id: int, text: str) -> str:
    """Приписка 'создано на платформе @Dialogue_Enginebot' — не показывается
    у Pro-владельцев (часть привилегий подписки)."""
    async with Session() as s:
        cfg = await s.get(ChildBot, bot_db_id)
    if not cfg:
        return text
    if await referrals.is_pro(cfg.owner_id):
        return text
    return text + f"\n\n<i>🤖 создано на платформе @{PLATFORM_BOT_USERNAME}</i>"


async def inject_extras(bot_db_id: int, text: str) -> str:
    """Реклама + приписка — единая точка вызова для приветственных сообщений."""
    # strip: если приветствие — только фото без текста, без этого приписки
    # начинались с пустых строк.
    text = (text or "").strip()
    text = await inject_ad(bot_db_id, text)
    text = await inject_footer(bot_db_id, text)
    return text


# =========================================================================
# Кнопки (inline-ссылки/триггеры, reply-клавиатура) — ОБЩИЙ билдер для
# фидбек- и постинг-ботов.
# =========================================================================
async def build_keyboards(bot_db_id: int, cfg: ChildBot, extra_inline: list | None = None,
                          with_close_ticket: bool = False):
    """Возвращает (InlineKeyboardMarkup|None, ReplyKeyboardMarkup|None).
    with_close_ticket=True добавляет в reply-клавиатуру кнопку
    "❌ Закрыть обращение" (см. open_ticket) — вместе с кнопкой доната, если
    она тоже reply-типа, т.к. Telegram позволяет только ОДНУ активную
    reply-клавиатуру в чате: раньше при появлении кнопки закрытия она бы
    молча заменила собой кнопку доната (или наоборот) — здесь строятся
    вместе, в одном вызове."""
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
            kb_rows.append([KeyboardButton(text=b.text, style=b.style, icon_custom_emoji_id=b.icon_emoji_id)])
        elif b.kind == "inline_ticket":
            inline_rows.append([InlineKeyboardButton(
                text=b.text, callback_data=f"open_ticket_subj:{b.id}",
                style=b.style, icon_custom_emoji_id=b.icon_emoji_id)])
        elif b.kind == "keyboard_ticket":
            kb_rows.append([KeyboardButton(text=b.text, style=b.style, icon_custom_emoji_id=b.icon_emoji_id)])
        elif b.kind == "inline_survey":
            inline_rows.append([InlineKeyboardButton(
                text=b.text, callback_data=f"survey_start:{b.id}",
                style=b.style, icon_custom_emoji_id=b.icon_emoji_id)])
        elif b.kind == "keyboard_survey":
            kb_rows.append([KeyboardButton(text=b.text, style=b.style, icon_custom_emoji_id=b.icon_emoji_id)])
        elif b.kind == "disown":
            # НОВОЕ (п.5): просто reply-кнопка с эффектом (см.
            # handle_keyboard_button) — рендерится как обычная кейборд-кнопка,
            # но, как и остальные, теперь поддерживает цвет/premium-эмодзи
            # (Bot API 9.4).
            kb_rows.append([KeyboardButton(text=b.text, style=b.style, icon_custom_emoji_id=b.icon_emoji_id)])
    if extra_inline:
        inline_rows = extra_inline + inline_rows
    if getattr(cfg, "open_mode", None) == OpenMode.button:
        inline_rows.insert(0, [InlineKeyboardButton(
            text=cfg.ticket_button_text, callback_data="open_ticket",
            style=cfg.ticket_button_style, icon_custom_emoji_id=cfg.ticket_button_icon)])
    if cfg.donate_enabled:
        if cfg.donate_button_type == "inline":
            inline_rows.append([InlineKeyboardButton(
                text=cfg.donate_button_text, callback_data="donate_btn",
                style=cfg.donate_button_style, icon_custom_emoji_id=cfg.donate_button_icon)])
        else:
            kb_rows.append([KeyboardButton(
                text=cfg.donate_button_text, style=cfg.donate_button_style,
                icon_custom_emoji_id=cfg.donate_button_icon)])
    if with_close_ticket and cfg.close_ticket_button_text:
        # НОВОЕ (по запросу): пустой/NULL текст = кнопка убрана владельцем.
        kb_rows.append([KeyboardButton(
            text=cfg.close_ticket_button_text, style=cfg.close_ticket_button_style,
            icon_custom_emoji_id=cfg.close_ticket_button_icon)])
    ikb = InlineKeyboardMarkup(inline_keyboard=inline_rows) if inline_rows else None
    rkb = ReplyKeyboardMarkup(keyboard=kb_rows, resize_keyboard=True) if kb_rows else None
    return ikb, rkb


async def welcome_pro_kwargs(cfg: ChildBot) -> dict:
    """Эффект приветствия — Pro-функция. Рич-текст теперь НЕ управляется
    тумблером: если владелец ставит приветствие через send_rich_message —
    оно уходит рич-форматом автоматически (гейт по is_pro на уровне
    send_with_keyboards). Тут возвращаем только effect_id."""
    if not await referrals.is_pro(cfg.owner_id):
        return {}
    return {"effect_id": cfg.welcome_effect_id}


async def send_with_keyboards(m: Message, text: str, ikb, rkb, photo: str | None = None,
                              effect_id: str | None = None, rich: bool = False):
    """Отправляет сообщение с клавиатурами.

    Telegram не позволяет прикрепить одновременно inline и reply клавиатуру к
    одному сообщению — если нужны оба вида, отправляем инлайн с текстом/фото,
    а следом отдельным сообщением выставляем reply-клавиатуру. У caption к
    фото лимит 1024 символа — если текст не влезает, шлём фото и текст
    отдельными сообщениями.

    effect_id — message_effect_id (Bot API) для анимации на приветственном
    сообщении, работает только в личных чатах — ровно наш случай. Если
    Telegram его не примет (например, сообщение с фото — эффект
    поддерживается не для всех типов вложений) — тихо шлём без эффекта, не
    роняя приветствие целиком.

    rich — УДАЛЕНО как явный параметр: рич-текст теперь включается
    автоматически если владелец — Pro и нет фото (Bot API не поддерживает
    рич+фото). Передаётся как True только при явном вызове из кода, где
    уже проверен Pro. Комбинация рич-сообщения с фото/эффектом Bot API
    не поддерживает — рич-режим тихо игнорируется в этом случае.

    БАГ из прод-логов ("wrong file identifier/HTTP URL specified"):
    file_id в Telegram привязан к конкретному боту, которым он был получен.
    welcome_photo сохраняется в конструкторе через МАСТЕР-бота (владелец
    присылает фото ЕМУ), а отправляет его уже ДОЧЕРНИЙ бот — с точки зрения
    Telegram это два разных бота, и file_id одного для другого невалиден.
    Итог — ЛЮБОЕ приветствие с фото падало с необработанным исключением при
    первом же /start. Раз конвертировать file_id между ботами без лишнего
    запроса к пользователю нельзя, здесь — защита: если фото не отправилось,
    не роняем весь апдейт, а тихо откатываемся на отправку текста.
    """
    PHOTO_CAPTION_LIMIT = 1024
    # БАГ: если текст приветствия пуст (например, владелец прислал фото без
    # подписи) И отправка фото падает (невалидный для этого бота file_id —
    # см. комментарий выше), старый код делал `if text else None` — при
    # пустом text это давало None и ЮЗЕР НЕ ПОЛУЧАЛ ВООБЩЕ НИЧЕГО на /start,
    # без единой ошибки в логах. Теперь всегда гарантируем хоть какое-то
    # сообщение — при пустом тексте подставляем нейтральный плейсхолдер.
    safe_text = text if text and text.strip() else f"{em('wave')} Привет!"
    fx = {"message_effect_id": effect_id} if effect_id else {}
    if rich and not photo:
        try:
            msg = await m.bot.send_rich_message(
                m.chat.id, InputRichMessage(html=safe_text),
                reply_markup=ikb or rkb, **fx)
            if ikb and rkb:
                await m.answer(f"{em('gear')} Меню", reply_markup=rkb)
            return msg
        except Exception as e:
            log.warning("send_with_keyboards: рич-сообщение не отправилось (%s), "
                        "шлю обычным текстом", e)
    if photo:
        try:
            if len(safe_text) <= PHOTO_CAPTION_LIMIT:
                msg = await m.answer_photo(photo, caption=safe_text, reply_markup=ikb or rkb, **fx)
            else:
                await m.answer_photo(photo)
                msg = await m.answer(safe_text, reply_markup=ikb or rkb, **fx)
        except TelegramBadRequest as e:
            log.warning("send_with_keyboards: не смог отправить фото (%s), шлю только текст", e)
            try:
                msg = await m.answer(safe_text, reply_markup=ikb or rkb, **fx)
            except TelegramBadRequest:
                msg = await m.answer(safe_text, reply_markup=ikb or rkb)
    else:
        try:
            msg = await m.answer(safe_text, reply_markup=ikb or rkb, **fx)
        except TelegramBadRequest:
            msg = await m.answer(safe_text, reply_markup=ikb or rkb)
    if ikb and rkb:
        await m.answer(f"{em('gear')} Меню", reply_markup=rkb)
    return msg


async def send_rich_or_plain(m: Message, text: str, reply_markup=None) -> Message | None:
    """Общий помощник: пробует send_rich_message, при любой ошибке (или если
    просто недоступно) — тихий откат на обычный m.answer с тем же текстом.
    Вынесено отдельно, чтобы рич-режим одинаково подключался на всех
    экранах бота, а не только в приветствии."""
    try:
        return await m.bot.send_rich_message(
            m.chat.id, InputRichMessage(html=text), reply_markup=reply_markup)
    except Exception as e:
        log.warning("send_rich_or_plain: рич-сообщение не отправилось (%s), "
                    "шлю обычным текстом", e)
        return None


async def rich_enabled(bot_db_id: int | None, cfg: ChildBot | None = None) -> bool:
    """Доступен ли рич-текст для этого бота — только Pro-функция.
    Тумблер rich_welcome убран: рич включается автоматически для Pro-ботов,
    не требует ручного переключения. Здесь только проверяем is_pro."""
    if cfg is None:
        if bot_db_id is None:
            return False
        cfg = await get_cfg(bot_db_id)
    if not cfg:
        return False
    return await referrals.is_pro(cfg.owner_id)


async def send_response(m: Message, text: str | None, photo: str | None = None,
                        bot_db_id: int | None = None):
    """Ответ на триггер/команду/кейборд-кнопку.

    БАГ: если владелец ставил на триггер ответ-фото БЕЗ текста, хендлеры
    проверяли `if b.response_text:` и молча ничего не отправляли. Плюс лимит
    caption 1024 — длинный текст с фото раньше ронял отправку.

    Фото триггера тоже настраивается через МАСТЕР-бота и отправляется потом
    ДОЧЕРНИМ — тот же класс бага, что и с welcome_photo (см.
    send_with_keyboards): file_id может быть невалиден для этого бота.
    Не роняем обработку — при ошибке отправки фото откатываемся на текст.

    bot_db_id — НОВОЕ: если передан и у бота включён рич-текст (Pro),
    ответ триггера тоже уходит через send_rich_message вместо обычного
    текста (расширение рич-текста "на все экраны" по запросу). Комбинация
    с фото рич-сообщения не поддерживает — в этом случае рич-режим
    игнорируется для конкретного ответа, летит обычная отправка.
    """
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
        if bot_db_id is not None and await rich_enabled(bot_db_id):
            if await send_rich_or_plain(m, text) is not None:
                return
        await m.answer(text)


async def handle_keyboard_button(m: Message, bot_db_id: int) -> bool:
    """Если m.text совпадает с текстом reply-кнопки — отвечает и возвращает
    True (значит, апдейт обработан и дальше по цепочке идти не нужно)."""
    if not m.text:
        return False
    async with Session() as s:
        b = await s.scalar(select(BotButton).where(
            BotButton.bot_id == bot_db_id,
            BotButton.kind.in_(("keyboard", "keyboard_ticket", "disown")),
            BotButton.text == m.text))
    if b and b.kind == "disown":
        # НОВОЕ (по запросу, п.5): чистый эффект — просто сообщение
        # пользователю, ничего в тикете/БД не меняется, обращение НЕ
        # закрывается.
        await m.answer(b.response_text or f"{em('warn')} От вас отказались.")
        return True
    if b and b.kind == "keyboard_ticket":
        # Открывает обращение по теме = тексту кнопки (см. п.3). Правило
        # "1 пользователь = 1 открытое обращение" строгое — если у юзера
        # уже открыто обращение (по любой теме), новое не создаём.
        cfg = await get_cfg(bot_db_id)
        if cfg:
            ticket, created, conflict = await open_ticket(m.bot, cfg, m.from_user.id, subject=b.text)
            if conflict:
                # НОВОЕ (по запросу): кнопку закрытия можно было отключить —
                # если её нет, не упоминаем её в подсказке.
                close_hint = (f" или кнопкой «{cfg.close_ticket_button_text}»"
                             if cfg.close_ticket_button_text else "")
                await m.answer(
                    f"{em('warn')} У вас уже есть открытое обращение"
                    f"{f' («{ticket.subject}»)' if ticket.subject else ''}. "
                    f"Закройте его командой /close{close_hint}, "
                    "чтобы открыть новое.")
            else:
                await send_response(m, b.response_text, b.response_photo, bot_db_id=bot_db_id)
        return True
    if b and (b.response_text is not None or b.response_photo):
        await send_response(m, b.response_text, b.response_photo, bot_db_id=bot_db_id)
        return True
    return False


async def _notify_user(bot: Bot, bot_db_id: int, user_id: int, text: str):
    """Best-effort ЛС пользователю. БАГ (статистика "заблокировали бота"):
    раньше любая ошибка (включая TelegramForbiddenError — юзер заблокировал
    бота) просто молча проглатывалась и НИГДЕ не фиксировалась — флаг
    is_blocked_bot выставляла только массовая рассылка. Теперь при отказе
    именно "заблокировал бота" статус сохраняется сразу, и пользователь
    корректно появляется в статистике/списке заблокировавших."""
    try:
        await bot.send_message(user_id, text)
    except TelegramForbiddenError:
        await mod.mark_blocked_bot(bot_db_id, user_id)
    except Exception:
        pass


async def open_ticket(bot: Bot, cfg: ChildBot, user_id: int,
                      force_new: bool = False,
                      subject: str | None = None) -> tuple[Ticket, bool, bool]:
    """Открывает (или переиспользует) тикет-переписку с пользователем —
    возвращает (ticket, created, conflict).
    created=True, если тикет только что создан (по этому флагу на первое
    сообщение вешается кнопка закрытия обращения).
    conflict=True — тикет НЕ создан и НЕ переиспользован для этой темы,
    потому что у пользователя уже открыто обращение по ДРУГОЙ теме (см.
    ниже) — вызывающий код должен показать пользователю, что нужно сначала
    закрыть текущее обращение.

    БАГ: если чат админов НЕ форум, а топики включены, create_forum_topic
    падал с исключением и сообщение пользователя терялось ВООБЩЕ (не
    релеилось). Теперь — мягкий фолбэк на режим без топика.

    ВАЖНО (по запросу): правило "1 пользователь = 1 открытое обращение"
    строгое — даже с несколькими тематическими кнопками
    (inline_ticket/keyboard_ticket, см. п.3) одновременно у пользователя
    может быть открыто только ОДНО обращение. subject — это просто метка
    темы для уже открытого/создаваемого обращения, а не способ завести
    параллельно несколько штук. Если пользователь нажимает кнопку темы, а
    у него уже открыто обращение (по любой теме или без темы) — новое НЕ
    создаётся, возвращается conflict=True, и пользователю нужно сначала
    закрыть текущее через /close или кнопку «Закрыть обращение»."""
    async with Session() as s:
        t = await s.scalar(select(Ticket).where(
            Ticket.bot_id == cfg.id, Ticket.user_id == user_id, Ticket.is_open))
        if t and not force_new:
            if subject is not None and t.subject != subject:
                # у юзера уже открыто обращение по ДРУГОЙ теме (или без темы) —
                # не открываем второе одновременно, сигналим конфликт
                return t, False, True
            t.last_active_at = datetime.utcnow()
            await s.commit()
            return t, False, False
        if t and force_new:
            if not getattr(cfg, "always_new_ticket", False):
                t.last_active_at = datetime.utcnow()
                await s.commit()
                return t, False, False
            t.is_open = False
            await s.commit()
        topic_id = None
        if cfg.use_topics and cfg.admin_chat_id:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == cfg.id, BotUser.user_id == user_id))
            topic_name = build_topic_name(
                cfg, user_id,
                full_name=(u.full_name if u else None),
                username=(u.username if u else None),
                subject=subject)
            try:
                # БАГ: передача icon_custom_emoji_id в create_forum_topic
                # вызывала ошибку "TOPIC_ICON_INVALID" для некоторых эмодзи —
                # при этом исключение ловилось и topic_id оставался None,
                # из-за чего message_thread_id не передавался и сообщения
                # уходили в General вместо топика (без ошибок в логах).
                # Исправление: сначала создаём топик БЕЗ иконки, потом
                # пробуем задать иконку отдельно (edit_forum_topic) — если
                # иконка не поддерживается, топик всё равно создан и будет
                # работать корректно.
                topic = await bot.create_forum_topic(cfg.admin_chat_id, topic_name)
                topic_id = topic.message_thread_id
                # Иконку ставим отдельно — ошибка тут не ломает топик
                if await referrals.is_pro(cfg.owner_id) and cfg.topic_icon_emoji_id:
                    try:
                        await bot.edit_forum_topic(
                            cfg.admin_chat_id, topic_id,
                            icon_custom_emoji_id=cfg.topic_icon_emoji_id)
                    except Exception as icon_err:
                        log.warning("Bot %s: не удалось задать иконку топика (%s) — "
                                    "топик создан без иконки", cfg.id, icon_err)
            except Exception as e:
                log.warning("Bot %s: create_forum_topic failed (%s) — "
                            "продолжаю без топика", cfg.id, e)
        t = Ticket(bot_id=cfg.id, user_id=user_id, topic_id=topic_id, subject=subject)
        s.add(t)
        await s.commit()
    await _send_ticket_opened_keyboard(bot, cfg, user_id)
    return t, True, False


async def _send_ticket_opened_keyboard(bot: Bot, cfg: ChildBot, user_id: int):
    """БАГ: у пользователя не было НИКАКОГО способа самому закрыть
    обращение — только админ мог закрыть его инлайн-кнопкой у себя. Здесь —
    reply-клавиатура с кнопкой закрытия, отправляется пользователю сразу
    при открытии тикета (в т.ч. когда это происходит "по факту" — например,
    подписчик просто прислал предложку в постинг-боте, ему тоже нужно и
    подтверждение, что сообщение дошло, и способ закрыть переписку)."""
    ikb, rkb = await build_keyboards(cfg.id, cfg, with_close_ticket=True)
    if rkb:
        try:
            await bot.send_message(user_id, f"{em('check')} Обращение открыто — "
                                   "можно продолжать писать сюда.", reply_markup=rkb)
        except Exception:
            pass


# =========================================================================
# Релей сообщений пользователя в админ-чат (ЕДИНЫЙ для фидбек- и
# постинг-ботов): forward/copy, шапка off/separate/merge, топики, альбомы,
# reply-контекст, маппинг для ответов/реакций, кнопка закрытия обращения.
# =========================================================================
async def _map_msg(bot_db_id: int, admin_msg_id: int, user_id: int,
                   user_msg_id: int | None = None, ticket_id: int | None = None):
    async with Session() as s:
        s.add(MsgMap(bot_id=bot_db_id, admin_chat_msg_id=admin_msg_id,
                     user_id=user_id, user_chat_msg_id=user_msg_id,
                     ticket_id=ticket_id))
        await s.commit()


def _combine_kb(kb1, kb2):
    if kb1 and kb2:
        return InlineKeyboardMarkup(inline_keyboard=kb1.inline_keyboard + kb2.inline_keyboard)
    return kb1 or kb2


async def _maybe_pin_first_message(bot: Bot, cfg: ChildBot, created: bool,
                                   chat_id: int, msg_id: int):
    """Настройка (по запросу): закреплять ли первое сообщение пользователя в
    админ-чате, когда бот НЕ по топикам (в топиках открытие обращения и так
    прекрасно видно — свой топик, закреплять там нечего). Работает только
    для только что созданного обращения (created=True) — чтобы НЕ дёргать
    pin на каждое последующее сообщение в этой же переписке."""
    if not created or cfg.use_topics or not getattr(cfg, "pin_first_message", False):
        return
    try:
        await bot.pin_chat_message(chat_id, msg_id, disable_notification=True)
    except Exception:
        pass


async def relay_to_admin_chat(msgs: list[Message], bot: Bot, cfg: ChildBot,
                              extra_kb: InlineKeyboardMarkup | None = None):
    """Пересылает сообщения пользователя в админ-чат.

    extra_kb — доп. кнопки (например "Принять/Отклонить" предложки). Кнопка
    "🔒 Закрыть обращение" вешается на первое сообщение нового тикета.
    """
    user = msgs[0].from_user
    ticket, created, _conflict = await open_ticket(bot, cfg, user.id)
    thread = ticket.topic_id if cfg.use_topics else None
    header = build_header(cfg, user, subject=ticket.subject)
    header_mode = getattr(cfg, "header_mode", "separate") or "separate"
    is_album = len(msgs) > 1
    first = msgs[0]

    # reply-контекст: если юзер ответил на сообщение, копия которого уже есть
    # в админ-чате, — прикрепляем нашу копию ответом на ту же копию, чтобы
    # админы ВИДЕЛИ, на что именно отвечает пользователь.
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
        # БАГ (по запросу): раньше кнопка "Закрыть обращение" вешалась
        # ТОЛЬКО на первое сообщение нового тикета — на всех последующих
        # сообщениях пользователя её не было вообще, приходилось скроллить
        # вверх. В режиме copy у каждой копии есть свой reply_markup —
        # вешаем кнопку на КАЖДОЕ сообщение. В режиме forward это
        # невозможно (forwardMessage не поддерживает reply_markup вообще) —
        # там вместо кнопки работает команда /close (реплаем или в топике).
        close_kb = InlineKeyboardMarkup(inline_keyboard=[[
            styled_button("🔒 Закрыть обращение", callback_data=f"close_ticket:{ticket.id}")]])

    # --- режим "шапка слитно с сообщением" (только copy + одиночное сообщение)
    if header_mode == "merge" and cfg.forward_mode == ForwardMode.copy and not is_album:
        markup = _combine_kb(extra_kb, close_kb)
        if first.text:
            merged = f"{header}\n\n{first.html_text}"
            if len(merged) <= 4096:
                sent = await safe_call(bot.send_message, cfg.admin_chat_id, merged,
                                       message_thread_id=thread,
                                       reply_markup=markup,
                                       reply_parameters=reply_params)
                await _map_msg(cfg.id, sent.message_id, user.id, first.message_id, ticket_id=ticket.id)
                await _maybe_pin_first_message(bot, cfg, created, cfg.admin_chat_id, sent.message_id)
                return
        else:
            fid, mtype = message_media(first)
            if fid and mtype in ("photo", "video", "animation", "document", "audio"):
                cap = first.html_text or ""
                merged = f"{header}\n\n{cap}" if cap else header
                if len(merged) <= 1024:
                    # copyMessage умеет заменять caption у медиа — шапка
                    # становится частью подписи, одно сообщение вместо двух.
                    sent = await safe_call(
                        bot.copy_message, cfg.admin_chat_id, first.chat.id, first.message_id,
                        message_thread_id=thread, caption=merged,
                        reply_markup=markup, reply_parameters=reply_params)
                    await _map_msg(cfg.id, sent.message_id, user.id, first.message_id, ticket_id=ticket.id)
                    await _maybe_pin_first_message(bot, cfg, created, cfg.admin_chat_id, sent.message_id)
                    return
        # не влезло/неподходящий тип — проваливаемся в режим отдельной шапки

    # --- отдельная шапка
    if header_mode != "off":
        hm = await safe_call(bot.send_message, cfg.admin_chat_id, header,
                             message_thread_id=thread)
        await _map_msg(cfg.id, hm.message_id, user.id, None, ticket_id=ticket.id)
        await _maybe_pin_first_message(bot, cfg, created, cfg.admin_chat_id, hm.message_id)
        created = False  # шапка уже закреплена (если нужно) — ниже больше не закрепляем
        # close_kb НЕ обнуляем — теперь кнопка вешается на КАЖДОЕ сообщение
        # пользователя (см. комментарий выше), а не только на шапку.

    if is_album:
        ids = [mm.message_id for mm in msgs]
        markup = _combine_kb(extra_kb, close_kb)
        if cfg.forward_mode == ForwardMode.forward:
            copies = await safe_call(bot.forward_messages, cfg.admin_chat_id, first.chat.id, ids,
                                     message_thread_id=thread)
            for mm, cp in zip(msgs, copies):
                await _map_msg(cfg.id, cp.message_id, user.id, mm.message_id, ticket_id=ticket.id)
            await _maybe_pin_first_message(bot, cfg, created, cfg.admin_chat_id, copies[0].message_id)
            if markup:
                # forwardMessages не поддерживает reply_markup вообще —
                # ограничение Bot API, тут отдельное сообщение неизбежно.
                sm = await safe_call(bot.send_message, cfg.admin_chat_id, "👆 Кнопки к посту выше",
                                     message_thread_id=thread, reply_markup=markup)
                await _map_msg(cfg.id, sm.message_id, user.id, None, ticket_id=ticket.id)
        elif markup:
            # БАГ (по запросу — "кнопки отдельным сообщением"): copy_messages
            # (батч) не поддерживает reply_markup ни на одном элементе — но
            # обычный copy_message ПО ОДНОМУ поддерживает. Копируем элементы
            # альбома по очереди и вешаем кнопки прямо на последний, без
            # отдельного служебного сообщения.
            copies = []
            for i, mm in enumerate(msgs):
                is_last = i == len(msgs) - 1
                cp = await safe_call(bot.copy_message, cfg.admin_chat_id, first.chat.id,
                                     mm.message_id, message_thread_id=thread,
                                     reply_markup=markup if is_last else None)
                copies.append(cp)
            for mm, cp in zip(msgs, copies):
                await _map_msg(cfg.id, cp.message_id, user.id, mm.message_id, ticket_id=ticket.id)
            await _maybe_pin_first_message(bot, cfg, created, cfg.admin_chat_id, copies[0].message_id)
        else:
            copies = await safe_call(bot.copy_messages, cfg.admin_chat_id, first.chat.id, ids,
                                     message_thread_id=thread)
            for mm, cp in zip(msgs, copies):
                await _map_msg(cfg.id, cp.message_id, user.id, mm.message_id, ticket_id=ticket.id)
            await _maybe_pin_first_message(bot, cfg, created, cfg.admin_chat_id, copies[0].message_id)
        return

    markup = _combine_kb(extra_kb, close_kb)
    if cfg.forward_mode == ForwardMode.forward and not markup:
        # forwardMessage не поддерживает reply_markup вообще (ограничение Bot
        # API) — но это ок, только когда кнопок нет. Если под сообщением
        # нужны кнопки (например "Принять/Отклонить" предложки), forward
        # такое молча не прикрепит, и раньше кнопки уходили ОТДЕЛЬНЫМ
        # сообщением ПОСЛЕ фото/текста — визуально выглядело как "разрыв".
        # Поэтому при наличии markup ниже используем copy_message вместо
        # forward: копия поддерживает reply_markup и приходит ОДНИМ
        # сообщением вместе с фото/текстом/подписью.
        sent = await safe_call(bot.forward_message, cfg.admin_chat_id, first.chat.id,
                               first.message_id, message_thread_id=thread)
        await _map_msg(cfg.id, sent.message_id, user.id, first.message_id, ticket_id=ticket.id)
        await _maybe_pin_first_message(bot, cfg, created, cfg.admin_chat_id, sent.message_id)
    else:
        sent = await safe_call(bot.copy_message, cfg.admin_chat_id, first.chat.id,
                               first.message_id, message_thread_id=thread,
                               reply_markup=markup, reply_parameters=reply_params)
        await _map_msg(cfg.id, sent.message_id, user.id, first.message_id, ticket_id=ticket.id)
        await _maybe_pin_first_message(bot, cfg, created, cfg.admin_chat_id, sent.message_id)


async def _mirror_reaction(bot: Bot, chat_id: int, message_id: int, reactions):
    """Ставит на сообщение те же реакции-эмодзи. Кастомные (премиум) реакции
    бот ставить не может — пропускаем; недопустимые эмодзи — молча игнорим."""
    emojis = [ReactionTypeEmoji(emoji=r.emoji) for r in reactions
              if getattr(r, "type", None) == "emoji" and getattr(r, "emoji", None)]
    try:
        await bot.set_message_reaction(chat_id, message_id, reaction=emojis)
    except Exception:
        pass


# Защита от того, что один и тот же апдейт с /ban, /warn, /unban, /unwarn
# обработается дважды (например если сеть/Telegram ретраит доставку, или
# апдейт долетел до бота более одного раза) — тогда бан/варн "не работает",
# т.к. warn+autoban сразу гасится следующим unwarn и т.п. Держим в памяти
# последние обработанные (bot_db_id, message_id) недолго — этого достаточно,
# ретраи приходят почти сразу друг за другом.
_recent_mod_cmds: dict[tuple[int, int], float] = {}
_MOD_CMD_DEDUP_TTL = 30.0


def _mod_cmd_already_handled(bot_db_id: int, message_id: int) -> bool:
    now = _time.monotonic()
    key = (bot_db_id, message_id)
    # чистим старое, чтобы словарь не рос бесконечно
    stale = [k for k, ts in _recent_mod_cmds.items() if now - ts > _MOD_CMD_DEDUP_TTL]
    for k in stale:
        _recent_mod_cmds.pop(k, None)
    if key in _recent_mod_cmds:
        return True
    _recent_mod_cmds[key] = now
    return False


async def _target_from_reply(bot_db_id: int, m: Message) -> int | None:
    """Если /ban, /warn и т.п. отправлены РЕПЛАЕМ на копию сообщения
    пользователя в админ-чате (топик или нет — не важно), достаём айди
    пользователя из MsgMap, чтобы не приходилось вручную вводить ID."""
    if not m.reply_to_message:
        return None
    async with Session() as s:
        mp = await s.scalar(select(MsgMap).where(
            MsgMap.bot_id == bot_db_id,
            MsgMap.admin_chat_msg_id == m.reply_to_message.message_id))
    return mp.user_id if mp else None


def build_common_router() -> Router:
    r = Router()

    # ---------- модерация (работает и в ЛС, и в админ-чате) ----------
    # /ban, /warn, /unban, /unwarn можно писать РЕПЛАЕМ на сообщение
    # пользователя в админ-чате (с топиками или без) — тогда ID не нужен,
    # причина/срок передаются как есть в аргументах. Без реплая работает
    # как раньше — первым словом обязателен числовой Telegram ID.
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

    # ---------- донат в Stars ----------
    class _DonateKbText(BaseFilter):
        """Текст reply-кнопки доната из настроек бота.

        БАГ: раньше хендлер был прибит гвоздями к дефолтному тексту
        «⭐️ Донат» — если владелец переименовал кнопку, нажатие улетало в
        админ-чат как обычное сообщение вместо запуска доната.
        """
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
        # БАГ "блокировка не мешает писать": /donate и кнопка доната не
        # проверяли бан вообще — забаненный пользователь мог продолжать
        # пользоваться ботом через донат-флоу в обход бана.
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.donate_enabled:
            return
        await _ask_donate_kind(m, cfg, state)

    async def _ask_donate_kind(m: Message, cfg: ChildBot, state: FSMContext):
        # НОВОЕ: разовый донат или ежемесячная Stars-подписка
        # (subscription_period в send_invoice) — раньше был только разовый.
        # Подписки — Pro-функция владельца бота: если Pro не активен, сразу
        # уходим в старый флоу (только разовый донат), не показывая выбор.
        if not await referrals.is_pro(cfg.owner_id):
            await state.set_state(DonateSt.amount)
            await state.update_data(kind="one")
            await m.answer(f"{em('star')} Введите количество звёзд для доната (1–10000):")
            return
        await m.answer(
            f"{em('star')} Разовый донат или ежемесячная подписка?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐️ Разово", callback_data="donatekind:one")],
                [InlineKeyboardButton(text="🔁 Подписка (ежемесячно)", callback_data="donatekind:sub")],
            ]))

    @r.callback_query(F.data.startswith("donatekind:"))
    async def cb_donate_kind(c: CallbackQuery, bot_db_id: int, state: FSMContext):
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.donate_enabled:
            await c.answer()
            return
        kind = c.data.split(":")[1]
        if kind == "sub" and not await referrals.is_pro(cfg.owner_id):
            # Владелец мог выключить Pro уже после того, как кнопка была
            # показана — перепроверяем перед тем, как выставлять счёт.
            await c.answer("Подписки временно недоступны.", show_alert=True)
            return
        await state.set_state(DonateSt.amount)
        await state.update_data(kind=kind)
        if kind == "sub":
            # Подписки Telegram Stars ограничены ценой 1–2500 ⭐️ (в отличие
            # от разового доната, где потолок 10000).
            await c.message.answer(f"{em('star')} Введите цену подписки в звёздах "
                                   "за месяц (1–2500):")
        else:
            await c.message.answer(f"{em('star')} Введите количество звёзд для доната (1–10000):")
        await c.answer()

    @r.message(DonateSt.amount, F.chat.type == "private")
    async def donate_amount(m: Message, bot: Bot, bot_db_id: int, state: FSMContext):
        if await mod.is_banned(bot_db_id, m.from_user.id):
            await state.clear()
            return
        if m.text and m.text.startswith("/"):
            # команда в середине ввода — отменяем донат и отдаём команду дальше
            await state.clear()
            raise SkipHandler
        data = await state.get_data()
        kind = data.get("kind", "one")
        await state.clear()
        if not m.text or not m.text.strip().isdigit():
            await m.answer(f"{em('warn')} Нужно целое число звёзд. Попробуйте /donate ещё раз.")
            return
        cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.donate_enabled:
            return
        if kind == "sub" and not await referrals.is_pro(cfg.owner_id):
            await m.answer(f"{em('warn')} Подписки временно недоступны, попробуйте разовый донат.")
            return
        stars = int(m.text.strip())
        limit = 2500 if kind == "sub" else 10000
        if not 1 <= stars <= limit:
            await m.answer(f"{em('warn')} Число должно быть от 1 до {limit}.")
            return
        try:
            if kind == "sub":
                # subscription_period не поддерживается в aiogram напрямую —
                # отправляем через сырой HTTP-запрос к Bot API.
                import aiohttp, json as _json
                payload_raw = {
                    "chat_id": m.chat.id,
                    "title": "Подписка",
                    "description": f"Ежемесячная подписка на {stars} ⭐️",
                    "payload": f"donate:sub:{stars}",
                    "currency": "XTR",
                    "prices": _json.dumps([{"label": f"{stars} Stars", "amount": stars}]),
                    "subscription_period": 2592000,  # 30 дней
                }
                url = f"https://api.telegram.org/bot{bot.token}/sendInvoice"
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(url, data=payload_raw) as resp:
                        result = await resp.json()
                if not result.get("ok"):
                    raise RuntimeError(result.get("description", "unknown error"))
            else:
                await bot.send_invoice(
                    chat_id=m.chat.id,
                    title="Донат",
                    description=f"Поддержка на {stars} ⭐️",
                    payload=f"donate:one:{stars}", currency="XTR",
                    prices=[LabeledPrice(label=f"{stars} Stars", amount=stars)])
        except Exception as e:
            log.warning("donate_amount: не удалось выставить счёт (kind=%s) для бота %s: %s",
                        kind, bot_db_id, e)
            await m.answer(f"{em('warn')} Не удалось выставить счёт. Попробуйте ещё раз позже.")

    @r.pre_checkout_query()
    async def pre_checkout(q: PreCheckoutQuery):
        await q.answer(ok=True)

    @r.message(F.successful_payment)
    async def paid(m: Message, bot_db_id: int):
        sp = m.successful_payment
        stars = sp.total_amount
        payload = sp.invoice_payload or ""
        is_sub = payload.startswith("donate:sub:")
        async with Session() as s:
            s.add(Donation(bot_id=bot_db_id, user_id=m.from_user.id, stars=stars,
                           is_subscription=is_sub,
                           subscription_state="active" if is_sub else None,
                           telegram_payment_charge_id=sp.telegram_payment_charge_id,
                           subscription_expiration=sp.subscription_expiration_date))
            await s.commit()
        if is_sub:
            await m.answer(f"{em('party')} Спасибо за подписку на {stars} {em('star')}/мес! "
                           "Отменить можно в настройках Telegram (Звёзды и Premium → Подписки) "
                           "или командой /unsubscribe в этом боте.")
        else:
            await m.answer(f"{em('party')} Спасибо за донат {stars} {em('star')}!")

    @r.message(Command("unsubscribe"), F.chat.type == "private")
    async def unsubscribe(m: Message, bot: Bot, bot_db_id: int):
        # НОВОЕ: докрутка отмены подписки — пользователь может отменить сам,
        # не отправляя его в системные настройки Telegram (editUserStarSubscription,
        # доступен и получателю платежей, и плательщику).
        async with Session() as s:
            don = await s.scalar(select(Donation).where(
                Donation.bot_id == bot_db_id, Donation.user_id == m.from_user.id,
                Donation.is_subscription == True,  # noqa: E712
                Donation.subscription_state == "active",
                Donation.telegram_payment_charge_id.is_not(None),
            ).order_by(Donation.id.desc()))
        if not don:
            await m.answer(f"{em('info')} У вас нет активной подписки в этом боте.")
            return
        try:
            await bot.edit_user_star_subscription(
                m.from_user.id, don.telegram_payment_charge_id, is_canceled=True)
        except Exception as e:
            log.warning("unsubscribe: edit_user_star_subscription failed для бота %s: %s",
                        bot_db_id, e)
            await m.answer(f"{em('warn')} Не удалось отменить подписку, попробуйте позже "
                           "или через настройки Telegram (Звёзды и Premium → Подписки).")
            return
        async with Session() as s:
            obj = await s.get(Donation, don.id)
            obj.subscription_state = "canceled"
            await s.commit()
        await m.answer(f"{em('check')} Подписка отменена — она останется активной до конца "
                       "уже оплаченного периода и не продлится дальше.")

    @r.subscription()
    async def on_subscription_updated(sub: BotSubscriptionUpdated, bot: Bot, bot_db_id: int):
        """НОВОЕ: докрутка продления/отмены подписки. Update.subscription
        (Bot API) приходит боту при изменении статуса Stars-подписки:
        state == "canceled" — пользователь отменил (сам или через /unsubscribe),
        state == "active" — включил отменённую подписку обратно,
        state == "failed" — не удалось списать за очередной период.
        Ищем последнюю подписку этого пользователя в этом боте по
        invoice_payload и обновляем статус + уведомляем владельца в чат
        админов, чтобы не приходилось мониторить это вручную."""
        async with Session() as s:
            don = await s.scalar(select(Donation).where(
                Donation.bot_id == bot_db_id, Donation.user_id == sub.user.id,
                Donation.is_subscription == True,  # noqa: E712
            ).order_by(Donation.id.desc()))
            if don:
                don.subscription_state = sub.state
                await s.commit()
            cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.admin_chat_id:
            return
        label = {"canceled": f"{em('cross')} отменена", "active": f"{em('check')} возобновлена",
                 "failed": f"{em('warn')} не удалось списать оплату"}.get(sub.state, sub.state)
        uname = f"@{sub.user.username}" if sub.user.username else sub.user.full_name
        try:
            await bot.send_message(cfg.admin_chat_id,
                                   f"🔁 Подписка на донат — {uname} (id {sub.user.id}): {label}")
        except Exception:
            pass

    @r.callback_query(F.data == "donate_btn")
    async def cb_donate(c: CallbackQuery, bot_db_id: int, state: FSMContext):
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        cfg = await get_cfg(bot_db_id)
        if not cfg or not cfg.donate_enabled:
            await c.answer()
            return
        await _ask_donate_kind(c.message, cfg, state)
        await c.answer()

    # ---------- самостоятельное закрытие обращения пользователем ----------
    class _CloseTicketKbText(BaseFilter):
        """Текст reply-кнопки закрытия обращения — по тому же принципу, что
        и _DonateKbText выше (учитывает переименование кнопки владельцем)."""
        async def __call__(self, m: Message, bot_db_id: int) -> bool:
            if not m.text:
                return False
            cfg = await get_cfg(bot_db_id)
            return bool(cfg and cfg.close_ticket_button_text
                        and m.text.strip() == cfg.close_ticket_button_text.strip())

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
        # уведомляем админ-чат, что обращение закрыто САМИМ пользователем
        # (а не админом) — чтобы не было впечатления, будто оно повисло
        if cfg and cfg.admin_chat_id:
            try:
                await bot.send_message(
                    cfg.admin_chat_id, f"{em('info')} Пользователь сам закрыл обращение.",
                    message_thread_id=t.topic_id if cfg.use_topics else None)
            except Exception:
                pass

    # ---------- триггер-кнопки и кнопка "открыть обращение" ----------
    @r.callback_query(F.data.startswith("trg:"))
    async def cb_trigger(c: CallbackQuery, bot_db_id: int):
        # БАГ: триггер-кнопки не проверяли бан — забаненный мог продолжать
        # получать авто-ответы бота как ни в чём не бывало.
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        cfg = await get_cfg(bot_db_id)
        async with Session() as s:
            b = await s.get(BotButton, int(c.data.split(":")[1]))
        if b and (b.response_text or b.response_photo):
            await send_response(c.message, b.response_text, b.response_photo, bot_db_id=bot_db_id)
        await c.answer()

    @r.callback_query(F.data == "open_ticket")
    async def cb_open_ticket(c: CallbackQuery, bot: Bot, bot_db_id: int):
        # БАГ: кнопка "открыть обращение" не проверяла бан — самый прямой
        # путь для забаненного снова начать писать в чат админов в обход бана.
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        cfg = await get_cfg(bot_db_id)
        # БАГ (по репорту): раньше здесь всегда стоял force_new=True — эта
        # кнопка (в отличие от inline_ticket/keyboard_ticket, см.
        # open_ticket() выше) НЕ проверяла конфликт и, если у владельца бота
        # включена настройка always_new_ticket, при каждом нажатии молча
        # ЗАКРЫВАЛА текущее открытое обращение и открывала новый топик —
        # получалось, что можно было "наплодить" параллельные обращения
        # нажатиями кнопки, хотя правило "1 пользователь = 1 обращение"
        # должно быть строгим. Теперь эта кнопка ведёт себя так же, как
        # остальные кнопки открытия обращения — если что-то уже открыто,
        # сообщаем об этом вместо тихого создания нового.
        ticket, created, conflict = await open_ticket(bot, cfg, c.from_user.id)
        if conflict:
            close_hint = (f" или кнопкой «{cfg.close_ticket_button_text}»"
                         if cfg.close_ticket_button_text else "")
            await c.answer(
                f"У вас уже есть открытое обращение. Закройте его командой "
                f"/close{close_hint}, чтобы открыть новое.",
                show_alert=True)
            return
        await c.answer("Обращение открыто! Напишите сообщение.", show_alert=True)

    @r.callback_query(F.data.startswith("open_ticket_subj:"))
    async def cb_open_ticket_subject(c: CallbackQuery, bot: Bot, bot_db_id: int):
        """Открытие обращения по конкретной теме (п.3) — кнопка inline_ticket.
        Текст/фото ответа (b.response_text/response_photo) — то, что
        владелец настроил присылать при открытии темы. Правило "1
        пользователь = 1 открытое обращение" строгое — если уже что-то
        открыто, новое НЕ создаётся, юзеру предлагается сперва закрыть
        текущее (/close или кнопка)."""
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        btn_id = int(c.data.split(":")[1])
        async with Session() as s:
            b = await s.get(BotButton, btn_id)
        if not b or b.bot_id != bot_db_id or b.kind != "inline_ticket":
            await c.answer("Кнопка не найдена", show_alert=True)
            return
        cfg = await get_cfg(bot_db_id)
        ticket, created, conflict = await open_ticket(bot, cfg, c.from_user.id, subject=b.text)
        if conflict:
            close_hint = (f" или кнопкой «{cfg.close_ticket_button_text}»"
                         if cfg.close_ticket_button_text else "")
            await c.answer(
                f"У вас уже есть открытое обращение"
                f"{f' («{ticket.subject}»)' if ticket.subject else ''}. "
                f"Закройте его командой /close{close_hint}, "
                "чтобы открыть новое.", show_alert=True)
            return
        if b.response_text or b.response_photo:
            await send_response(c.message, b.response_text, b.response_photo, bot_db_id=bot_db_id)
        await c.answer(f"Обращение «{b.text}» открыто!", show_alert=True)

    # ---------- триггер-команды (ОБЩИЕ для обоих типов ботов) ----------
    # БАГ: раньше жили только в фидбек-роутере — в постинг-ботах
    # триггер-команды не работали вообще, а сами команды улетали в админ-чат
    # как предложка.
    @r.message(F.chat.type == "private", F.text.startswith("/"))
    async def custom_command(m: Message, bot_db_id: int):
        cmd = m.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in RESERVED_COMMANDS:
            raise SkipHandler
        # БАГ: пользовательские триггер-команды не проверяли бан —
        # забаненный мог продолжать получать авто-ответы через них.
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        cfg = await get_cfg(bot_db_id)
        async with Session() as s:
            b = await s.scalar(select(BotButton).where(
                BotButton.bot_id == bot_db_id, BotButton.kind == "command",
                BotButton.text == cmd))
        if not b:
            # КРИТИЧНО: неизвестную/чужую команду нельзя "съедать" молча —
            # пропускаем дальше по роутерам (/newpost, /cancel и т.п. живут в
            # роутере конкретного типа бота).
            raise SkipHandler
        await send_response(m, b.response_text, b.response_photo, bot_db_id=bot_db_id)

    # ---------- закрытие / переоткрытие обращения ----------
    async def _close_ticket_core(bot: Bot, bot_db_id: int, tid: int,
                                 notify_user: bool = True) -> str | None:
        """Общая логика закрытия тикета — используется и инлайн-кнопкой
        (доступна только в copy-режиме), и командой /close (работает в
        любом режиме — единственный способ закрыть тикет при forward, и
        альтернативный способ при copy), и самим пользователем через /close.
        Возвращает текст ошибки (если что-то пошло не так) или None при
        успехе. notify_user=False — не слать юзеру уведомление о закрытии
        (используется, когда закрывает сам пользователь — ему и так придёт
        прямой ответ на его /close, дублировать не нужно)."""
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
        # НОВОЕ (по запросу): текст уведомления теперь настраивается в
        # конструкторе (cfg.close_notify_text, HTML — поддерживает
        # <tg-emoji emoji-id="..."> для premium-эмодзи, см. Bot API 9.4).
        # Пустое значение/NULL — уведомление НЕ отправляется вовсе (раньше
        # было невозможно ни изменить, ни убрать этот текст).
        if notify_user and cfg.close_notify_text:
            try:
                sent = None
                if await rich_enabled(None, cfg=cfg):
                    try:
                        sent = await bot.send_rich_message(
                            t.user_id, InputRichMessage(html=cfg.close_notify_text),
                            reply_markup=ReplyKeyboardRemove())
                    except Exception as e:
                        log.warning("close notify: рич-сообщение не отправилось (%s), "
                                    "шлю обычным текстом", e)
                if sent is None:
                    await bot.send_message(t.user_id, cfg.close_notify_text,
                                           reply_markup=ReplyKeyboardRemove())
            except TelegramForbiddenError:
                await mod.mark_blocked_bot(bot_db_id, t.user_id)
            except Exception:
                pass
        return None

    @r.callback_query(F.data.startswith("close_ticket:"))
    async def cb_close_ticket(c: CallbackQuery, bot: Bot, bot_db_id: int):
        # Закрыть обращение может любой участник админ-чата.
        # После действия в чат пишется кто именно закрыл.
        tid = int(c.data.split(":")[1])
        err = await _close_ticket_core(bot, bot_db_id, tid)
        if err:
            await c.answer(err, show_alert=(err == "Обращение не найдено"))
            return
        uname = c.from_user.username
        actor = f"@{uname}" if uname else str(c.from_user.id)
        try:
            await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[styled_button("🔓 Открыть снова",
                                                callback_data=f"reopen_ticket:{tid}")]]))
        except Exception:
            pass
        try:
            await c.message.answer(f"🔒 Закрыл — {actor}")
        except Exception:
            pass
        await c.answer("Обращение закрыто")

    # ---------- /close: закрыть обращение командой (реплаем или в топике) ----------
    # БАГ (по запросу): в режиме forward инлайн-кнопку закрытия вообще
    # некуда прицепить (forwardMessage не поддерживает reply_markup) — там
    # это ЕДИНСТВЕННЫЙ способ закрыть тикет. В copy-режиме работает как
    # равноценная альтернатива кнопке.
    @r.message(Command("close"))
    async def cmd_close(m: Message, bot: Bot, bot_db_id: int):
        # Боты-анкеты (п.4) не используют обращения/тикеты — там у /close
        # другой смысл (отмена текущей незаполненной анкеты), обрабатывается
        # в child/survey.py::build_survey_router().
        cfg0 = await get_cfg(bot_db_id)
        if cfg0 and cfg0.bot_type == BotType.survey:
            raise SkipHandler

        # БАГ (по факту репорта): раньше ветка "юзер закрывает своё
        # обращение" выбиралась ТОЛЬКО если is_bot_admin(...) вернул False.
        # Но владелец бота (и любой добавленный админ) is_bot_admin ВСЕГДА
        # возвращает True — включая ситуацию, когда владелец сам тестирует
        # бота, просто написав ему в ЛИЧКУ. В итоге /close у владельца в
        # личке уходил в админскую ветку (которая требует реплай на
        # сообщение в чате админов или запуск прямо в топике) и ожидаемо не
        # находила ничего — "Не нашёл открытое обращение...".
        #
        # Правильный критерий — тип чата, а не права: если это ЛИЧКА с
        # ботом (m.chat.type == "private"), то ЛЮБОЙ, кто туда пишет — и
        # обычный пользователь, и владелец/админ, который сейчас переписывается
        # с ботом как пользователь — закрывает СВОЁ СОБСТВЕННОЕ открытое
        # обращение. Админская логика (по реплаю/топику) имеет смысл только
        # в самом чате админов (группе), поэтому теперь применяется только
        # там.
        if m.chat.type == "private":
            async with Session() as s:
                t = await s.scalar(select(Ticket).where(
                    Ticket.bot_id == bot_db_id, Ticket.user_id == m.from_user.id,
                    Ticket.is_open))
            if not t:
                await m.answer(f"{em('info')} У вас нет открытых обращений.")
                return
            err = await _close_ticket_core(bot, bot_db_id, t.id, notify_user=False)
            await m.answer(
                f"{em('cross')} {err}" if err else
                f"{em('lock')} Обращение закрыто. Напишите сообщение, чтобы открыть новое.",
                reply_markup=ReplyKeyboardRemove() if not err else None)
            return

        # Дальше — только чат админов (группа/супергруппа). Проверяем права:
        # обычные пользователи там оказаться не должны, но на всякий случай.
        if not await is_bot_admin(bot_db_id, m.from_user.id):
            return
        ticket_id = None
        if m.reply_to_message:
            # НОВОЕ (п.3): у пользователя может быть НЕСКОЛЬКО параллельно
            # открытых обращений (по разным темам) — раньше тут искалось
            # "открытое обращение этого юзера" без уточнения, что при
            # нескольких открытых стало бы неоднозначным. Теперь сначала
            # пробуем достать ТОЧНОЕ обращение из MsgMap (по какому именно
            # сообщению кликнули reply), и только если такой записи нет
            # (старые сообщения без ticket_id) — откатываемся на прежний
            # эвристический поиск по user_id.
            async with Session() as s:
                mp = await s.scalar(select(MsgMap).where(
                    MsgMap.bot_id == bot_db_id,
                    MsgMap.admin_chat_msg_id == m.reply_to_message.message_id))
            if mp and mp.ticket_id is not None:
                async with Session() as s:
                    t = await s.get(Ticket, mp.ticket_id)
                if t and t.is_open:
                    ticket_id = t.id
            if ticket_id is None:
                uid = await _target_from_reply(bot_db_id, m)
                if uid is not None:
                    async with Session() as s:
                        t = await s.scalar(select(Ticket).where(
                            Ticket.bot_id == bot_db_id, Ticket.user_id == uid,
                            Ticket.is_open).order_by(Ticket.last_active_at.desc()))
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
        # Переоткрыть обращение может любой участник админ-чата.
        # После действия в чат пишется кто именно переоткрыл.
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
        uname = c.from_user.username
        actor = f"@{uname}" if uname else str(c.from_user.id)
        try:
            await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[styled_button("🔒 Закрыть обращение",
                                                callback_data=f"close_ticket:{tid}")]]))
        except Exception:
            pass
        try:
            await c.message.answer(f"🔓 Переоткрыл — {actor}")
        except Exception:
            pass
        await c.answer("Обращение снова открыто")

    # ---------- админ-чат: ответы пользователям (ОБЩИЕ для обоих типов) ----------
    @r.message(F.chat.type.in_({"group", "supergroup"}))
    async def admin_reply(m: Message, bot: Bot, bot_db_id: int):
        cfg = await get_cfg(bot_db_id)
        if not cfg or m.chat.id != cfg.admin_chat_id or m.from_user.is_bot:
            return
        # НОВОЕ (по запросу): в ботах-анкетах переписка с респондентом — это
        # отдельный переключатель "режим диалога" (survey_dialog_enabled,
        # по умолчанию ВЫКЛЮЧЕН). Если выключен — ответ админа на присланную
        # анкету никуда не доставляется, как будто MsgMap не существует.
        if cfg.bot_type == BotType.survey and not cfg.survey_dialog_enabled:
            return
        if m.text and m.text.startswith("/"):
            return  # команды модерации обработаны выше; неизвестные — игнорим
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
                # reply-контекст в обратную сторону: юзер видит, на какое его
                # сообщение ответил админ.
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
            # НОВОЕ (по запросу, п.6): реакция теперь настраивается
            # (cfg.admin_reply_reaction) и может быть выключена (None/"").
            if cfg.admin_reply_reaction:
                try:
                    await m.react([{"type": "emoji", "emoji": cfg.admin_reply_reaction}])
                except Exception:
                    pass  # реакция необязательна — недоставленной по её вине быть не должно
        except TelegramForbiddenError:
            # БАГ: сообщение об ошибке говорило "заблокировал бота", но
            # нигде это не сохранялось — в статистике человек всё равно
            # числился активным, пока его случайно не задевала рассылка.
            await mod.mark_blocked_bot(bot_db_id, target_uid)
            await m.reply(f"{em('cross')} Не доставлено (пользователь заблокировал бота).")
        except Exception:
            await m.reply(f"{em('cross')} Не доставлено (пользователь заблокировал бота).")

    # ---------- реакции-эмодзи: зеркалим в обе стороны ----------
    # Юзер ставит реакцию в ЛС -> та же реакция появляется на копии в
    # админ-чате. Админ ставит реакцию в админ-чате -> она появляется на
    # сообщении юзера. (allowed_updates подхватывается автоматически, т.к.
    # дочерние диспетчеры используют resolve_used_update_types().)
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
