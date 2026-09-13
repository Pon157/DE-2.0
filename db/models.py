# db/models.py
import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, String as SAString

from db.base import Base
from utils.crypto import decrypt_token, encrypt_token


class EncryptedToken(TypeDecorator):
    """Прозрачно шифрует токен бота при записи и расшифровывает при чтении."""

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
    first_message = "first_message"
    start_command = "start_command"
    button = "button"


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

    # Настройки feedback
    open_mode: Mapped[OpenMode] = mapped_column(
        Enum(OpenMode), default=OpenMode.first_message
    )
    forward_mode: Mapped[ForwardMode] = mapped_column(
        Enum(ForwardMode), default=ForwardMode.forward
    )
    copy_header: Mapped[str] = mapped_column(
        Text, default="{name} | @{username} | <code>{id}</code> · {anon_id}"
    )
    admin_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    use_topics: Mapped[bool] = mapped_column(Boolean, default=False)
    header_mode: Mapped[str] = mapped_column(String(16), default="separate")
    topic_name_template: Mapped[str] = mapped_column(
        Text, default="✉️ {name} · {id}"
    )
    topic_icon_emoji_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    topic_color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pin_first_message: Mapped[bool] = mapped_column(Boolean, default=False)

    # Приветственное сообщение
    welcome_text: Mapped[str] = mapped_column(
        Text, default="Привет! Напишите ваше сообщение."
    )
    welcome_photo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    welcome_effect_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    rich_welcome: Mapped[bool] = mapped_column(Boolean, default=False)

    # Общие настройки кнопок и обращений
    warn_limit: Mapped[int] = mapped_column(Integer, default=3)
    ticket_button_text: Mapped[str] = mapped_column(
        String(64), default="✉️ Открыть обращение"
    )
    ticket_button_style: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    ticket_button_icon: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    close_ticket_button_text: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default="❌ Закрыть обращение"
    )
    close_ticket_button_style: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    close_ticket_button_icon: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    close_notify_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default="🔒 Обращение закрыто администрацией. Ваше новое сообщение откроет новое обращение.",
    )
    admin_close_button_text: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    admin_close_button_style: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    admin_close_button_icon: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    always_new_ticket: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_reply_reaction: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    # Донаты
    donate_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    donate_button_type: Mapped[str] = mapped_column(
        String(10), default="inline"
    )
    donate_button_text: Mapped[str] = mapped_column(
        String(64), default="⭐️ Донат"
    )
    donate_button_style: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    donate_button_icon: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    donate_stars_min: Mapped[int] = mapped_column(Integer, default=1)
    donate_stars_max: Mapped[int] = mapped_column(Integer, default=10000)
    donate_subscription_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    donate_subscription_stars: Mapped[int] = mapped_column(
        Integer, default=50
    )

    # Анкеты
    survey_start_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    survey_dialog_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    survey_finish_media_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    survey_finish_media_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    # Настройки posting
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    accept_suggestions: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    post_template: Mapped[str] = mapped_column(Text, default="{text}")
    template_buttons_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    channel_delivery_mode: Mapped[str] = mapped_column(
        String(10), default="template"
    )
    channel_publish_mode: Mapped[str] = mapped_column(
        String(10), default="copy"
    )

    # Антиспам
    antispam_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    rate_limit_max: Mapped[int] = mapped_column(Integer, default=6)
    rate_limit_window: Mapped[int] = mapped_column(Integer, default=10)
    captcha_every: Mapped[int] = mapped_column(Integer, default=20)
    antispam_ignore_owner: Mapped[bool] = mapped_column(
        Boolean, default=True
    )


class BotAdmin(Base):
    __tablename__ = "bot_admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)


class BotButton(Base):
    __tablename__ = "bot_buttons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_photo: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    icon_emoji_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    survey_id: Mapped[int | None] = mapped_column(
        ForeignKey("surveys.id", ondelete="CASCADE"),
        nullable=True,
    )
    disown_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )


class BotUser(Base):
    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    full_name: Mapped[str] = mapped_column(String(256), default="")

    # Поля из обеих версий модели
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    first_seen: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    last_active: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    is_blocked_bot: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    ban_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    warns: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)

    # Антиспам и статистика
    req_window_start: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    req_window_count: Mapped[int] = mapped_column(Integer, default=0)
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    captcha_pending: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    captcha_answer: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    captcha_asked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    spam_strikes: Mapped[int] = mapped_column(Integer, default=0)
    throttled_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    incoming_msg_count: Mapped[int] = mapped_column(
        Integer, default=0
    )

    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    topic_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    subject: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, server_default=func.now()
    )


class MessageLog(Base):
    # Оставлено имя таблицы из старой версии.
    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(String(8))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class MsgMap(Base):
    __tablename__ = "msg_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    admin_chat_msg_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_chat_msg_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"),
        nullable=True,
    )


class Suggestion(Base):
    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger)
    html_text: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    media_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    media_group_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    origin_chat_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    origin_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    origin_message_ids: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )
    decided_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    decided_by_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    author_id: Mapped[int] = mapped_column(BigInteger)
    html_text: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    media_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    media_group_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    origin_chat_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    origin_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    origin_message_ids: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    buttons_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    buttons_mode: Mapped[str] = mapped_column(
        String(16), default="both"
    )
    publish_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class Donation(Base):
    __tablename__ = "donations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    stars: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    is_subscription: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    subscription_state: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    subscription_expiration: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )


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
    __tablename__ = "advertisements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_bot_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    kind: Mapped[AdKind] = mapped_column(
        Enum(AdKind), default=AdKind.impressions
    )
    text: Mapped[str] = mapped_column(String(100))
    media_file_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    media_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    target_impressions: Mapped[int] = mapped_column(
        Integer, default=0
    )
    shown_count: Mapped[int] = mapped_column(Integer, default=0)
    price_rub: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[AdStatus] = mapped_column(
        Enum(AdStatus), default=AdStatus.pending
    )
    reject_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    payment_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    extends_ad_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )


class AdCooldown(Base):
    __tablename__ = "ad_cooldowns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    last_broadcast_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class ModerationLog(Base):
    __tablename__ = "moderation_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    admin_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    action: Mapped[str] = mapped_column(String(16))
    target_user_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class PlatformUser(Base):
    __tablename__ = "platform_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    referred_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    referral_count: Mapped[int] = mapped_column(Integer, default=0)
    pro_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    banned_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    captcha_pending: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    captcha_answer: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    captcha_asked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    accepted_terms: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    accepted_terms_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )


class ReferralEvent(Base):
    __tablename__ = "referral_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    invitee_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class BotRuntimeLock(Base):
    __tablename__ = "bot_runtime_locks"

    bot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder: Mapped[str] = mapped_column(String(36))
    last_seen: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class Survey(Base):
    __tablename__ = "surveys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class SurveyQuestion(Base):
    __tablename__ = "survey_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    survey_id: Mapped[int] = mapped_column(
        ForeignKey("surveys.id", ondelete="CASCADE"),
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    qtype: Mapped[str] = mapped_column(
        String(16), default="text"
    )
    options_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    media_file_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    media_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )


class SurveyResponse(Base):
    __tablename__ = "survey_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    survey_id: Mapped[int] = mapped_column(
        ForeignKey("surveys.id", ondelete="CASCADE"),
        index=True,
    )
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    answers_json: Mapped[str] = mapped_column(Text, default="[]")
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    topic_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )


class AutoReplyKind(str, enum.Enum):
    first_message = "first_message"
    every_n = "every_n"
    keyword = "keyword"


class AutoReply(Base):
    __tablename__ = "auto_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[AutoReplyKind] = mapped_column(Enum(AutoReplyKind))
    param: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    photo: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


# Сценарии (flow_api / scenario_runner)
class ScenarioTrigger(str, enum.Enum):
    command = "command"
    button = "button"
    keyword = "keyword"
    start = "start"


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("child_bots.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(128), default="Новый сценарий"
    )
    trigger_type: Mapped[ScenarioTrigger] = mapped_column(
        Enum(ScenarioTrigger), default=ScenarioTrigger.command
    )
    trigger_value: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class ScenarioNode(Base):
    __tablename__ = "scenario_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        index=True,
    )
    node_type: Mapped[str] = mapped_column(String(32))
    config: Mapped[str] = mapped_column(Text, default="{}")
    label: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    pos_x: Mapped[float | None] = mapped_column(nullable=True)
    pos_y: Mapped[float | None] = mapped_column(nullable=True)


class ScenarioEdge(Base):
    __tablename__ = "scenario_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        index=True,
    )
    from_node_id: Mapped[int] = mapped_column(
        ForeignKey("scenario_nodes.id", ondelete="CASCADE"),
        index=True,
    )
    to_node_id: Mapped[int] = mapped_column(
        ForeignKey("scenario_nodes.id", ondelete="CASCADE")
    )
    label: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )


class ScenarioSession(Base):
    __tablename__ = "scenario_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    scenario_id: Mapped[int] = mapped_column(Integer, index=True)
    current_node_id: Mapped[int] = mapped_column(Integer)
    variables_json: Mapped[str] = mapped_column(Text, default="{}")
    waiting_input: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    input_variable: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    last_step_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)
