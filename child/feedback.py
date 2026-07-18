from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from db.base import Session
from db.models import ChildBot, MessageLog, OpenMode
from services import moderation as mod
from services import antispam
from child.common import (inject_extras, build_keyboards, send_with_keyboards,
                          handle_keyboard_button, open_ticket, get_cfg,
                          buffer_or_process, relay_to_admin_chat, is_bot_admin,
                          should_apply_antispam, terms_gate_blocks, send_terms_gate)
from utils.emoji import em


async def _cfg(bot_db_id: int) -> ChildBot:
    return await get_cfg(bot_db_id)


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
        # БАГ (по запросу): бот отправлял приветствие ДО согласия с
        # политикой конфиденциальности/соглашением/политикой возвратов —
        # теперь /start сначала показывает экран согласия и ничего больше
        # не делает, пока человек не нажмёт "Принимаю".
        if await terms_gate_blocks(bot_db_id, cfg, m.from_user.id):
            await send_terms_gate(m, cfg)
            return
        ikb, rkb = await build_keyboards(bot_db_id, cfg)
        welcome = await inject_extras(bot_db_id, cfg.welcome_text)
        await send_with_keyboards(m, welcome, ikb, rkb, photo=cfg.welcome_photo)
        if cfg.open_mode == OpenMode.start_command:
            await open_ticket(bot, cfg, m.from_user.id)

    @r.message(Command("restart"), F.chat.type == "private")
    async def restart(m: Message, bot: Bot, bot_db_id: int):
        # БАГ: /restart не проверял бан вообще — забаненный пользователь мог
        # открыть новое обращение и снова писать в обход бана.
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        cfg = await _cfg(bot_db_id)
        if await terms_gate_blocks(bot_db_id, cfg, m.from_user.id):
            await send_terms_gate(m, cfg)
            return
        await open_ticket(bot, cfg, m.from_user.id, force_new=True)
        await m.answer(f"{em('new')} Новое обращение открыто!"
                       if cfg.always_new_ticket else
                       f"{em('check')} Обращение продолжено.")

    # Триггер-команды, кнопки open_ticket/trg/donate, ответы админов и
    # реакции обрабатываются в child/common.py::build_common_router() — он
    # подключается ко ВСЕМ ботам (и фидбек-, и постинг-).

    @r.message(F.chat.type == "private")
    async def user_message(m: Message, bot: Bot, bot_db_id: int, state: FSMContext):
        cfg = await _cfg(bot_db_id)
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id, direction="in"))
            await s.commit()
        if await terms_gate_blocks(bot_db_id, cfg, m.from_user.id):
            await send_terms_gate(m, cfg)
            return
        # Антиспам (rate-limit/капча/прогрессирующий тайм-аут) — обычных
        # админов не трогает, владельца — в зависимости от тоггла
        # cfg.antispam_ignore_owner (см. child/common.py::should_apply_antispam).
        if await should_apply_antispam(bot_db_id, cfg, m.from_user.id):
            res = await antispam.check(bot_db_id, cfg, m.from_user.id, m.text)
            if not res.allowed:
                if res.notice:
                    await m.answer(res.notice)
                return
        # БАГ: сообщения, отправленные ВО ВРЕМЯ FSM-диалога (например сумма
        # доната после кнопки), при некоторых условиях улетали в админ-чат
        # как обычные. Если идёт любой FSM-ввод — тут делать нечего.
        if await state.get_state() is not None:
            return
        if await handle_keyboard_button(m, bot_db_id):
            return
        # БАГ: неизвестные команды (/что-угодно) раньше релеились в админ-чат
        # как обращение. Триггер-команды из конструктора уже обработаны в
        # common-роутере — сюда доходят только чужие/неизвестные.
        if m.text and m.text.startswith("/"):
            return
        if not cfg.admin_chat_id:
            return

        async def _process(msgs: list[Message]):
            await relay_to_admin_chat(msgs, bot, cfg)

        # БАГ "с фотками сложно": альбомы релеились по одному фото, каждое с
        # ОТДЕЛЬНОЙ шапкой (в copy-режиме) — админ-чат превращался в спам.
        # Теперь фидбек-бот копит альбом и шлёт его одной группой с одной
        # шапкой, как постинг-бот.
        await buffer_or_process(m, _process)

    return r
