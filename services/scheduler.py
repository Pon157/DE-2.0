import asyncio
import logging
from datetime import datetime
from aiogram import Bot
from sqlalchemy import select
from db.base import Session
from db.models import Post, ChildBot

log = logging.getLogger("scheduler")


async def run_scheduler(interval: int = 20):
    """Раз в interval секунд публикует отложенные посты, время которых пришло."""
    from child.posting import publish  # локальный импорт — избегаем циклов
    while True:
        try:
            async with Session() as s:
                due = (await s.scalars(select(Post).where(
                    Post.published.is_(False),
                    Post.publish_at.is_not(None),
                    Post.publish_at <= datetime.utcnow()))).all()
                bot_ids = {p.bot_id for p in due}
                cfgs = {}
                for bid in bot_ids:
                    cfgs[bid] = await s.get(ChildBot, bid)

            for p in due:
                cfg = cfgs.get(p.bot_id)
                if not cfg or not cfg.channel_id:
                    continue
                bot = Bot(cfg.token)
                try:
                    await publish(bot, cfg, p.html_text, p.media_file_id, p.media_type,
                                 use_template=False, buttons_json=p.buttons_json)
                    async with Session() as s:
                        obj = await s.get(Post, p.id)
                        obj.published = True
                        await s.commit()
                except Exception:
                    log.exception("Failed to publish scheduled post %s", p.id)
                finally:
                    await bot.session.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(interval)
