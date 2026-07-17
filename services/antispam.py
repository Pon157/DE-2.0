"""Антиспам для дочерних ботов.

Три независимых уровня защиты:

1. Rate-limit — "не больше X сообщений за Y секунд" (настраивается в
   конструкторе, ChildBot.rate_limit_max / rate_limit_window).
2. Капча — каждые N запросов (ChildBot.captcha_every, по умолчанию 20)
   пользователь должен решить простой пример, иначе бот его игнорирует.
3. Прогрессирующий тайм-аут — если пользователь продолжает превышать
   rate-limit после того как его уже осаживали, каждое новое нарушение
   увеличивает срок "заморозки": 5 минут -> 10 минут -> 20 минут -> ...
   (удваивается на каждый новый "страйк", сбрасывается после суток без
   нарушений).

Всё хранится в BotUser (см. db/models.py), поэтому переживает рестарт бота
и работает одинаково для всех дочерних ботов на инстансе.
"""
import random
from datetime import datetime, timedelta
from sqlalchemy import select
from db.base import Session
from db.models import BotUser, ChildBot
from utils.emoji import em

FIRST_THROTTLE_MINUTES = 5          # первый тайм-аут за флуд
STRIKE_RESET_AFTER_HOURS = 24       # если сутки не нарушал — счётчик обнуляется
CAPTCHA_TIMEOUT_MINUTES = 3         # сколько времени даётся на решение капчи


def _throttle_minutes(strikes: int) -> int:
    """5 -> 10 -> 20 -> 40 ... (прогрессия, удвоение на каждый страйк)."""
    return FIRST_THROTTLE_MINUTES * (2 ** max(strikes - 1, 0))


def _make_captcha() -> tuple[str, str]:
    a, b = random.randint(2, 9), random.randint(2, 9)
    op = random.choice(["+", "-"])
    if op == "-" and a < b:
        a, b = b, a
    answer = a + b if op == "+" else a - b
    return f"{a} {op} {b} = ?", str(answer)


class AntispamResult:
    """Итог проверки: allowed=False значит сообщение уже обработано
    (пользователю отправлен ответ) и дальше по цепочке идти не нужно."""
    def __init__(self, allowed: bool, notice: str | None = None):
        self.allowed = allowed
        self.notice = notice


async def check(bot_db_id: int, cfg: ChildBot, user_id: int, text: str | None) -> AntispamResult:
    if not getattr(cfg, "antispam_enabled", True):
        return AntispamResult(True)

    now = datetime.utcnow()
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_db_id, BotUser.user_id == user_id))
        if not u:
            # пользователь ещё не создан в этой таблице — антиспам применится
            # начиная со следующего сообщения (get_or_create_user создаёт его
            # раньше по цепочке вызовов в incoming()).
            return AntispamResult(True)

        # --- 0. уже "заморожен" за флуд ---
        if u.throttled_until and u.throttled_until > now:
            return AntispamResult(False)  # молча игнорируем, чтобы не провоцировать спамера дальше
        if u.throttled_until and u.throttled_until <= now:
            u.throttled_until = None

        # --- 1. ожидается ответ на капчу ---
        if u.captcha_pending:
            if u.captcha_asked_at and now - u.captcha_asked_at > timedelta(minutes=CAPTCHA_TIMEOUT_MINUTES):
                # капча "протухла" — зададим новую при следующем сообщении
                u.captcha_pending = False
            else:
                answer_ok = bool(text) and text.strip() == (u.captcha_answer or "")
                if answer_ok:
                    u.captcha_pending = False
                    u.captcha_answer = None
                    u.req_window_start = now
                    u.req_window_count = 0
                    await s.commit()
                    return AntispamResult(False, f"{em('check')} Проверка пройдена, можно продолжать.")
                else:
                    await s.commit()
                    return AntispamResult(False, f"{em('warn')} Неверно. Решите пример из "
                                                 "предыдущего сообщения, чтобы продолжить.")

        # --- 2. rate-limit (окно N секунд) ---
        window = timedelta(seconds=cfg.rate_limit_window or 10)
        if not u.req_window_start or now - u.req_window_start > window:
            u.req_window_start = now
            u.req_window_count = 1
        else:
            u.req_window_count += 1

        rate_max = cfg.rate_limit_max or 6
        if u.req_window_count > rate_max:
            # флуд внутри окна — прогрессирующий тайм-аут
            u.spam_strikes += 1
            minutes = _throttle_minutes(u.spam_strikes)
            u.throttled_until = now + timedelta(minutes=minutes)
            u.req_window_count = 0
            await s.commit()
            return AntispamResult(False, f"{em('no_entry')} Слишком много сообщений подряд. "
                                         f"Подождите {minutes} мин.")

        # --- 3. капча каждые N запросов ---
        u.total_requests += 1
        every = cfg.captcha_every if cfg.captcha_every is not None else 20
        if every > 0 and u.total_requests % every == 0:
            q, answer = _make_captcha()
            u.captcha_pending = True
            u.captcha_answer = answer
            u.captcha_asked_at = now
            await s.commit()
            return AntispamResult(False, f"{em('shield')} Проверка: реши пример и пришли ответ "
                                         f"числом.\n<b>{q}</b>")

        await s.commit()

    # сброс счётчика страйков, если долго не нарушал — не наказываем вечно
    # за один давний всплеск
    return AntispamResult(True)


async def reset_strikes_if_stale(bot_db_id: int, user_id: int):
    """Обнуляет spam_strikes, если последнее нарушение было давно.
    Вызывается не на каждое сообщение (дорого), а по желанию из админки/крона."""
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_db_id, BotUser.user_id == user_id))
        if u and u.spam_strikes and u.throttled_until:
            if datetime.utcnow() - u.throttled_until > timedelta(hours=STRIKE_RESET_AFTER_HOURS):
                u.spam_strikes = 0
                await s.commit()
