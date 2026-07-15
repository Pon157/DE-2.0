"""Мини aiohttp-сервер для приёма вебхуков ЮKassa.

Слушает config.AD_WEBHOOK_PORT. Должен быть проксирован (nginx/traefik) на
https://dialogengine.ru/yookassa/webhook — этот URL указывается в личном
кабинете ЮKassa как HTTP-уведомление.
"""
import logging
from aiohttp import web
from aiogram import Bot
from db.base import Session
from db.models import Advertisement, AdKind
from services import ads as ads_service
from services import referrals
from services.payments import fetch_payment
from services.broadcast import run_broadcast
from utils.emoji import em
from config import MASTER_BOT_TOKEN

log = logging.getLogger("ad_webhook")


async def _notify(user_id: int, text: str):
    # И реклама, и Pro теперь покупаются прямо в master-боте — покупатель
    # уже переписывается именно с ним, отдельный Bot() для дочернего бота
    # не нужен (раньше это было лишним источником нестабильности).
    bot = Bot(MASTER_BOT_TOKEN)
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception:
        log.exception("Failed to notify user %s", user_id)
    finally:
        await bot.session.close()


async def _run_broadcast_ad(ad: Advertisement):
    """Разослать рекламный текст во все активные дочерние боты всем их
    пользователям (боты Pro-владельцев автоматически исключены)."""
    bots = await ads_service.all_active_bots_tokens()
    for bot_id, token in bots:
        try:
            await run_broadcast(token, bot_id, target="all",
                                html_text=ad.text, media_file_id=ad.media_file_id,
                                media_type=ad.media_type)
        except Exception:
            log.exception("Broadcast ad failed for bot_id=%s", bot_id)


async def _handle_ad_payment(payment) -> web.Response:
    ad_id = int((payment.metadata or {}).get("ad_id", 0))
    if not ad_id:
        return web.json_response({"ok": True})

    async with Session() as s:
        ad = await s.get(Advertisement, ad_id)
        if not ad or ad.paid:
            return web.json_response({"ok": True})  # уже обработано — идемпотентно

    ad = await ads_service.mark_paid(ad_id, payment.id)
    if not ad:
        return web.json_response({"ok": True})

    if ad.kind == AdKind.broadcast:
        await ads_service.mark_cooldown(ad.buyer_id)
        await _run_broadcast_ad(ad)
        await _notify(ad.buyer_id,
                     f"{em('party')} Оплата получена, рассылка запущена во всех ботах!")
    else:
        await _notify(ad.buyer_id,
                     f"{em('party')} Оплата получена! Реклама на "
                     f"{ad.target_impressions} показов запущена.")
    return web.json_response({"ok": True})


async def _handle_pro_payment(payment) -> web.Response:
    meta = payment.metadata or {}
    user_id = int(meta.get("pro_user_id", 0))
    months = int(meta.get("pro_months", 1))
    if not user_id:
        return web.json_response({"ok": True})
    await referrals.grant_pro_days(user_id, months * 30)
    await _notify(user_id, f"{em('sparkles')} Оплата получена! Pro-подписка "
                  f"активирована на {months} мес. — в ваших ботах больше нет рекламы.")
    return web.json_response({"ok": True})


async def handle_webhook(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)

    event = data.get("event")
    obj = data.get("object") or {}
    payment_id = obj.get("id")
    if event != "payment.succeeded" or not payment_id:
        # Другие события (canceled и т.п.) — просто подтверждаем получение,
        # ничего не делаем.
        return web.json_response({"ok": True})

    # НЕ доверяем содержимому вебхука — переспрашиваем у ЮKassa напрямую.
    try:
        payment = await fetch_payment(payment_id)
    except Exception:
        log.exception("Failed to verify payment %s", payment_id)
        return web.json_response({"ok": False}, status=500)

    if payment.status != "succeeded":
        return web.json_response({"ok": True})

    meta = payment.metadata or {}
    if "ad_id" in meta:
        return await _handle_ad_payment(payment)
    if "pro_user_id" in meta:
        return await _handle_pro_payment(payment)
    return web.json_response({"ok": True})


async def handle_return(request: web.Request) -> web.Response:
    return web.Response(text="Оплата обрабатывается, вернитесь в Telegram-бота.",
                        content_type="text/plain")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/yookassa/webhook", handle_webhook)
    app.router.add_get("/yookassa/return", handle_return)
    return app


async def run_webhook_server(port: int):
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("YooKassa webhook server listening on :%s", port)
    return runner
