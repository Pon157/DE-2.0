from io import BytesIO
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select, func
from db.base import Session
from db.models import BotUser, MessageLog, Suggestion, Post, ChildBot
from config import STATS_DAYS

BG, FG, ACCENT, GRID = (23, 23, 33), (235, 235, 245), (110, 140, 255), (55, 55, 75)
GREEN, RED = (90, 200, 130), (235, 90, 90)


def _font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_chart(draw, box, series: list[int], labels: list[str], color):
    x0, y0, x1, y1 = box
    mx = max(series) or 1
    n = len(series)
    # сетка
    for i in range(5):
        y = y0 + (y1 - y0) * i / 4
        draw.line([(x0, y), (x1, y)], fill=GRID, width=1)
        draw.text((x0 - 8, y - 7), str(round(mx * (4 - i) / 4)),
                  font=_font(13), fill=FG, anchor="ra")
    pts = [(x0 + (x1 - x0) * i / max(n - 1, 1),
            y1 - (y1 - y0) * v / mx) for i, v in enumerate(series)]
    if len(pts) > 1:
        draw.line(pts, fill=color, width=3, joint="curve")
    for p in pts:
        draw.ellipse([p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4], fill=color)
    for i, lab in enumerate(labels):
        if i % max(1, n // 7) == 0:
            draw.text((pts[i][0], y1 + 8), lab, font=_font(12), fill=FG, anchor="ma")


async def build_stats_image(bot_id: int) -> BytesIO:
    today = datetime.utcnow().date()
    days = [today - timedelta(days=i) for i in range(STATS_DAYS - 1, -1, -1)]

    async with Session() as s:
        cfg = await s.get(ChildBot, bot_id)
        total_users = await s.scalar(select(func.count()).where(BotUser.bot_id == bot_id))
        blocked = await s.scalar(select(func.count()).where(
            BotUser.bot_id == bot_id, BotUser.is_blocked_bot.is_(True)))
        banned = await s.scalar(select(func.count()).where(
            BotUser.bot_id == bot_id, BotUser.is_banned.is_(True)))
        total_msgs = await s.scalar(select(func.count()).where(MessageLog.bot_id == bot_id))

        # сообщения по дням
        rows = (await s.execute(
            select(func.date(MessageLog.created_at), func.count())
            .where(MessageLog.bot_id == bot_id,
                   MessageLog.created_at >= datetime.utcnow() - timedelta(days=STATS_DAYS))
            .group_by(func.date(MessageLog.created_at)))).all()
        per_day = {r[0]: r[1] for r in rows}
        msg_series = [per_day.get(d, 0) for d in days]

        # новые юзеры по дням
        rows = (await s.execute(
            select(func.date(BotUser.first_seen), func.count())
            .where(BotUser.bot_id == bot_id,
                   BotUser.first_seen >= datetime.utcnow() - timedelta(days=STATS_DAYS))
            .group_by(func.date(BotUser.first_seen)))).all()
        u_day = {r[0]: r[1] for r in rows}
        user_series = [u_day.get(d, 0) for d in days]

        # активность админов: всего / неделя / день
        admin_rows = []
        for label, since in (("всего", None), ("7д", 7), ("24ч", 1)):
            q = (select(MessageLog.user_id, MessageLog.admin_username, func.count())
                 .where(MessageLog.bot_id == bot_id, MessageLog.is_admin.is_(True))
                 .group_by(MessageLog.user_id, MessageLog.admin_username))
            if since:
                q = q.where(MessageLog.created_at >= datetime.utcnow() - timedelta(days=since))
            admin_rows.append((label, (await s.execute(q)).all()))

        posting_lines = []
        if cfg.bot_type.value == "posting":
            published = await s.scalar(select(func.count()).where(
                Post.bot_id == bot_id, Post.published.is_(True)))
            posting_lines.append(f"Постов выложено: {published}")
            rows = (await s.execute(
                select(Suggestion.decided_by_username, Suggestion.status, func.count())
                .where(Suggestion.bot_id == bot_id, Suggestion.status != "pending")
                .group_by(Suggestion.decided_by_username, Suggestion.status))).all()
            agg: dict[str, dict] = {}
            for un, st, c in rows:
                agg.setdefault(un or "?", {"approved": 0, "rejected": 0})[st] = c
            for un, d in agg.items():
                posting_lines.append(f"@{un}: принял {d['approved']}, отклонил {d['rejected']}")

    # ---- рисуем ----
    W, H = 1200, 1400
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # БАГ: эмодзи в заголовке рисовался "квадратом" — в DejaVu нет цветных
    # эмодзи. В картинке используем только текст.
    d.text((W // 2, 40), f"Статистика @{cfg.username}", font=_font(36), fill=FG, anchor="ma")

    stats = [("Пользователей", total_users, ACCENT), ("Сообщений", total_msgs, GREEN),
             ("Заблокировали", blocked, RED), ("Забанено", banned, RED)]
    for i, (label, val, color) in enumerate(stats):
        x = 60 + i * 275
        d.rounded_rectangle([x, 110, x + 255, 210], 18, fill=(33, 33, 48))
        d.text((x + 127, 130), str(val), font=_font(34), fill=color, anchor="ma")
        d.text((x + 127, 175), label, font=_font(16), fill=FG, anchor="ma")

    labels = [dd.strftime("%d.%m") for dd in days]
    d.text((80, 250), "Сообщения по дням", font=_font(22), fill=FG)
    _line_chart(d, (110, 300, W - 80, 560), msg_series, labels, ACCENT)
    d.text((80, 620), "Новые пользователи", font=_font(22), fill=FG)
    _line_chart(d, (110, 670, W - 80, 930), user_series, labels, GREEN)

    y = 990
    d.text((80, y), "Активность админов:", font=_font(22), fill=FG); y += 40
    for label, rows in admin_rows:
        for uid, un, cnt in rows[:6]:
            d.text((100, y), f"[{label}] {cnt} сообщ. — @{un or '—'} (id {uid})",
                   font=_font(17), fill=FG); y += 26
    for line in posting_lines:
        d.text((100, y), line, font=_font(17), fill=GREEN); y += 26

    buf = BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf
