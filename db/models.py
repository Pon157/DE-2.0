# db/models.py
import enum
from datetime import datetime
from sqlalchemy import (BigInteger, Boolean, DateTime, Enum, ForeignKey,
                        Integer, String, Text, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column
from db.base import Base


class BotType(str, enum.Enum):
    feedback = "feedback"
    posting = "posting"


class OpenMode(str, enum.Enum):
    first_message = "first_message"   # обращение при первом сообщении
    start_command = "start_command"   # при /start (потом /restart)
    button = "button"                 # по кнопке


class ForwardMode(str, enum.Enum):
    forward = "forward"
    copy = "copy"


class ChildBot(Base):
    __tablename__ = "child_bots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)
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
    # Как шапка попадает в админ-чат: отдельным сообщением / слитно с сообщением юзера / выкл
    header_mode: Mapped[str] = mapped_column(String(16), default="separate")  # separate|merge|off
    # Шаблон имени топика. Переменные: {name} {username} {id} {anon_id}
    topic_name_template: Mapped[str] = mapped_column(Text, default="✉️ {name} · {id}")
    welcome_text: Mapped[str] = mapped_column(Text, default="Привет! Напишите ваше сообщение.")
    welcome_photo: Mapped[str | None] = mapped_column(String(256), nullable=True)  # file_id
    warn_limit: Mapped[int] = mapped_column(Integer, default=3)
    donate_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    donate_button_type: Mapped[str] = mapped_column(String(10), default="inline")  # inline|keyboard
    donate_button_text: Mapped[str] = mapped_column(String(64), default="⭐️ Донат")
    ticket_button_text: Mapped[str] = mapped_column(String(64), default="✉️ Открыть обращение")
    ticket_button_style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ticket_button_icon: Mapped[str | None] = mapped_column(String(32), nullable=True)
    always_new_ticket: Mapped[bool] = mapped_column(Boolean, default=False)  # /start или restart-кнопка -> новый тикет/топик

    # ---- настройки posting ----
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    accept_suggestions: Mapped[bool] = mapped_column(Boolean, default=True)
    post_template: Mapped[str] = mapped_column(Text, default="{text}")
    template_buttons_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # кнопки на КАЖДЫЙ пост
    channel_delivery_mode: Mapped[str] = mapped_column(String(10), default="template")  # template|copy
    # Отдельный переключатель: КАК именно контент уходит в канал при
    # подтверждённой публикации — "copy" (bot.copy_message/copy_messages,
    # без пометки "Forwarded from") или "forward" (bot.forward_message/
    # forward_messages, с пометкой). Раньше это было жёстко зашито на copy.
    channel_publish_mode: Mapped[str] = mapped_column(String(10), default="copy")  # copy|forward

    # ---- антиспам (services/antispam.py) ----
    antispam_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit_max: Mapped[int] = mapped_column(Integer, default=6)      # сколько сообщений разрешено...
    rate_limit_window: Mapped[int] = mapped_column(Integer, default=10)  # ...за столько секунд
    captcha_every: Mapped[int] = mapped_column(Integer, default=20)      # капча каждые N запросов
    # Раньше владелец/админы бота были ВСЕГДА исключены из антиспама — из-за
    # этого при тестировании казалось, что антиспам "не работает". Теперь это
    # переключаемо: True (по умолчанию) — старое поведение, антиспам владельца
    # не трогает; False — антиспам проверяет и владельца тоже (удобно для теста).
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
    style: Mapped[str | None] = mapped_column(String(16), nullable=True)  # primary|secondary|success|danger (только inline)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)    # HTML ответ триггера
    response_photo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class BotUser(Base):
    __tablename__ = "bot_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_active: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_blocked_bot: Mapped[bool] = mapped_column(Boolean, default=False)   # юзер заблокировал бота
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    warns: Mapped[int] = mapped_column(Integer, default=0)

    # ---- антиспам (services/antispam.py) ----
    req_window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    req_window_count: Mapped[int] = mapped_column(Integer, default=0)   # запросов в текущем "окне" (rate-limit)
    total_requests: Mapped[int] = mapped_column(Integer, default=0)     # для капчи "каждые 20 запросов"
    captcha_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    captcha_answer: Mapped[str | None] = mapped_column(String(8), nullable=True)
    captcha_asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    spam_strikes: Mapped[int] = mapped_column(Integer, default=0)       # счётчик для прогрессирующего таймаута
    throttled_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # временный "тихий" бан за спам

    __table_args__ = (UniqueConstraint("bot_id", "user_id"),)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # форум-топик
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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
    # id исходного сообщения в ЛС юзера (None для служебных сообщений — шапок и т.п.)
    user_chat_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Suggestion(Base):
    __tablename__ = "suggestions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    html_text: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_group_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # альбом: [{file_id,type}]
    origin_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # альбом: "1,2,3"
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected
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
    media_group_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # альбом: [{file_id,type}]
    origin_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # альбом: "1,2,3"
    buttons_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # [[{text,url,style,icon}]]
    # Источник кнопок для поста: кнопки шаблона / свои / оба / без кнопок
    buttons_mode: Mapped[str] = mapped_column(String(16), default="both")  # both|template|custom|none
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


class AdStatus(str, enum.Enum):
    pending = "pending"        # ждёт решения супер-админа
    rejected = "rejected"      # отклонено супер-админом
    awaiting_payment = "awaiting_payment"  # одобрено, ждём оплату
    active = "active"          # оплачено, показывается
    finished = "finished"      # показы закончились / рассылка выполнена


class AdKind(str, enum.Enum):
    impressions = "impressions"   # показ в стартовых сообщениях N раз
    broadcast = "broadcast"       # разовая рассылка во все боты сразу


class Advertisement(Base):
    """Рекламная кампания (см. /ads в дочерних ботах)."""
    __tablename__ = "advertisements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_bot_id: Mapped[int] = mapped_column(Integer)      # в каком боте создана заявка
    kind: Mapped[AdKind] = mapped_column(Enum(AdKind), default=AdKind.impressions)
    text: Mapped[str] = mapped_column(String(100))
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_impressions: Mapped[int] = mapped_column(Integer, default=0)  # для kind=impressions
    shown_count: Mapped[int] = mapped_column(Integer, default=0)
    price_rub: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[AdStatus] = mapped_column(Enum(AdStatus), default=AdStatus.pending)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # id платежа YooKassa
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    # ---- бан в САМОМ КОНСТРУКТОРЕ (master-боте), а не в дочерних ботах ----
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ---- капча в САМОМ КОНСТРУКТОРЕ (master-боте) — показывается ВСЕМ,
    # включая владельца/SUPER_ADMIN_ID, каждые CAPTCHA_EVERY запросов ----
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    captcha_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    captcha_answer: Mapped[str | None] = mapped_column(String(8), nullable=True)
    captcha_asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReferralEvent(Base):
    """Фиксирует факт 'этого юзера привёл этот реферер' (защита от повторного счёта)."""
    __tablename__ = "referral_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    invitee_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BotRuntimeLock(Base):
    """Распределённый лок 'кто сейчас держит getUpdates для этого бота'.

    Нужен, если приложение вдруг оказывается запущено более чем в одном
    процессе/контейнере на одну и ту же БД (например, старый контейнер не
    успел завершиться при деплое) — иначе Telegram видит два конкурентных
    getUpdates и отдаёт TelegramConflictError.
    """
    __tablename__ = "bot_runtime_locks"
    bot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder: Mapped[str] = mapped_column(String(36))       # uuid процесса
    last_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
