import os

from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# WGDashboard API Configuration
WG_DASHBOARD_URL = os.getenv("WG_DASHBOARD_URL", "http://localhost:10086")
WG_DASHBOARD_API_KEY = os.getenv("WG_DASHBOARD_API_KEY")
WG_CONFIG_NAME = os.getenv("WG_CONFIG_NAME", "awg0")

# Database Configuration
DATABASE_FILE = "data/wgbot.db"
CLIENTS_JSON_PATH = os.getenv("CLIENTS_JSON_PATH", "clients.json")

# Peer Configuration
PEER_EXPIRY_DAYS = 30

# Payment Configuration
# YooKassa Configuration
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# Webhook Configuration
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
DOMAIN = os.getenv("DOMAIN")

# Support
SUPPORT_URL = os.getenv("SUPPORT_URL")


# Tariff Configuration (env-driven with sensible defaults)
def get_tariffs():
    """Получает тарифы из переменных окружения (динамически перезагружаемые)"""
    # Перезагружаем переменные окружения для получения актуальных значений
    from dotenv import load_dotenv

    load_dotenv(override=True)

    tariff_14_days_stars = int(os.getenv("TARIFF_14_DAYS_STARS", 100))
    tariff_14_days_rub = int(os.getenv("TARIFF_14_DAYS_RUB", 150))
    tariff_30_days_stars = int(os.getenv("TARIFF_30_DAYS_STARS", 200))
    tariff_30_days_rub = int(os.getenv("TARIFF_30_DAYS_RUB", 300))

    return {
        "14_days": {
            "days": 14,
            "stars_price": tariff_14_days_stars,
            "rub_price": tariff_14_days_rub,
            "name": "14 дней",
            "description": "Доступ на 2 недели",
        },
        "30_days": {
            "days": 30,
            "stars_price": tariff_30_days_stars,
            "rub_price": tariff_30_days_rub,
            "name": "30 дней",
            "description": "Доступ на месяц",
        },
        "90_days": {
            "days": 90,
            "stars_price": int(os.getenv("TARIFF_90_DAYS_STARS", 500)),
            "rub_price": int(os.getenv("TARIFF_90_DAYS_RUB", 800)),
            "name": "90 дней",
            "description": "Доступ на 3 месяца",
        },
        "180_days": {
            "days": 180,
            "stars_price": int(os.getenv("TARIFF_180_DAYS_STARS", 900)),
            "rub_price": int(os.getenv("TARIFF_180_DAYS_RUB", 1500)),
            "name": "180 дней",
            "description": "Доступ на 6 месяцев",
        },
    }


# Инициализируем тарифы при импорте
TARIFFS = get_tariffs()
