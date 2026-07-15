import os
from dotenv import load_dotenv

load_dotenv()

MASTER_BOT_TOKEN = os.environ["MASTER_BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", 0))

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
