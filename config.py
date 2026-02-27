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
CUSTOM_CLIENTS_PATH = os.getenv("CUSTOM_CLIENTS_PATH", "custom_clients.txt")

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

# Promo Configuration
PROMO_FILE_PATH = os.getenv("PROMO_FILE_PATH", "promo.txt")


# Tariff Configuration (env-driven with sensible defaults)
def get_tariffs():
    """Load tariffs from environment variables (dynamically reloaded)."""
    # Reload environment variables to get the latest values
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
            "name": "2 недели",
            "description": "Доступ на 2 недели",
        },
        "30_days": {
            "days": 30,
            "stars_price": tariff_30_days_stars,
            "rub_price": tariff_30_days_rub,
            "name": "1 месяц",
            "description": "Доступ на 1 месяц",
        },
        "90_days": {
            "days": 90,
            "stars_price": int(os.getenv("TARIFF_90_DAYS_STARS", 500)),
            "rub_price": int(os.getenv("TARIFF_90_DAYS_RUB", 800)),
            "name": "3 месяца",
            "description": "Доступ на 3 месяца",
        },
        "180_days": {
            "days": 180,
            "stars_price": int(os.getenv("TARIFF_180_DAYS_STARS", 900)),
            "rub_price": int(os.getenv("TARIFF_180_DAYS_RUB", 1500)),
            "name": "6 месяцев",
            "description": "Доступ на 6 месяцев",
        },
    }


# Initialize tariffs on import
TARIFFS = get_tariffs()
