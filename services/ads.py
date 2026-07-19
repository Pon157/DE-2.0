"""Логика рекламной системы: тарифы, создание заявок, показ в стартовых
сообщениях дочерних ботов, разовая рассылка во все боты.
"""
import random
from datetime import datetime, timedelta
from sqlalchemy import select
from db.base import Session
from db.models import Advertisement, AdStatus, AdKind, AdCooldown, ChildBot
from config import AD_BASE_PRICE_PER_100, AD_BROADCAST_COOLDOWN_DAYS
from services import referrals

# Тариф рассылки "во все боты" — фиксированная цена (отдельно от показов).
BROADCAST_PRICE_RUB = 500


def price_for_impressions(n: int) -> int:
    """Чем больше показов, тем дешевле цена за сотню (простая ступенчатая скидка).

    100    -> 20 руб/100 (базовая цена)
    500+   -> 18 руб/100 (-10%)
    1000+  -> 16 руб/100 (-20%)
    5000+  -> 13 руб/100 (-35%)
    10000+ -> 11 руб/100 (-45%)
    """
    if n <= 0:
        return 0
    per100 = AD_BASE_PRICE_PER_100
    if n >= 10000:
        per100 = round(AD_BASE_PRICE_PER_100 * 0.55)
    elif n >= 5000:
        per100 = round(AD_BASE_PRICE_PER_100 * 0.65)
    elif n >= 1000:
        per100 = round(AD_BASE_PRICE_PER_100 * 0.80)
    elif n >= 500:
        per100 = round(AD_BASE_PRICE_PER_100 * 0.90)
    price = (n / 100) * per100
    return max(1, round(price))


TARIFF_PRESETS = [100, 500, 1000, 5000, 10000]


async def bot_is_pro_protected(bot_id: int) -> bool:
    """True, если владелец этого бота — Pro-подписчик: рекламу нельзя ни
    купить для этого бота, ни показать/разослать в него."""
    async with Session() as s:
        cb = await s.get(ChildBot, bot_id)
    if not cb:
        return False
    return await referrals.is_pro(cb.owner_id)


async def create_impressions_ad(buyer_id: int, source_bot_id: int | None, text: str,
                                impressions: int) -> Advertisement | None:
    """source_bot_id=None -> реклама показывается в /start ВО ВСЕХ активных
    ботах платформы (кроме Pro-владельцев), а не только в одном выбранном.
    БАГ (по запросу "убери медиа у рекламных постов"): раньше сюда же
    принимались media_file_id/media_type — теперь реклама только текстовая,
    это упрощает модерацию и не даёт разместить потенциально запрещённый
    визуал в чужих ботах без проверки."""
    if source_bot_id is not None and await bot_is_pro_protected(source_bot_id):
        return None  # у владельца Pro — реклама в этот бот недоступна
    price = price_for_impressions(impressions)
    async with Session() as s:
        ad = Advertisement(buyer_id=buyer_id, source_bot_id=source_bot_id,
                           kind=AdKind.impressions, text=text,
                           target_impressions=impressions, price_rub=price,
                           status=AdStatus.pending)
        s.add(ad)
        await s.commit()
        await s.refresh(ad)
        return ad


async def create_broadcast_ad(buyer_id: int, source_bot_id: int, text: str) -> Advertisement | None:
    """Возвращает None, если ещё действует кулдаун 5 дней."""
    async with Session() as s:
        cd = await s.scalar(select(AdCooldown).where(AdCooldown.buyer_id == buyer_id))
        if cd and cd.last_broadcast_at > datetime.utcnow() - timedelta(days=AD_BROADCAST_COOLDOWN_DAYS):
            return None
        ad = Advertisement(buyer_id=buyer_id, source_bot_id=source_bot_id,
                           kind=AdKind.broadcast, text=text,
                           price_rub=BROADCAST_PRICE_RUB, status=AdStatus.pending)
        s.add(ad)
        await s.commit()
        await s.refresh(ad)
        return ad


async def cooldown_remaining(buyer_id: int) -> timedelta | None:
    async with Session() as s:
        cd = await s.scalar(select(AdCooldown).where(AdCooldown.buyer_id == buyer_id))
        if not cd:
            return None
        left = cd.last_broadcast_at + timedelta(days=AD_BROADCAST_COOLDOWN_DAYS) - datetime.utcnow()
        return left if left.total_seconds() > 0 else None


async def mark_cooldown(buyer_id: int):
    async with Session() as s:
        cd = await s.scalar(select(AdCooldown).where(AdCooldown.buyer_id == buyer_id))
        if cd:
            cd.last_broadcast_at = datetime.utcnow()
        else:
            s.add(AdCooldown(buyer_id=buyer_id))
        await s.commit()


async def approve(ad_id: int) -> Advertisement | None:
    async with Session() as s:
        ad = await s.get(Advertisement, ad_id)
        if not ad or ad.status != AdStatus.pending:
            return None
        ad.status = AdStatus.awaiting_payment
        ad.decided_at = datetime.utcnow()
        await s.commit()
        await s.refresh(ad)
        return ad


async def reject(ad_id: int, reason: str = "") -> Advertisement | None:
    async with Session() as s:
        ad = await s.get(Advertisement, ad_id)
        if not ad or ad.status != AdStatus.pending:
            return None
        ad.status = AdStatus.rejected
        ad.reject_reason = reason
        ad.decided_at = datetime.utcnow()
        await s.commit()
        await s.refresh(ad)
        return ad


async def mark_paid(ad_id: int, payment_id: str) -> Advertisement | None:
    async with Session() as s:
        ad = await s.get(Advertisement, ad_id)
        if not ad:
            return None
        ad.paid = True
        ad.payment_id = payment_id
        ad.paid_at = datetime.utcnow()
        if ad.extends_ad_id:
            # Это заявка на ПРОДЛЕНИЕ уже активной (или любой другой)
            # кампании — не переключаем в active сама по себе (не создаём
            # вторую параллельно "живую" запись на тот же текст/бота),
            # вместо этого сразу вливаем оплаченные показы в оригинал.
            # Оригинал при этом ни на миг не останавливается — его
            # status/shown_count не трогаем, только увеличиваем лимит.
            parent = await s.get(Advertisement, ad.extends_ad_id)
            if parent:
                parent.target_impressions += ad.target_impressions
                if parent.status == AdStatus.finished:
                    # если оригинал успел исчерпаться, пока продление ждало
                    # оплаты — реактивируем его, показы снова пойдут
                    parent.status = AdStatus.active
            ad.status = AdStatus.finished  # сама запись-продление больше нигде не участвует
            ad.shown_count = ad.target_impressions
            await s.commit()
            await s.refresh(parent) if parent else None
            return parent or ad
        ad.status = AdStatus.active
        await s.commit()
        await s.refresh(ad)
        return ad


async def get_active_ad_for_display(bot_id: int) -> Advertisement | None:
    """Случайная активная реклама с показами в запасе — для КОНКРЕТНОГО бота
    (source_bot_id == bot_id) ИЛИ показ "во всех ботах" (source_bot_id IS
    NULL). Не показываем рекламу, если владелец бота — Pro."""
    if await bot_is_pro_protected(bot_id):
        return None
    async with Session() as s:
        ads = (await s.scalars(select(Advertisement).where(
            Advertisement.status == AdStatus.active,
            Advertisement.kind == AdKind.impressions,
            (Advertisement.source_bot_id == bot_id) | (Advertisement.source_bot_id.is_(None))))).all()
        ads = [a for a in ads if a.shown_count < a.target_impressions]
        if not ads:
            return None
        return random.choice(ads)


async def register_impression(ad_id: int):
    async with Session() as s:
        ad = await s.get(Advertisement, ad_id)
        if not ad:
            return
        ad.shown_count += 1
        if ad.shown_count >= ad.target_impressions:
            ad.status = AdStatus.finished
        await s.commit()


async def all_active_bots_tokens() -> list[tuple[int, str]]:
    """Активные боты, ИСКЛЮЧАЯ те, чей владелец — Pro (в них ничего не
    отправляется при глобальной рекламной рассылке)."""
    async with Session() as s:
        rows = (await s.scalars(select(ChildBot).where(ChildBot.is_active))).all()
    result = []
    for cb in rows:
        if await referrals.is_pro(cb.owner_id):
            continue
        result.append((cb.id, cb.token))
    return result


async def list_active_bots() -> list[ChildBot]:
    """Для выбора бота, в котором размещается реклама (см. /ads в мастер-боте).
    Боты Pro-владельцев не показываются — в них рекламу не разместить."""
    async with Session() as s:
        bots = list((await s.scalars(select(ChildBot).where(
            ChildBot.is_active).order_by(ChildBot.username))).all())
    result = []
    for cb in bots:
        if await referrals.is_pro(cb.owner_id):
            continue
        result.append(cb)
    return result


# =========================================================================
# ===================   Рекламный кабинет покупателя   ===================
# =========================================================================
async def list_my_ads(buyer_id: int) -> list[Advertisement]:
    """Все кампании конкретного покупателя — для "🎯 Мои кампании"."""
    async with Session() as s:
        return list((await s.scalars(select(Advertisement).where(
            Advertisement.buyer_id == buyer_id
        ).order_by(Advertisement.created_at.desc()))).all())


async def my_spend_total(buyer_id: int) -> int:
    """Сколько всего потрачено (только реально оплаченные кампании)."""
    async with Session() as s:
        ads = (await s.scalars(select(Advertisement).where(
            Advertisement.buyer_id == buyer_id, Advertisement.paid.is_(True)))).all()
    return sum(a.price_rub for a in ads)


async def get_ad_for_owner(ad_id: int, buyer_id: int) -> Advertisement | None:
    async with Session() as s:
        ad = await s.get(Advertisement, ad_id)
    if not ad or ad.buyer_id != buyer_id:
        return None
    return ad


async def extend_ad(ad_id: int, buyer_id: int, extra_impressions: int) -> Advertisement | None:
    """"Продлить" кампанию — докупить ещё показов к impressions-кампании,
    ДАЖЕ ПОКА ОНА ЕЩЁ АКТИВНА и показывается. Раньше это было возможно
    только для уже полностью исчерпанной (finished) кампании — расширение
    активной штатно требовало бы временно переводить её в awaiting_payment,
    что прервало бы показы до оплаты.

    Реализовано через ОТДЕЛЬНУЮ заявку (Advertisement.extends_ad_id) —
    проходит обычную модерацию/оплату независимо, а после оплаты (см.
    mark_paid) её показы вливаются в оригинал одним increment'ом, без
    единой секунды простоя исходной кампании."""
    async with Session() as s:
        ad = await s.get(Advertisement, ad_id)
        if not ad or ad.buyer_id != buyer_id or ad.kind != AdKind.impressions:
            return None
        if ad.status not in (AdStatus.active, AdStatus.finished):
            return None
        price = price_for_impressions(extra_impressions)
        extension = Advertisement(buyer_id=buyer_id, source_bot_id=ad.source_bot_id,
                                  kind=AdKind.impressions, text=ad.text,
                                  target_impressions=extra_impressions, price_rub=price,
                                  status=AdStatus.pending, extends_ad_id=ad.id)
        s.add(extension)
        await s.commit()
        await s.refresh(extension)
        return extension
