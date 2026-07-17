import re
from datetime import datetime, timedelta
from sqlalchemy import select
from db.base import Session
from db.models import BotUser, ChildBot, ModerationLog, PlatformUser

DURATION_RE = re.compile(r"^(\d+)([mhdwy])$|^perm$", re.I)
UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks", "y": "days"}


def parse_duration(token: str) -> datetime | None:
    """'7d' -> дата окончания, 'perm' -> None (навсегда)."""
    m = DURATION_RE.match(token)
    if not m or m.group(0).lower() == "perm":
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "y":
        n *= 365
    return datetime.utcnow() + timedelta(**{UNITS[unit]: n})


def parse_ban_args(args: str) -> tuple[int, str, str] | None:
    """'/ban 1234 Неадекват 7d' -> (1234, 'Неадекват', '7d')."""
    parts = args.split()
    if not parts or not parts[0].lstrip("-").isdigit():
        return None
    user_id = int(parts[0])
    duration = "perm"
    if len(parts) > 1 and DURATION_RE.match(parts[-1]):
        duration = parts.pop()
    reason = " ".join(parts[1:]) or "Не указана"
    return user_id, reason, duration


async def get_or_create_user(session, bot_id: int, tg_user) -> BotUser:
    u = await session.scalar(select(BotUser).where(
        BotUser.bot_id == bot_id, BotUser.user_id == tg_user.id))
    if not u:
        u = BotUser(bot_id=bot_id, user_id=tg_user.id,
                    username=tg_user.username, full_name=tg_user.full_name)
        session.add(u)
        await session.flush()
    else:
        u.username, u.full_name = tg_user.username, tg_user.full_name
        u.last_active = datetime.utcnow()
        u.is_blocked_bot = False
    return u


async def _log(bot_id: int, admin_id: int, admin_username: str | None,
               action: str, target_user_id: int, reason: str | None = None):
    async with Session() as s:
        s.add(ModerationLog(bot_id=bot_id, admin_id=admin_id, admin_username=admin_username,
                            action=action, target_user_id=target_user_id, reason=reason))
        await s.commit()


async def ban_user(bot_id: int, user_id: int, reason: str, duration: str,
                   admin_id: int = 0, admin_username: str | None = None) -> str:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.user_id == user_id))
        if not u:
            u = BotUser(bot_id=bot_id, user_id=user_id)
            s.add(u)
        u.is_banned, u.ban_reason = True, reason
        u.ban_until = parse_duration(duration)
        await s.commit()
        until = u.ban_until.strftime("%d.%m.%Y %H:%M") if u.ban_until else "навсегда"
    await _log(bot_id, admin_id, admin_username, "ban", user_id, reason)
    return f"Пользователь <code>{user_id}</code> забанен ({until}).\nПричина: {reason}"


async def unban_user(bot_id: int, user_id: int,
                     admin_id: int = 0, admin_username: str | None = None) -> str:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.user_id == user_id))
        if u:
            u.is_banned, u.ban_until, u.ban_reason = False, None, None
            await s.commit()
    await _log(bot_id, admin_id, admin_username, "unban", user_id)
    return f"Пользователь <code>{user_id}</code> разбанен."


async def warn_user(bot_id: int, user_id: int, reason: str,
                    admin_id: int = 0, admin_username: str | None = None) -> tuple[str, bool]:
    """Возвращает (текст_для_админа, автобан_сработал)."""
    async with Session() as s:
        bot_cfg = await s.get(ChildBot, bot_id)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.user_id == user_id))
        if not u:
            u = BotUser(bot_id=bot_id, user_id=user_id)
            s.add(u)
        u.warns += 1
        text = f"Варн <code>{user_id}</code> ({u.warns}/{bot_cfg.warn_limit}).\nПричина: {reason}"
        autoban = u.warns >= bot_cfg.warn_limit
        if autoban:
            u.is_banned, u.ban_until = True, None
            u.ban_reason = f"Автобан: {bot_cfg.warn_limit} варнов"
            text += "\n🚫 Достигнут лимит — автобан!"
        await s.commit()
    await _log(bot_id, admin_id, admin_username, "warn", user_id, reason)
    return text, autoban


async def unwarn_user(bot_id: int, user_id: int,
                      admin_id: int = 0, admin_username: str | None = None) -> str:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.user_id == user_id))
        if u and u.warns > 0:
            u.warns -= 1
            await s.commit()
            result = f"Снят варн с <code>{user_id}</code> (теперь {u.warns})."
        else:
            result = "У пользователя нет варнов."
    await _log(bot_id, admin_id, admin_username, "unwarn", user_id)
    return result


async def is_banned(bot_id: int, user_id: int) -> bool:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.user_id == user_id))
        if not u or not u.is_banned:
            return False
        if u.ban_until and u.ban_until < datetime.utcnow():
            u.is_banned = False           # бан истёк
            await s.commit()
            return False
        return True


async def ban_platform_user(user_id: int, reason: str) -> str:
    """Банит пользователя в САМОМ КОНСТРУКТОРЕ (master-боте) — он не сможет
    им пользоваться вообще (только SUPER_ADMIN может это делать, см.
    master/router.py)."""
    async with Session() as s:
        u = await s.get(PlatformUser, user_id)
        if not u:
            u = PlatformUser(id=user_id)
            s.add(u)
        u.is_banned, u.ban_reason, u.banned_at = True, reason, datetime.utcnow()
        await s.commit()
    return f"Пользователь <code>{user_id}</code> забанен в конструкторе.\nПричина: {reason}"


async def unban_platform_user(user_id: int) -> str:
    async with Session() as s:
        u = await s.get(PlatformUser, user_id)
        if u:
            u.is_banned, u.ban_reason, u.banned_at = False, None, None
            await s.commit()
    return f"Пользователь <code>{user_id}</code> разбанен в конструкторе."


async def is_platform_banned(user_id: int) -> bool:
    async with Session() as s:
        u = await s.get(PlatformUser, user_id)
        return bool(u and u.is_banned)


async def admin_stats_text(bot_id: int) -> str:
    """Текстовый блок 'статистика по админам' — сколько сообщений/банов/варнов
    каждый админ обработал (для кнопки '📊 Статистика' в конструкторе)."""
    async with Session() as s:
        from db.models import MessageLog
        rows = (await s.scalars(select(MessageLog).where(
            MessageLog.bot_id == bot_id, MessageLog.direction == "out",
            MessageLog.is_admin.is_(True)))).all()
        mod_rows = (await s.scalars(select(ModerationLog).where(
            ModerationLog.bot_id == bot_id))).all()
    msg_counts: dict[str, int] = {}
    for r in rows:
        key = r.admin_username or "—"
        msg_counts[key] = msg_counts.get(key, 0) + 1
    action_counts: dict[str, dict[str, int]] = {}
    for r in mod_rows:
        key = r.admin_username or str(r.admin_id)
        d = action_counts.setdefault(key, {"ban": 0, "warn": 0, "unban": 0, "unwarn": 0})
        d[r.action] = d.get(r.action, 0) + 1
    names = sorted(set(msg_counts) | set(action_counts))
    if not names:
        return "Пока нет данных по действиям админов."
    lines = []
    for name in names:
        msgs = msg_counts.get(name, 0)
        a = action_counts.get(name, {})
        lines.append(f"@{name}: {msgs} ответов, 🚫 {a.get('ban', 0)} банов, "
                    f"⚠️ {a.get('warn', 0)} варнов")
    return "\n".join(lines)
