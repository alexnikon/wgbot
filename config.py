import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()



# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Cascade server registry
CASCADE_SERVERS_FILE = Path(
    os.getenv("CASCADE_SERVERS_FILE", "/run/secrets/cascade_servers.json")
)
CASCADE_REQUEST_TIMEOUT = float(os.getenv("CASCADE_REQUEST_TIMEOUT", "20"))
CASCADE_RESERVATION_MINUTES = int(os.getenv("CASCADE_RESERVATION_MINUTES", "30"))
CASCADE_RETRY_INTERVAL_SECONDS = int(
    os.getenv("CASCADE_RETRY_INTERVAL_SECONDS", "60")
)

# Database Configuration
DATABASE_FILE = os.getenv("DATABASE_PATH", "data/wgbot.db")

# Payment Configuration
# YooKassa Configuration
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
PAYMENT_RETURN_URL = os.getenv("PAYMENT_RETURN_URL", "https://t.me/nikonvpn_bot")

# Webhook Configuration
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
DOMAIN = os.getenv("DOMAIN")

# Support
SUPPORT_URL = os.getenv("SUPPORT_URL")

# Admin notifications
def get_admin_telegram_ids() -> list[int]:
    """Parse admin Telegram IDs from ADMIN_TELEGRAM_IDS."""
    raw_value = os.getenv("ADMIN_TELEGRAM_IDS", "")
    result: list[int] = []
    for raw_item in raw_value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            result.append(int(item))
        except ValueError:
            continue
    return result

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
    }


# Initialize tariffs on import
TARIFFS = get_tariffs()
