import os
from dotenv import load_dotenv

load_dotenv()

MASTER_BOT_TOKEN = os.environ["MASTER_BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", 0))

# ---- шифрование токенов дочерних ботов в БД ----
# Токен бота — это фактически пароль полного доступа к нему (кто угодно с
# токеном может писать от его имени, менять настройки через Bot API и
# т.п.), поэтому хранить его в БД открытым текстом небезопасно — при
# утечке дампа БД скомпрометированы ВСЕ боты платформы разом. Раньше так и
# было (столбец token хранился как есть). Теперь токен шифруется на лету
# при записи/чтении (см. utils/crypto.py, db/models.py::EncryptedToken).
#
# Ключ ОБЯЗАТЕЛЬНО берите из переменной окружения TOKEN_ENCRYPTION_KEY —
# сгенерировать: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Если переменная не задана, ключ детерминированно выводится из
# MASTER_BOT_TOKEN — это лучше, чем вообще без шифрования, но НЕ заменяет
# отдельный секрет: задайте TOKEN_ENCRYPTION_KEY явно на проде.
TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
if not TOKEN_ENCRYPTION_KEY:
    import base64
    import hashlib
    import logging
    logging.getLogger("config").warning(
        "TOKEN_ENCRYPTION_KEY не задан в окружении — токены ботов шифруются "
        "ключом, выведенным из MASTER_BOT_TOKEN. Настоятельно рекомендуется "
        "задать TOKEN_ENCRYPTION_KEY отдельно (см. комментарий в config.py).")
    TOKEN_ENCRYPTION_KEY = base64.urlsafe_b64encode(
        hashlib.sha256(("de2-token-key::" + MASTER_BOT_TOKEN).encode()).digest()).decode()

BROADCAST_RATE = 25          # сообщений/сек при рассылке
STATS_DAYS = 14              # дней на графике

# ---- YooKassa (оплата рекламы) ----
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "")
# Домен для return_url/webhook, ЮKassa шлёт уведомления сюда:
# https://dialogengine.ru/yookassa/webhook — путь должен быть проксирован
# (nginx/traefik) на порт AD_WEBHOOK_PORT этого приложения.
PAYMENT_CALLBACK_DOMAIN = os.environ.get("PAYMENT_CALLBACK_DOMAIN", "https://dialogengine.ru")
AD_WEBHOOK_PORT = int(os.environ.get("AD_WEBHOOK_PORT", 8085))

# ---- реклама ----
AD_MAX_LEN = 100
AD_BROADCAST_COOLDOWN_DAYS = 5
# тарифы за показы: (показов, цена_руб). Дальше цена за 100 показов дешевле
# при большем объёме (простая линейная скидка, см. services/ads.py).
AD_BASE_PRICE_PER_100 = 20

# ---- Pro-подписка и рефералка ----
PRO_PRICE_RUB = 99
PLATFORM_BOT_USERNAME = "Dialogue_Enginebot"
