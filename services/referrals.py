"""Реферальная система и Pro-подписка.

За каждые 10 приглашённых (реально зашедших в master-бота по реф-ссылке)
пользователь получает 10 дней Pro. Pro убирает рекламу (показы + рассылки)
из ЕГО ботов — рекламу нельзя ни купить для его бота, ни разослать в него
глобальной рассылкой.
"""
from datetime import datetime, timedelta
from sqlalchemy import select
from db.base import Session
from db.models import PlatformUser, ReferralEvent

REFERRALS_PER_BONUS = 10
BONUS_DAYS = 10
PRO_PRICE_RUB = 99


async def get_or_create(user_id: int) -> PlatformUser:
    async with Session() as s:
        pu = await s.get(PlatformUser, user_id)
        if not pu:
            pu = PlatformUser(id=user_id)
            s.add(pu)
            await s.commit()
            await s.refresh(pu)
        return pu


async def register_start(user_id: int, ref_arg: str | None):
    """Вызывается на /start в master-боте. Если это первый визит юзера и есть
    валидный ref_<id> — засчитывает реферала (один раз, защита через
    unique(invitee_id))."""
    async with Session() as s:
        existing = await s.get(PlatformUser, user_id)
        if existing:
            return  # уже был здесь — реферальный аргумент больше не считаем
        pu = PlatformUser(id=user_id)
        inviter_id = None
        if ref_arg and ref_arg.startswith("ref_"):
            try:
                inviter_id = int(ref_arg[4:])
            except ValueError:
                inviter_id = None
        if inviter_id and inviter_id != user_id:
            already = await s.scalar(select(ReferralEvent).where(
                ReferralEvent.invitee_id == user_id))
            if not already:
                s.add(ReferralEvent(inviter_id=inviter_id, invitee_id=user_id))
                pu.referred_by = inviter_id
                inviter = await s.get(PlatformUser, inviter_id)
                if not inviter:
                    inviter = PlatformUser(id=inviter_id)
                    s.add(inviter)
                    await s.flush()
                inviter.referral_count += 1
                if inviter.referral_count % REFERRALS_PER_BONUS == 0:
                    base = inviter.pro_until if (inviter.pro_until and inviter.pro_until > datetime.utcnow()) else datetime.utcnow()
                    inviter.pro_until = base + timedelta(days=BONUS_DAYS)
        s.add(pu)
        await s.commit()


async def is_pro(user_id: int) -> bool:
    async with Session() as s:
        pu = await s.get(PlatformUser, user_id)
        return bool(pu and pu.pro_until and pu.pro_until > datetime.utcnow())


async def grant_pro_days(user_id: int, days: int):
    async with Session() as s:
        pu = await s.get(PlatformUser, user_id)
        if not pu:
            pu = PlatformUser(id=user_id)
            s.add(pu)
            await s.flush()
        base = pu.pro_until if (pu.pro_until and pu.pro_until > datetime.utcnow()) else datetime.utcnow()
        pu.pro_until = base + timedelta(days=days)
        await s.commit()


async def status_text(user_id: int, bot_username: str) -> str:
    pu = await get_or_create(user_id)
    pro_line = (f"✨ Pro активен до {pu.pro_until.strftime('%d.%m.%Y')}"
               if pu.pro_until and pu.pro_until > datetime.utcnow()
               else "Pro не активен")
    left = REFERRALS_PER_BONUS - (pu.referral_count % REFERRALS_PER_BONUS)
    return (f"👥 Приглашено: {pu.referral_count}\n"
           f"🎁 До следующего бонуса (+{BONUS_DAYS} дней Pro): {left}\n"
           f"{pro_line}\n\n"
           f"Ваша реферальная ссылка:\nhttps://t.me/{bot_username}?start=ref_{user_id}")
