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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.base import Session
from db.models import BotButton, ChildBot, MessageLog, Survey, SurveyQuestion, SurveyResponse
from services import moderation as mod
from services import antispam
from child.common import (inject_extras, build_keyboards, send_with_keyboards,
                          handle_keyboard_button, get_cfg, is_bot_admin,
                          should_apply_antispam, safe_call, send_response,
                          message_media)
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


async def _ask_current(m: Message, resp: SurveyResponse, questions: list[SurveyQuestion]):
    q = questions[resp.current_index]
    await m.answer(q.text, reply_markup=_question_kb(resp.id, q))


async def _finish(bot: Bot, cfg: ChildBot, resp_id: int):
    """Сохраняет (уже сохранено по ходу анкеты) и пересылает заполненную
    анкету в чат админов — как обычное обращение."""
    async with Session() as s:
        resp = await s.get(SurveyResponse, resp_id)
        survey = await s.get(Survey, resp.survey_id)
        resp.completed = True
        resp.completed_at = datetime.utcnow()
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
        for i in range(0, len(text), 4000):
            try:
                await safe_call(bot.send_message, cfg.admin_chat_id, text[i:i + 4000])
            except Exception as e:
                log.warning("survey._finish: не удалось отправить в admin_chat_id "
                            "бота %s: %s", cfg.id, e)
        for a in media_answers:
            try:
                await _send_answer_media(bot, cfg.admin_chat_id, a["media_type"],
                                         a["media_file_id"], caption=f"↳ {a['q']}")
            except Exception as e:
                log.warning("survey._finish: не удалось переслать медиа-ответ "
                            "бота %s: %s", cfg.id, e)


async def _send_answer_media(bot: Bot, chat_id: int, media_type: str, file_id: str,
                             caption: str | None = None):
    if media_type == "photo":
        await bot.send_photo(chat_id, file_id, caption=caption)
    elif media_type == "video":
        await bot.send_video(chat_id, file_id, caption=caption)
    elif media_type == "document":
        await bot.send_document(chat_id, file_id, caption=caption)
    elif media_type == "animation":
        await bot.send_animation(chat_id, file_id, caption=caption)
    elif media_type == "audio":
        await bot.send_audio(chat_id, file_id, caption=caption)
    elif media_type == "voice":
        await bot.send_voice(chat_id, file_id, caption=caption)
    elif media_type == "video_note":
        await bot.send_video_note(chat_id, file_id)
    elif media_type == "sticker":
        await bot.send_sticker(chat_id, file_id)


async def _advance_or_finish(bot: Bot, cfg: ChildBot, m: Message, resp_id: int):
    async with Session() as s:
        resp = await s.get(SurveyResponse, resp_id)
    questions = await _questions(resp.survey_id)
    if resp.current_index >= len(questions):
        await _finish(bot, cfg, resp_id)
        await m.answer(cfg.survey_start_text
                       or f"{em('check')} Спасибо! Анкета отправлена.")
        return
    await _ask_current(m, resp, questions)


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
            resp = SurveyResponse(bot_id=bot_db_id, survey_id=survey_id, user_id=user_id)
            s.add(resp)
            await s.commit()
            await s.refresh(resp)
    await _ask_current(m, resp, questions)


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
        await send_with_keyboards(m, welcome, ikb, rkb, photo=cfg.welcome_photo)

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
            await send_response(c.message, b.response_text, b.response_photo)
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
        if await should_apply_antispam(bot_db_id, cfg, m.from_user.id):
            res = await antispam.check(bot_db_id, cfg, m.from_user.id, m.text)
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
                    await send_response(m, b.response_text, b.response_photo)
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

        # Ни одна анкета не начата и это не ответ — подсказываем меню.
        ikb, rkb = await build_keyboards(bot_db_id, cfg)
        if ikb or rkb:
            await m.answer(f"{em('info')} Выберите анкету из меню ниже.",
                           reply_markup=ikb or rkb)

    return r
