"""Интеграция с ЮKassa для оплаты рекламы.

ВАЖНО: ЮKassa не подписывает вебхуки секретом по умолчанию, поэтому по
рекомендации самой ЮKassa мы не доверяем телу вебхука напрямую — при
получении уведомления мы отдельным запросом (Payment.find_one) переспрашиваем
статус платежа по его id через официальный API. Так поддельный POST на
webhook не может "подтвердить" оплату, которой не было.

ВАЖНО №2 (нашёл при разборе бага "менеджер падает вместе со всеми ботами"):
официальный SDK `yookassa` делает СИНХРОННЫЕ HTTP-запросы (через `requests`).
Весь проект — один процесс с ОДНИМ общим asyncio event loop, на котором
крутится long-polling сразу всех дочерних ботов. Если вызвать
`Payment.create(...)` напрямую внутри async-хендлера, поток исполнения
блокируется на время сетевого запроса к ЮKassa — а вместе с ним блокируется
ВЕСЬ event loop, то есть замирают ВСЕ боты платформы одновременно, пока не
придёт ответ от ЮKassa. При медленной сети/таймауте это выглядит как
хаотичные "случайные падения" сразу нескольких ботов. Поэтому здесь и ниже
каждый вызов SDK обёрнут в `asyncio.to_thread`, чтобы блокирующий код
выполнялся в отдельном потоке и не останавливал event loop.
"""
import logging
import uuid
import asyncio
from yookassa import Configuration, Payment
from config import (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY,
                    PAYMENT_CALLBACK_DOMAIN)

log = logging.getLogger("payments")

_configured = False


def _ensure_configured():
    global _configured
    if not _configured:
        if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
            raise RuntimeError(
                "YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY не заданы в .env — "
                "оплата рекламы не может быть создана.")
        Configuration.account_id = YOOKASSA_SHOP_ID
        Configuration.secret_key = YOOKASSA_SECRET_KEY
        _configured = True


def _create_payment_sync(payload: dict, idempotence_key: str):
    return Payment.create(payload, idempotence_key)


async def create_payment(amount_rub: int, description: str, metadata: dict) -> tuple[str, str]:
    """Создаёт платёж в ЮKassa. Возвращает (payment_id, confirmation_url)."""
    _ensure_configured()
    idempotence_key = str(uuid.uuid4())
    payload = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "return_url": f"{PAYMENT_CALLBACK_DOMAIN}/yookassa/return",
        },
        "capture": True,
        "description": description[:128],
        "metadata": {k: str(v) for k, v in metadata.items()},
    }
    # asyncio.to_thread — см. пояснение в шапке файла: без этого блокирующий
    # HTTP-запрос заморозил бы поллинг ВСЕХ ботов платформы одновременно.
    payment = await asyncio.to_thread(_create_payment_sync, payload, idempotence_key)
    return payment.id, payment.confirmation.confirmation_url


async def create_ad_payment(ad_id: int, amount_rub: int, description: str) -> tuple[str, str]:
    """Платёж за рекламу (см. create_payment)."""
    return await create_payment(amount_rub, description, {"ad_id": ad_id})


async def create_pro_payment(user_id: int, months: int = 1) -> tuple[str, str]:
    """Платёж за Pro-подписку."""
    from config import PRO_PRICE_RUB  # локальный импорт, избегаем цикла
    amount = PRO_PRICE_RUB * months
    return await create_payment(amount, f"Pro-подписка на {months} мес.",
                                {"pro_user_id": user_id, "pro_months": months})


async def fetch_payment(payment_id: str):
    """Переспрашивает статус платежа напрямую у ЮKassa (не доверяем телу вебхука)."""
    _ensure_configured()
    return await asyncio.to_thread(Payment.find_one, payment_id)
