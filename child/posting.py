"""
ПАТЧ child/posting.py — исправление анон-предложки.

Замените три блока (incoming, anon_choice_cb, _relay_to_admins) на версии ниже.
Остальной код файла не трогать.
"""

# ---- глобальное хранилище (уже есть в файле, без изменений) ----
# _pending_anon_msgs: dict[int, tuple[list[Message], int]] = {}


# ================= обычные сообщения (подписчики И админы вне /newpost) =================
# ИЗМЕНЕНО: анон-предложка теперь буферизует через buffer_or_process,
# чтобы правильно собирать альбомы перед тем как спросить анон/не анон.
    @r.message(F.chat.type == "private")
    async def incoming(m: Message, bot: Bot, bot_db_id: int, state: FSMContext):
        cfg = await _cfg(bot_db_id)
        async with Session() as s:
            await mod.get_or_create_user(s, bot_db_id, m.from_user)
            s.add(MessageLog(bot_id=bot_db_id, user_id=m.from_user.id, direction="in"))
            await s.commit()
        if await mod.is_banned(bot_db_id, m.from_user.id):
            return
        if await should_apply_antispam(bot_db_id, cfg, m.from_user.id, bot):
            res = await antispam.check(bot_db_id, cfg, m.from_user.id, m.text, bot)
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

        anon_enabled = getattr(cfg, "anon_suggestion_enabled", False)
        if anon_enabled and cfg.accept_suggestions:
            # БАГ ИСПРАВЛЕН: раньше сохранялся только [m] — альбомы терялись.
            # Теперь buffer_or_process собирает все части альбома, и только
            # после этого сохраняем полный список в _pending_anon_msgs.
            async def _ask_anon(msgs: list[Message]):
                _pending_anon_msgs[m.from_user.id] = (msgs, bot_db_id)
                ask_text = getattr(cfg, "anon_suggestion_ask_text", None) or "Как хотите отправить предложку?"
                yes_btn = getattr(cfg, "anon_yes_button_text", None) or "🕵️ Анонимно"
                no_btn = getattr(cfg, "anon_no_button_text", None) or "👤 От моего имени"
                await m.answer(
                    ask_text,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        styled_button(yes_btn, callback_data=f"anon_yes:{m.from_user.id}"),
                        styled_button(no_btn, callback_data=f"anon_no:{m.from_user.id}"),
                    ]])
                )
            await buffer_or_process(m, _ask_anon)
            return

        async def _process(msgs: list[Message]):
            await _relay_to_admins(msgs, bot, cfg, bot_db_id, is_anon=False)

        await buffer_or_process(m, _process)


    # -------- Выбор анонимности --------
    # ИЗМЕНЕНО: убрана повторная буферизация через buffer_or_process —
    # msgs уже собраны на этапе incoming(), передаём их напрямую.
    @r.callback_query(F.data.startswith(("anon_yes:", "anon_no:")))
    async def anon_choice_cb(c: CallbackQuery, bot: Bot):
        is_anon = c.data.startswith("anon_yes:")
        user_id = int(c.data.split(":")[-1])

        if c.from_user.id != user_id:
            await c.answer("Не ваша кнопка", show_alert=True)
            return

        if user_id not in _pending_anon_msgs:
            await c.answer("Сообщение истекло, пришлите ещё раз", show_alert=True)
            return

        msgs, bot_db_id = _pending_anon_msgs.pop(user_id)
        cfg = await _cfg(bot_db_id)

        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await c.answer()

        # БАГ ИСПРАВЛЕН: раньше вызывался buffer_or_process(msgs[0], _process),
        # что пересобирало буфер из одного сообщения и не передавало альбом.
        # Теперь вызываем _relay_to_admins напрямую с уже собранным списком.
        await _relay_to_admins(msgs, bot, cfg, bot_db_id, is_anon=is_anon)


    # _relay_to_admins — без изменений, оставляем как есть в оригинале
    async def _relay_to_admins(msgs: list[Message], bot: Bot, cfg: ChildBot, bot_db_id: int,
                               is_anon: bool = False):
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
                                origin_message_ids=origin_ids,
                                is_anonymous=is_anon)
                s.add(sg)
                await s.commit()
                await s.refresh(sg)
            sugg_kb = InlineKeyboardMarkup(inline_keyboard=[[
                styled_button("✅ Принять", callback_data=f"sg_ok:{sg.id}"),
                styled_button("❌ Отклонить", callback_data=f"sg_no:{sg.id}")]])
        await relay_to_admin_chat(msgs, bot, cfg, extra_kb=sugg_kb,
                                  force_anonymous=is_anon)
