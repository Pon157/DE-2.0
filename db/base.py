# db/base.py
import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import DATABASE_URL

log = logging.getLogger("db")


class Base(DeclarativeBase):
    pass


engine = create_async_engine(DATABASE_URL, pool_size=20, max_overflow=30)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _exec(stmt: str):
    """Выполняет один DDL-стейтмент в СВОЕЙ ОТДЕЛЬНОЙ транзакции/соединении.

    ВАЖНО: раньше все ALTER-ы шли в одной общей транзакции
    (`async with engine.begin() as conn`). В PostgreSQL, если ЛЮБОЙ стейтмент
    внутри транзакции падает с ошибкой, вся транзакция помечается как
    "aborted" — и ВСЕ последующие команды в ней тоже начинают падать (с
    "current transaction is aborted"), даже если каждая обёрнута в свой
    try/except на стороне Python! Из-за этого один неудачный ALTER мог молча
    "выключить" вообще все остальные миграции, включая поиск и снятие
    NOT NULL со старых колонок — без единой ошибки в логах. Теперь каждый
    стейтмент — это отдельная транзакция: сбой одного никак не влияет на
    остальные.
    """
    from sqlalchemy import text
    try:
        async with engine.begin() as conn:
            await conn.execute(text(stmt))
        return True
    except Exception as e:
        log.debug("Migration statement skipped/failed (%s): %s", stmt[:80], e)
        return False


async def _drop_stray_not_null_constraints():
    """Самовосстанавливающаяся защита от "застрявших" колонок.

    За время разработки колонки в ChildBot несколько раз переименовывались
    (open_ticket_button_text -> ticket_button_text -> restart_creates_new_ticket
    -> always_new_ticket и т.д.). SQLAlchemy's create_all НЕ переименовывает и
    НЕ удаляет старые колонки в уже существующей таблице — они остаются
    висеть в реальной БД как есть. Если такая колонка когда-то была создана
    как NOT NULL без дефолта, а текущая модель её больше не знает и не
    заполняет — любой INSERT падает с NotNullViolationError, и бот вообще не
    создаётся (что и ломает автозапуск: панель пуста, потому что строка в
    child_bots ни разу не была успешно сохранена).

    Сравниваем реальные колонки таблиц в БД с колонками, которые ожидает
    текущая модель, и для любых "лишних" NOT NULL колонок без дефолта
    автоматически снимаем ограничение.
    """
    from sqlalchemy import inspect as sa_inspect

    def _sync_check(sync_conn):
        insp = sa_inspect(sync_conn)
        result = []
        for table in Base.metadata.tables.values():
            if table.name not in insp.get_table_names():
                continue
            expected = set(table.columns.keys())
            try:
                existing_cols = insp.get_columns(table.name)
            except Exception:
                continue
            for col in existing_cols:
                if col["name"] in expected:
                    continue
                if col["nullable"]:
                    continue
                result.append((table.name, col["name"]))
        return result

    async with engine.begin() as conn:
        stray = await conn.run_sync(_sync_check)

    for table_name, col_name in stray:
        ok = await _exec(f'ALTER TABLE "{table_name}" ALTER COLUMN "{col_name}" DROP NOT NULL')
        if ok:
            log.warning("Сняли NOT NULL со старой неиспользуемой колонки %s.%s",
                       table_name, col_name)


async def init_db():
    from db import models  # noqa — регистрирует все модели в Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Лёгкая авто-миграция для существующих БД: create_all не добавляет новые
    # колонки к уже существующим таблицам. Каждый стейтмент — своя транзакция
    # (см. _exec) чтобы один сбой не глушил все остальные молча.
    for stmt in (
        "ALTER TABLE bot_buttons ADD COLUMN IF NOT EXISTS style VARCHAR(16)",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS buttons_json TEXT",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS ticket_button_text VARCHAR(64) DEFAULT '✉️ Открыть обращение'",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS ticket_button_style VARCHAR(16)",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS ticket_button_icon VARCHAR(32)",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS always_new_ticket BOOLEAN DEFAULT false",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS template_buttons_json TEXT",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS channel_delivery_mode VARCHAR(10) DEFAULT 'template'",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS media_group_json TEXT",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS origin_chat_id BIGINT",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS origin_message_id BIGINT",
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS origin_message_ids TEXT",
        "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS media_group_json TEXT",
        "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS origin_chat_id BIGINT",
        "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS origin_message_id BIGINT",
        "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS origin_message_ids TEXT",
        # Режим шапки (отдельно/слитно/выкл) и настраиваемое имя топика
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS header_mode VARCHAR(16) DEFAULT 'separate'",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS topic_name_template TEXT DEFAULT '✉️ {name} · {id}'",
        # id исходного сообщения юзера — reply-контекст и зеркалирование реакций
        "ALTER TABLE msg_map ADD COLUMN IF NOT EXISTS user_chat_msg_id BIGINT",
        # Источник кнопок поста (шаблон/свои/оба/без)
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS buttons_mode VARCHAR(16) DEFAULT 'both'",
        # Гарантированные точечные фиксы для УЖЕ ИЗВЕСТНЫХ застрявших колонок
        # (в дополнение к универсальному поиску ниже — на случай, если он по
        # какой-то причине не сработает на конкретной инсталляции).
        'ALTER TABLE child_bots ALTER COLUMN restart_creates_new_ticket DROP NOT NULL',
        'ALTER TABLE child_bots ALTER COLUMN open_ticket_button_text DROP NOT NULL',
        # ---- антиспам (rate-limit / капча / прогрессирующие тайм-ауты) ----
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS req_window_start TIMESTAMP",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS req_window_count INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS total_requests INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS captcha_pending BOOLEAN DEFAULT false",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS captcha_answer VARCHAR(8)",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS captcha_asked_at TIMESTAMP",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS spam_strikes INTEGER DEFAULT 0",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS throttled_until TIMESTAMP",
        # ---- настраиваемые в конструкторе пороги антиспама на бота ----
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS antispam_enabled BOOLEAN DEFAULT true",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS rate_limit_max INTEGER DEFAULT 6",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS rate_limit_window INTEGER DEFAULT 10",
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS captcha_every INTEGER DEFAULT 20",
        # владелец бота проверяется антиспамом или нет (тоггл для теста)
        "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS antispam_ignore_owner BOOLEAN DEFAULT true",
        # бан пользователя в САМОМ КОНСТРУКТОРЕ (master-боте)
        "ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT false",
        "ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS ban_reason TEXT",
        "ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS banned_at TIMESTAMP",
    ):
        await _exec(stmt)

    # Универсальная защита от ЛЮБЫХ "застрявших" NOT NULL колонок от прошлых
    # переименований (см. подробности в докстринге функции).
    await _drop_stray_not_null_constraints()
