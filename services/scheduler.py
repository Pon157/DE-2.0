import asyncio
import json
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
                    # БАГ (критичный): publish() объявлена с keyword-only
                    # параметрами (после cfg стоит *), а здесь всё передавалось
                    # ПОЗИЦИОННО — каждый отложенный пост падал с TypeError на
                    # КАЖДОМ тике и не публиковался НИКОГДА. Плюс терялись
                    # альбомы и кнопки (не передавались media_group/origin_*/
                    # buttons_mode), а шаблон поста не применялся вовсе
                    # (жёсткий use_template=False в обход настройки).
                    await publish(
                        bot, cfg,
                        html_text=p.html_text,
                        file_id=p.media_file_id,
                        media_type=p.media_type,
                        media_group=json.loads(p.media_group_json) if p.media_group_json else None,
                        origin_chat_id=p.origin_chat_id,
                        origin_message_id=p.origin_message_id,
                        origin_message_ids=p.origin_message_ids,
                        use_template=(cfg.channel_delivery_mode != "copy"),
                        buttons_json=p.buttons_json,
                        buttons_mode=p.buttons_mode or "both")
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
