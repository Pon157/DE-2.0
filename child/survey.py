
"""Боты-анкеты (п.4, по запросу) — новый тип дочернего бота (BotType.survey).

Владелец настраивает одну или несколько анкет (Survey) в конструкторе,
каждая — набор вопросов (SurveyQuestion): либо свободный текст, либо
варианты ответа кнопками. Анкета запускается инлайн- или кейборд-кнопкой
(BotButton.kind in ("inline_survey", "keyboard_survey"), BotButton.survey_id
указывает на конкретную анкету) — таких кнопок/анкет может быть несколько.

Бан/варн и вообще модерация — общие для всех типов ботов, см.
child/common.py::build_common_router() (подключается ко всем ботам).
"""
import html
import json
import logging
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from db.base import Session
from db.models import (BotButton, BotUser, ChildBot, MessageLog, MsgMap, Survey,
                       SurveyQuestion, SurveyResponse)
from services import moderation as mod
from services import antispam
from services import referrals
from child.common import (inject_extras, build_keyboards, send_with_keyboards,
                          handle_keyboard_button, get_cfg, is_bot_admin,
                          should_apply_antispam, safe_call, send_response,
                          message_media, build_topic_name, welcome_pro_kwargs,
                          rich_enabled, send_rich_or_plain)
from utils.emoji import em
from sqlalchemy import select

log = logging.getLogger("child.survey")


async def _questions(survey_id: int) -> list[SurveyQuestion]:
    async with Session() as s:
        return list((await s.scalars(
            select(SurveyQuestion).where(SurveyQuestion.survey_id == survey_id)
            .order_by(SurveyQuestion.position))).all())


async def _in_progress(bot_db_id: int, user_id: int) -> SurveyResponse | None:
    async with Session() as s:
        return await s.scalar(select(SurveyResponse).where(
            SurveyResponse.bot_id == bot_db_id, SurveyResponse.user_id == user_id,
            SurveyResponse.completed == False))  # noqa: E712


def _question_kb(resp_id: int, q: SurveyQuestion) -> InlineKeyboardMarkup | None:
    if q.qtype != "choice":
        return None
    opts = json.loads(q.options_json or "[]")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=opt, callback_data=f"survey_ans:{resp_id}:{i}")]
        for i, opt in enumerate(opts)
    ])


async def _send_question_media(m: Message, media_type: str, file_id: str,
                               caption: str | None, reply_markup):
    """Отправка медиа-вложения вопроса анкеты (см. докстринг SurveyQuestion в
    db/models.py) — caption поддерживает HTML/premium-эмодзи, т.к. дочерний
    бот создаётся с default parse_mode="HTML" (см. services/bot_manager.py).
    Caption ограничен 1024 символами — если текст вопроса длиннее, шлём
    медиа без подписи и текст отдельным сообщением следом (тот же приём,
    что и в send_response/send_with_keyboards в child/common.py)."""
    kwargs = {"reply_markup": reply_markup}
    text_after = None
    if caption and len(caption) > 1024:
        kwargs_media = {}
        text_after = caption
    else:
        kwargs_media = {"caption": caption} if caption else {}
    try:
        if media_type == "photo":
            sent = await m.answer_photo(file_id, **kwargs_media, **({} if text_after else kwargs))
        elif media_type == "video":
            sent = await m.answer_video(file_id, **kwargs_media, **({} if text_after else kwargs))
        elif media_type == "animation":
            sent = await m.answer_animation(file_id, **kwargs_media, **({} if text_after else kwargs))
        elif media_type == "document":
            sent = await m.answer_document(file_id, **kwargs_media, **({} if text_after else kwargs))
        elif media_type == "audio":
            sent = await m.answer_audio(file_id, **kwargs_media, **({} if text_after else kwargs))
        elif media_type == "voice":
            sent = await m.answer_voice(file_id, **kwargs_media, **({} if text_after else kwargs))
        elif media_type == "video_note":
            # video_note не поддерживает caption/reply_markup вообще —
            # шлём кружок, а текст вопроса и кнопки вариантов — отдельным
            # сообщением следом, иначе они бы просто терялись.
            await m.answer_video_note(file_id)
            text_after = caption
            sent = None
        elif media_type == "sticker":
            await m.answer_sticker(file_id)
            text_after = caption
            sent = None
        else:
            text_after = caption
            sent = None
    except Exception as e:
        log.warning("survey._send_question_media: не удалось отправить медиа "
                    "вопроса (%s), шлю только текст", e)
        text_after = caption
        sent = None
    if text_after:
        sent = await m.answer(text_after, reply_markup=reply_markup)
    elif reply_markup is not None and sent is None:
        # Медиа без caption (video_note/sticker) и без текста вопроса, но
        # есть варианты ответа кнопками — кнопкам нужно на чём-то висеть.
        sent = await m.answer(f"{em('info')} Выберите вариант ответа:", reply_markup=reply_markup)
    return sent


async def _ask_current(m: Message, resp: SurveyResponse, questions: list[SurveyQuestion],
                       cfg: ChildBot | None = None):
    q = questions[resp.current_index]
    kb = _question_kb(resp.id, q)
    # НОВОЕ (фикс бага п.1 из запроса): вопрос анкеты теперь может нести
    # медиа-вложение, а текст вопроса — это HTML (см. миграцию вопроса на
    # m.html_text в master/router.py::survey_q_text_save) — раньше сюда
    # приходил голый m.text без единого тега форматирования/premium-эмодзи.
    if q.media_file_id:
        await _send_question_media(m, q.media_type, q.media_file_id, q.text or None, kb)
        return
    # НОВОЕ: рич-текст вопроса (Pro, расширение "на все экраны" по запросу)
    # — рич-сообщение не совместимо с media, поэтому только для текстовых
    # вопросов без вложения.
    if cfg is not None and await rich_enabled(None, cfg=cfg):
        if await send_rich_or_plain(m, q.text, reply_markup=kb) is not None:
            return
    await m.answer(q.text, reply_markup=kb)


async def _get_or_create_topic(bot: Bot, cfg: ChildBot, user_id: int) -> int | None:
    """Топик в чате админов для анкет этого пользователя в этом боте (фикс
    бага "в анкетах по топикам анкеты не отправляются" — см. докстринг
    SurveyResponse.topic_id в db/models.py). Переиспользует топик последней
    анкеты/диалога этого пользователя, если он уже есть, иначе заводит новый
    — тот же приём, что и в child/common.py::open_ticket, с тем же мягким
    фолбэком на отправку без топика, если чат админов — не форум."""
    if not cfg.use_topics or not cfg.admin_chat_id:
        return None
    async with Session() as s:
        existing = await s.scalar(select(SurveyResponse.topic_id).where(
            SurveyResponse.bot_id == cfg.id, SurveyResponse.user_id == user_id,
            SurveyResponse.topic_id.is_not(None)).order_by(SurveyResponse.id.desc()))
        if existing:
            return existing
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == cfg.id, BotUser.user_id == user_id))
    topic_name = build_topic_name(cfg, user_id,
                                  full_name=(u.full_name if u else None),
                                  username=(u.username if u else None))
    # Иконка топика — Pro-функция; если Pro у владельца истёк, топик всё
    # равно создаём (топики — это фикс бага, не Pro-фича), просто без иконки.
    icon = cfg.topic_icon_emoji_id if await referrals.is_pro(cfg.owner_id) else None
    try:
        topic = await bot.create_forum_topic(
            cfg.admin_chat_id, topic_name,
            icon_custom_emoji_id=icon or None)
        return topic.message_thread_id
    except Exception as e:
        log.warning("survey._get_or_create_topic: create_forum_topic failed "
                    "для бота %s (%s) — продолжаю без топика", cfg.id, e)
        return None


async def _finish(bot: Bot, cfg: ChildBot, resp_id: int):
    """Сохраняет (уже сохранено по ходу анкеты) и пересылает заполненную
    анкету в чат админов — как обычное обращение."""
    async with Session() as s:
        user_id = (await s.get(SurveyResponse, resp_id)).user_id
    # НОВОЕ (фикс бага топиков): топик заводится/переиспользуется ДО того,
    # как анкета помечается завершённой — resp.topic_id сохраняется вместе с
    # ответами, чтобы дальнейшие сообщения "режима диалога" от этого же
    # пользователя (см. _relay_dialog_message) уходили в тот же топик.
    thread = await _get_or_create_topic(bot, cfg, user_id)
    async with Session() as s:
        resp = await s.get(SurveyResponse, resp_id)
        survey = await s.get(Survey, resp.survey_id)
        resp.completed = True
        resp.completed_at = datetime.utcnow()
        resp.topic_id = thread
        await s.commit()
    answers = json.loads(resp.answers_json or "[]")
    lines = [f"📋 <b>Анкета «{html.escape(survey.name)}»</b>",
            f"👤 <code>{resp.user_id}</code>"]
    # media-ответы (по запросу: фото/аудио/etc. в ответ на вопрос анкеты) —
    # шлём ОТДЕЛЬНЫМИ сообщениями после текста анкеты, т.к. эти файлы
    # получены ЭТИМ ЖЕ (дочерним) ботом — в отличие от рассылки, здесь
    # file_id валиден напрямую, перезаливка не нужна.
    media_answers = []
    for a in answers:
        a_text = a["a"] if a["a"] else "(без текста)"
        lines.append(f"\n<b>{html.escape(a['q'])}</b>\n{html.escape(a_text)[:3500]}")
        if a.get("media_file_id"):
            media_answers.append(a)
    text = "\n".join(lines)
    if cfg.admin_chat_id:
        # Длинный текст анкеты может легко перевалить за 4096 — режем на
        # части, чтобы отправка не падала целиком (тот же класс бага, что и
        # в п.1 с рассылкой).
        first_sent = None
        for i in range(0, len(text), 4000):
            try:
                sent = await safe_call(bot.send_message, cfg.admin_chat_id, text[i:i + 4000],
                                       message_thread_id=thread)
                first_sent = first_sent or sent
            except Exception as e:
                log.warning("survey._finish: не удалось отправить в admin_chat_id "
                            "бота %s: %s", cfg.id, e)
        # НОВОЕ (по запросу): чтобы админ мог написать подавшему анкету —
        # реплаем на её сообщение в чате админов, как это уже работает в
        # фидбек-ботах. Сама доставка ответа обрабатывается ОБЩИМ хендлером
        # admin_reply в child/common.py::build_common_router() — он ищет
        # MsgMap по id сообщения, на которое ответил админ, и копирует ответ
        # адресату. Тут нужно только создать эту связь при отправке анкеты.
        if first_sent:
            async with Session() as s:
                s.add(MsgMap(bot_id=cfg.id, admin_chat_msg_id=first_sent.message_id,
                             user_id=resp.user_id, user_chat_msg_id=None))
                await s.commit()
        for a in media_answers:
            try:
                await _send_answer_media(bot, cfg.admin_chat_id, a["media_type"],
                                         a["media_file_id"], caption=f"↳ {a['q']}",
                                         message_thread_id=thread)
            except Exception as e:
                log.warning("survey._finish: не удалось переслать медиа-ответ "
                            "бота %s: %s", cfg.id, e)
    return thread


async def _send_answer_media(bot: Bot, chat_id: int, media_type: str, file_id: str,
                             caption: str | None = None, message_thread_id: int | None = None):
    kw = {"caption": caption, "message_thread_id": message_thread_id}
    if media_type == "photo":
        await bot.send_photo(chat_id, file_id, **kw)
    elif media_type == "video":
        await bot.send_video(chat_id, file_id, **kw)
    elif media_type == "document":
        await bot.send_document(chat_id, file_id, **kw)
    elif media_type == "animation":
        await bot.send_animation(chat_id, file_id, **kw)
    elif media_type == "audio":
        await bot.send_audio(chat_id, file_id, **kw)
    elif media_type == "voice":
        await bot.send_voice(chat_id, file_id, **kw)
    elif media_type == "video_note":
        await bot.send_video_note(chat_id, file_id, message_thread_id=message_thread_id)
    elif media_type == "sticker":
        await bot.send_sticker(chat_id, file_id, message_thread_id=message_thread_id)


async def _relay_dialog_message(m: Message, bot: Bot, cfg: ChildBot):
    """НОВОЕ (по запросу): "режим диалога" — свободное сообщение
    респондента ПОСЛЕ отправки анкеты (не ответ на текущий вопрос)
    пересылается в чат админов копией, с привязкой MsgMap, чтобы админ мог
    ответить (доставку ответа берёт на себя общий admin_reply в
    child/common.py). Если это реплай на сообщение, у которого уже есть
    копия в чате админов — прикрепляем нашу копию туда же (видно, на что
    именно отвечает респондент), как и в фидбек-ботах.
    """
    reply_params = None
    if m.reply_to_message:
        async with Session() as s:
            mp = await s.scalar(select(MsgMap).where(
                MsgMap.bot_id == cfg.id,
                MsgMap.user_chat_msg_id == m.reply_to_message.message_id
            ).order_by(MsgMap.id.desc()))
        if mp:
            reply_params = ReplyParameters(message_id=mp.admin_chat_msg_id)
    # НОВОЕ (фикс бага топиков): сообщения "режима диалога" тоже должны
    # уходить в топик пользователя, а не теряться/падать в General, если он
    # закрыт (см. _get_or_create_topic).
    thread = await _get_or_create_topic(bot, cfg, m.from_user.id)
    try:
        sent = await bot.copy_message(cfg.admin_chat_id, m.chat.id, m.message_id,
                                      message_thread_id=thread,
                                      reply_parameters=reply_params)
    except Exception as e:
        log.warning("survey._relay_dialog_message: не удалось переслать в "
                    "admin_chat_id бота %s: %s", cfg.id, e)
        return
    async with Session() as s:
        s.add(MsgMap(bot_id=cfg.id, admin_chat_msg_id=sent.message_id,
                     user_id=m.from_user.id, user_chat_msg_id=m.message_id))
        await s.commit()


async def _advance_or_finish(bot: Bot, cfg: ChildBot, m: Message, resp_id: int):
    async with Session() as s:
        resp = await s.get(SurveyResponse, resp_id)
    questions = await _questions(resp.survey_id)
    if resp.current_index >= len(questions):
        await _finish(bot, cfg, resp_id)
        # НОВОЕ (фикс бага п.2 из запроса): финальное сообщение теперь может
        # нести медиа-вложение вместе с HTML-текстом/premium-эмодзи — раньше
        # это был только m.answer(текст) без единой возможности прикрепить
        # фото/видео (см. survey_finish_media_id в db/models.py и
        # surveyfinish_save в master/router.py).
        finish_text = cfg.survey_start_text or f"{em('check')} Спасибо! Анкета отправлена."
        if cfg.survey_finish_media_id:
            await _send_question_media(m, cfg.survey_finish_media_type,
                                       cfg.survey_finish_media_id,
                                       cfg.survey_start_text or None, None)
        elif await rich_enabled(None, cfg=cfg) and await send_rich_or_plain(m, finish_text) is not None:
            pass
        else:
            await m.answer(finish_text)
        return
    await _ask_current(m, resp, questions, cfg=cfg)


async def _start_or_resume(m: Message, bot_db_id: int, user_id: int, survey_id: int):
    questions = await _questions(survey_id)
    if not questions:
        await m.answer(f"{em('warn')} В этой анкете пока нет вопросов — "
                       "владелец бота ещё не настроил её.")
        return
    async with Session() as s:
        resp = await s.scalar(select(SurveyResponse).where(
            SurveyResponse.bot_id == bot_db_id, SurveyResponse.user_id == user_id,
            SurveyResponse.survey_id == survey_id, SurveyResponse.completed == False))  # noqa: E712
        if not resp:
            # БАГ: раньше тут ничего не мешало завести ВТОРУЮ незавершённую
            # SurveyResponse параллельно с уже начатой анкетой (по другому
            # survey_id) — везде в файле "текущая незавершённая анкета"
            # ищется через _in_progress() как select(...).scalar() ПО
            # bot_id+user_id БЕЗ survey_id, а scalar() при двух подходящих
            # строках падает с MultipleResultsFound — весь диалог с ботом
            # переставал отвечать пользователю. Теперь правило "1
            # пользователь = 1 анкета в процессе одновременно" — как и с
            # обращениями в фидбек-ботах (см. child/common.py::open_ticket).
            other = await s.scalar(select(SurveyResponse).where(
                SurveyResponse.bot_id == bot_db_id, SurveyResponse.user_id == user_id,
                SurveyResponse.completed == False))  # noqa: E712
            if other:
                await m.answer(f"{em('warn')} У вас уже есть незавершённая анкета. "
                               "Сначала закончите её или отмените командой /close.")
                return
            resp = SurveyResponse(bot_id=bot_db_id, survey_id=survey_id, user_id=user_id)
            s.add(resp)
            await s.commit()
            await s.refresh(resp)
    cfg = await get_cfg(bot_db_id)
    await _ask_current(m, resp, questions, cfg=cfg)


def build_survey_router() -> Router:
    r = Router()

    @r.message(CommandStart(), F.chat.type == "private")
    async def start(m: Message, bot: Bot, bot_db_id: int):
        cfg = await get_cfg(bot_db_id)
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            await s.commit()
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        ikb, rkb = await build_keyboards(bot_db_id, cfg)
        welcome = await inject_extras(bot_db_id, cfg.welcome_text)
        await send_with_keyboards(m, welcome, ikb, rkb, photo=cfg.welcome_photo,
                                   **await welcome_pro_kwargs(cfg))

    @r.message(Command("close"), F.chat.type == "private")
    async def cmd_close(m: Message, bot_db_id: int):
        # У анкет нет "обращений"-тикетов — /close тут отменяет текущую
        # незавершённую анкету (чтобы можно было начать заново с чистого
        # листа, не отвечая на "хвост" старой).
        if await is_bot_admin(bot_db_id, m.from_user.id):
            return  # у админов свой /close — см. child/common.py
        resp = await _in_progress(bot_db_id, m.from_user.id)
        if not resp:
            await m.answer(f"{em('info')} У вас нет анкеты в процессе заполнения.")
            return
        async with Session() as s:
            r2 = await s.get(SurveyResponse, resp.id)
            await s.delete(r2)
            await s.commit()
        await m.answer(f"{em('lock')} Анкета отменена. Чтобы начать заново — "
                       "выберите её в меню.")

    @r.callback_query(F.data.startswith("survey_start:"))
    async def cb_survey_start(c: CallbackQuery, bot_db_id: int):
        if await mod.is_banned(bot_db_id, c.from_user.id):
            await c.answer("Вы забанены в этом боте.", show_alert=True)
            return
        btn_id = int(c.data.split(":")[1])
        async with Session() as s:
            b = await s.get(BotButton, btn_id)
        if not b or b.bot_id != bot_db_id or b.kind != "inline_survey" or not b.survey_id:
            await c.answer("Анкета не найдена", show_alert=True)
            return
        if b.response_text and b.response_text.strip():
            await send_response(c.message, b.response_text, b.response_photo, bot_db_id=bot_db_id)
        await _start_or_resume(c.message, bot_db_id, c.from_user.id, b.survey_id)
        await c.answer()

    @r.callback_query(F.data.startswith("survey_ans:"))
    async def cb_survey_answer(c: CallbackQuery, bot: Bot, bot_db_id: int):
        _, resp_id_s, opt_idx_s = c.data.split(":")
        resp_id, opt_idx = int(resp_id_s), int(opt_idx_s)
        async with Session() as s:
            resp = await s.get(SurveyResponse, resp_id)
        if (not resp or resp.bot_id != bot_db_id or resp.user_id != c.from_user.id
                or resp.completed):
            await c.answer()
            return
        questions = await _questions(resp.survey_id)
        if resp.current_index >= len(questions):
            await c.answer()
            return
        q = questions[resp.current_index]
        opts = json.loads(q.options_json or "[]")
        if not (0 <= opt_idx < len(opts)):
            await c.answer()
            return
        chosen = opts[opt_idx]
        async with Session() as s:
            r2 = await s.get(SurveyResponse, resp_id)
            answers = json.loads(r2.answers_json or "[]")
            answers.append({"q": q.text, "a": chosen})
            r2.answers_json = json.dumps(answers, ensure_ascii=False)
            r2.current_index += 1
            await s.commit()
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        cfg = await get_cfg(bot_db_id)
        await _advance_or_finish(bot, cfg, c.message, resp_id)
        await c.answer(f"Вы выбрали: {chosen}")

    @r.message(F.chat.type == "private")
    async def user_message(m: Message, bot: Bot, bot_db_id: int):
        cfg = await get_cfg(bot_db_id)
        if not cfg or await mod.is_banned(bot_db_id, m.from_user.id):
            return
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id, direction="in"))
            await s.commit()
        if await should_apply_antispam(bot_db_id, cfg, m.from_user.id, bot):
            res = await antispam.check(bot_db_id, cfg, m.from_user.id, m.text, bot)
            if not res.allowed:
                if res.notice:
                    await m.answer(res.notice)
                return

        # Кейборд-кнопка запуска анкеты — текстом, как обычные кейборд-кнопки.
        if m.text:
            async with Session() as s:
                b = await s.scalar(select(BotButton).where(
                    BotButton.bot_id == bot_db_id, BotButton.kind == "keyboard_survey",
                    BotButton.text == m.text))
            if b and b.survey_id:
                if b.response_text and b.response_text.strip():
                    await send_response(m, b.response_text, b.response_photo, bot_db_id=bot_db_id)
                await _start_or_resume(m, bot_db_id, m.from_user.id, b.survey_id)
                return
        # Обычные кнопки/триггеры конструктора (не анкетные)
        if await handle_keyboard_button(m, bot_db_id):
            return
        if m.text and m.text.startswith("/"):
            return

        # Если пользователь сейчас в процессе анкеты — это ответ на текущий
        # текстовый вопрос.
        resp = await _in_progress(bot_db_id, m.from_user.id)
        if resp:
            questions = await _questions(resp.survey_id)
            if resp.current_index >= len(questions):
                return
            q = questions[resp.current_index]
            if q.qtype == "choice":
                await m.answer(f"{em('warn')} Пожалуйста, выберите один из вариантов "
                               "кнопкой выше.")
                return
            # По запросу: ответом на свободный текстовый вопрос анкеты
            # теперь можно прислать и медиа (фото/видео/аудио/голосовое/
            # документ/гиф/кружок/стикер) — раньше медиа без подписи просто
            # терялось и сохранялось как "(сообщение без текста)".
            media_file_id, media_type = message_media(m)
            answer_text = m.text or m.caption or ("" if media_file_id else "")
            answer = {"q": q.text, "a": answer_text}
            if media_file_id:
                answer["media_file_id"] = media_file_id
                answer["media_type"] = media_type
            async with Session() as s:
                r2 = await s.get(SurveyResponse, resp.id)
                answers = json.loads(r2.answers_json or "[]")
                answers.append(answer)
                r2.answers_json = json.dumps(answers, ensure_ascii=False)
                r2.current_index += 1
                await s.commit()
            await _advance_or_finish(bot, cfg, m, resp.id)
            return

        # Ни одна анкета не начата и это не ответ — если включён "режим
        # диалога", относимся к сообщению как к продолжению переписки с
        # админами (по запросу): пересылаем в admin_chat_id и запоминаем
        # связь, чтобы админ мог ответить (общий admin_reply в
        # child/common.py это подхватит). Реплай на сообщение админа
        # цепляется к той же ветке через reply_parameters, если получится
        # найти исходную копию.
        if cfg.survey_dialog_enabled and cfg.admin_chat_id:
            await _relay_dialog_message(m, bot, cfg)
            return

        # Ни одна анкета не начата и диалог выключен — подсказываем меню.
        ikb, rkb = await build_keyboards(bot_db_id, cfg)
        if ikb or rkb:
            await m.answer(f"{em('info')} Выберите анкету из меню ниже.",
                           reply_markup=ikb or rkb)

    return r

