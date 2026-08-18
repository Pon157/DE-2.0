
# db/models.py
import enum
from datetime import datetime
from sqlalchemy import (BigInteger, Boolean, DateTime, Enum, ForeignKey,
                        Integer, String, Text, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, String as SAString
from db.base import Base
from utils.crypto import encrypt_token, decrypt_token


class EncryptedToken(TypeDecorator):
    """Прозрачно шифрует токен бота на запись и расшифровывает на чтение —
    весь остальной код (Bot(cb.token), run_broadcast(cb.token, ...) и т.п.)
    продолжает работать как раньше, ничего не зная о шифровании. ВАЖНО:
    так как Fernet НЕ детерминирован, эту колонку нельзя использовать в
    `WHERE token = :значение` — для поиска/уникальности есть отдельная
    колонка token_fingerprint (см. ChildBot ниже)."""
    impl = SAString(512)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_token(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt_token(value)


class BotType(str, enum.Enum):
    feedback = "feedback"
    posting = "posting"
    survey = "survey"


class OpenMode(str, enum.Enum):
    first_message = "first_message"   # обращение при первом сообщении
    start_command = "start_command"   # при /start (потом /restart)
    button = "button"                  # по кнопке


class ForwardMode(str, enum.Enum):
    forward = "forward"
    copy = "copy"


class ChildBot(Base):
    __tablename__ = "child_bots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    token: Mapped[str] = mapped_column(EncryptedToken)
    token_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    bot_tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str] = mapped_column(String(64))
    bot_type: Mapped[BotType] = mapped_column(Enum(BotType))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # ---- настройки feedback ----
    open_mode: Mapped[OpenMode] = mapped_column(Enum(OpenMode), default=OpenMode.first_message)
    forward_mode: Mapped[ForwardMode] = mapped_column(Enum(ForwardMode), default=ForwardMode.forward)
    copy_header: Mapped[str] = mapped_column(Text, default="{name} | @{username} | <code>{id}</code> · {anon_id}")
    admin_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # куда слать
    use_topics: Mapped[bool] = mapped_column(Boolean, default=False)
    header_mode: Mapped[str] = mapped_column(String(16), default="separate")  # separate|merge|off
    topic_name_template: Mapped[str] = mapped_column(Text, default="✉️ {name} · {id}")
    # НОВОЕ: иконка топика — premium-эмодзи (create_forum_topic поддерживает
    # icon_custom_emoji_id, Bot API 9.4+) — раньше топики создавались вообще
    # без иконки, хотя выбор цвета/иконки у обычных кнопок уже был.
    topic_icon_emoji_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    welcome_text: Mapped[str] = mapped_column(Text, default="Привет! Напишите ваше сообщение.")
    welcome_photo: Mapped[str | None] = mapped_column(String(256), nullable=True)  # file_id
    # НОВОЕ: эффект на приветственном сообщении (message_effect_id, Bot API,
    # доступно только в личных чатах — как раз наш случай) — 🔥/❤️/🎉 и т.п.
    welcome_effect_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # НОВОЕ: рич-текст приветствия (send_rich_message / InputRichMessage,
    # Bot API 10.1, июнь 2026) — Pro-функция, см. referrals.is_pro.
    rich_welcome: Mapped[bool] = mapped_column(Boolean, default=False)
    warn_limit: Mapped[int] = mapped_column(Integer, default=3)
    donate_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    donate_button_type: Mapped[str] = mapped_column(String(10), default="inline")  # inline|keyboard
    donate_button_text: Mapped[str] = mapped_column(String(64), default="⭐️ Донат")
    # НОВОЕ (Bot API 9.4): цвет/premium-эмодзи для кнопки доната — работает
    # и для inline-, и для kayboard-варианта (donate_button_type).
    donate_button_style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    donate_button_icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ticket_button_text: Mapped[str] = mapped_column(String(64), default="✉️ Открыть обращение")
    ticket_button_style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ticket_button_icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # НОВОЕ (по запросу): NULL/пусто = кнопка "Закрыть обращение" у
    # пользователя вообще не показывается (раньше её нельзя было убрать —
    # только переименовать). Раньше поле было NOT NULL с дефолтом.
    close_ticket_button_text: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default="❌ Закрыть обращение")
    # НОВОЕ (по запросу): цвет/premium-эмодзи именно КНОПКИ "Закрыть
    # обращение" (не путать с close_notify_text — это текст, который
    # приходит пользователю ПОСЛЕ закрытия админом, разные вещи).
    close_ticket_button_style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    close_ticket_button_icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    close_notify_text: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        default="🔒 Обращение закрыто администрацией. Ваше новое сообщение откроет новое обращение.")
    always_new_ticket: Mapped[bool] = mapped_column(Boolean, default=False)
    pin_first_message: Mapped[bool] = mapped_column(Boolean, default=False)
    survey_start_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # НОВОЕ (фикс бага п.2 из запроса): текст благодарности после анкеты
    # раньше сохранялся только если m.text был непустым — сообщение с
    # фото/видео без текста отклонялось ("Нужен текст"), медиа к финальному
    # сообщению приложить было вообще нельзя. survey_start_text уже хранит
    # HTML (m.html_text, формат/premium-эмодзи и так сохранялись) —
    # не хватало именно медиа-вложения.
    survey_finish_media_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    survey_finish_media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # НОВОЕ (по запросу): "режим диалога" для ботов-анкет — если выключен
    # (по умолчанию), ни ответ админа на присланную анкету, ни последующее
    # сообщение респондента НЕ доставляются друг другу (см.
    # child/common.py::admin_reply и child/survey.py::user_message) — анкета
    # остаётся чисто "прислал и всё", без переписки.
    survey_dialog_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Реакция (эмодзи), которую бот ставит на сообщение админа при ответе
    admin_reply_reaction: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ---- настройки posting ----
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    accept_suggestions: Mapped[bool] = mapped_column(Boolean, default=True)
    post_template: Mapped[str] = mapped_column(Text, default="{text}")
    template_buttons_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # кнопки на КАЖДЫЙ пост
    channel_delivery_mode: Mapped[str] = mapped_column(String(10), default="template")  # template|copy
    channel_publish_mode: Mapped[str] = mapped_column(String(10), default="copy")  # copy|forward

    # ---- антиспам (services/antispam.py) ----
    antispam_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit_max: Mapped[int] = mapped_column(Integer, default=6)
    rate_limit_window: Mapped[int] = mapped_column(Integer, default=10)
    captcha_every: Mapped[int] = mapped_column(Integer, default=20)
    antispam_ignore_owner: Mapped[bool] = mapped_column(Boolean, default=True)


class BotAdmin(Base):
    __tablename__ = "bot_admins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)


class BotButton(Base):
    """Инлайн / кейборд кнопки + триггер-команды."""
    __tablename__ = "bot_buttons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))        # inline_url | inline_trigger | keyboard | command
    text: Mapped[str] = mapped_column(String(128))       # надпись на кнопке / имя команды
    icon_emoji_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    style: Mapped[str | None] = mapped_column(String(16), nullable=True)  # primary|secondary|success|danger
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)    # HTML ответ триггера
    response_photo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    survey_id: Mapped[int | None] = mapped_column(
        ForeignKey("surveys.id", ondelete="CASCADE"), nullable=True)


class BotUser(Base):
    __tablename__ = "bot_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_active: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_blocked_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    warns: Mapped[int] = mapped_column(Integer, default=0)

    # ---- антиспам (services/antispam.py) ----
    req_window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    req_window_count: Mapped[int] = mapped_column(Integer, default=0)
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    captcha_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    captcha_answer: Mapped[str | None] = mapped_column(String(8), nullable=True)
    captcha_asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    spam_strikes: Mapped[int] = mapped_column(Integer, default=0)
    throttled_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_active_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MessageLog(Base):
    __tablename__ = "message_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(String(8))    # in | out
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class MsgMap(Base):
    """Связь: сообщение в админ-чате ↔ юзер (для ответов reply) + связь с
    исходным сообщением юзера (для reply-контекста и зеркалирования реакций)."""
    __tablename__ = "msg_map"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    admin_chat_msg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_chat_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True)


class Suggestion(Base):
    __tablename__ = "suggestions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    html_text: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_group_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_by_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    html_text: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_group_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    buttons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    buttons_mode: Mapped[str] = mapped_column(String(16), default="both")
    publish_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Donation(Base):
    __tablename__ = "donations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    stars: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # НОВОЕ: ежемесячная Stars-подписка (createInvoiceLink/send_invoice с
    # subscription_period, Bot API) — отличаем от разового доната.
    is_subscription: Mapped[bool] = mapped_column(Boolean, default=False)
    # НОВОЕ (докрутка продления/отмены подписки): состояние подписки и
    # реквизиты, нужные для editUserStarSubscription (владелец бота может
    # отменить подписку пользователя вручную) — заполняются из
    # successful_payment/Update.subscription (см. child/common.py).
    subscription_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # active / canceled / failed / expired
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subscription_expiration: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AdStatus(str, enum.Enum):
    pending = "pending"
    rejected = "rejected"
    awaiting_payment = "awaiting_payment"
    active = "active"
    finished = "finished"


class AdKind(str, enum.Enum):
    impressions = "impressions"
    broadcast = "broadcast"


class Advertisement(Base):
    """Рекламная кампания (см. /ads в дочерних ботах)."""
    __tablename__ = "advertisements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_bot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[AdKind] = mapped_column(Enum(AdKind), default=AdKind.impressions)
    text: Mapped[str] = mapped_column(String(100))
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_impressions: Mapped[int] = mapped_column(Integer, default=0)
    shown_count: Mapped[int] = mapped_column(Integer, default=0)
    price_rub: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[AdStatus] = mapped_column(Enum(AdStatus), default=AdStatus.pending)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extends_ad_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AdCooldown(Base):
    """Ограничение 'разослать во все боты' — не чаще раза в 5 дней на покупателя."""
    __tablename__ = "ad_cooldowns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    last_broadcast_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ModerationLog(Base):
    """Кто из админов банил/варнил кого — для блока статистики по админам."""
    __tablename__ = "moderation_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    admin_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(16))   # ban|unban|warn|unwarn
    target_user_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class PlatformUser(Base):
    """Пользователь ПЛАТФОРМЫ (не бота) — для рефералки и Pro-подписки.
    id — это Telegram user_id владельца/пользователя master-бота."""
    __tablename__ = "platform_users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referral_count: Mapped[int] = mapped_column(Integer, default=0)
    pro_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    captcha_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    captcha_answer: Mapped[str | None] = mapped_column(String(8), nullable=True)
    captcha_asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_terms: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_terms_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReferralEvent(Base):
    """Фиксирует факт 'этого юзера привёл этот реферер' (защита от повторного счёта)."""
    __tablename__ = "referral_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    invitee_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BotRuntimeLock(Base):
    """Распределённый лок 'кто сейчас держит getUpdates для этого бота'."""
    __tablename__ = "bot_runtime_locks"
    bot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder: Mapped[str] = mapped_column(String(36))       # uuid процесса
    last_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Survey(Base):
    __tablename__ = "surveys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SurveyQuestion(Base):
    __tablename__ = "survey_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    qtype: Mapped[str] = mapped_column(String(16), default="text")
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # НОВОЕ (фикс бага п.1 из запроса): текст вопроса раньше сохранялся как
    # m.text (голый plain text) — форматирование, стили и premium-эмодзи
    # (<tg-emoji>, Bot API 9.4) молча терялись. Теперь текст хранится как
    # HTML (m.html_text), плюс вопрос может нести медиа-вложение (фото/видео/
    # гиф/документ/аудио) — раньше при попытке прислать в конструкторе фото
    # вместо текста вопрос вообще не сохранялся ("Нужен текст").
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)


class SurveyResponse(Base):
    """Одно прохождение анкеты одним пользователем."""
    __tablename__ = "survey_responses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id", ondelete="CASCADE"), index=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    answers_json: Mapped[str] = mapped_column(Text, default="[]")
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # НОВОЕ (фикс бага "в анкетах по топикам анкеты не отправляются"):
    # боты-анкеты никогда не создавали форум-топик, даже когда владелец
    # включал тумблер "Топики" в настройках — заполненная анкета уходила
    # send_message'ом без message_thread_id. Если чат админов — форум с
    # закрытым/скрытым General-топиком (обычная настройка при работе через
    # топики), Telegram отклонял такую отправку и анкета терялась молча.
    # Теперь на каждое прохождение анкеты (или переиспользование) заводится
    # свой топик, как это уже работает в боте-обращений (см.
    # child/common.py::open_ticket).
    topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

