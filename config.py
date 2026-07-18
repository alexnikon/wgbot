import os
from functools import lru_cache
from pathlib import Path


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    value = float(os.getenv(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value



# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Cascade server registry
CASCADE_SERVERS_FILE = Path(
    os.getenv("CASCADE_SERVERS_FILE", "/run/secrets/cascade_servers.json")
)
CASCADE_REQUEST_TIMEOUT = _get_float("CASCADE_REQUEST_TIMEOUT", 20, minimum=1)
CASCADE_RESERVATION_MINUTES = _get_int("CASCADE_RESERVATION_MINUTES", 30, minimum=1)
CASCADE_RETRY_INTERVAL_SECONDS = _get_int(
    "CASCADE_RETRY_INTERVAL_SECONDS", 60, minimum=5
)
PROVISIONING_LEASE_SECONDS = _get_int("PROVISIONING_LEASE_SECONDS", 300, minimum=60)

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
WEBHOOK_MAX_BODY_BYTES = _get_int("WEBHOOK_MAX_BODY_BYTES", 65536, minimum=1024)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
INTERNAL_METRICS_TOKEN = os.getenv("INTERNAL_METRICS_TOKEN", "").strip()

# Support
SUPPORT_URL = os.getenv("SUPPORT_URL")

# Admin notifications
@lru_cache(maxsize=1)
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
@lru_cache(maxsize=1)
def get_tariffs() -> dict[str, dict[str, int | str]]:
    """Return the immutable tariff configuration loaded at process start."""
    return {
        "14_days": {
            "days": 14,
            "stars_price": _get_int("TARIFF_14_DAYS_STARS", 100, minimum=1),
            "rub_price": _get_int("TARIFF_14_DAYS_RUB", 150, minimum=1),
            "name": "2 недели",
            "description": "Доступ на 2 недели",
        },
        "30_days": {
            "days": 30,
            "stars_price": _get_int("TARIFF_30_DAYS_STARS", 200, minimum=1),
            "rub_price": _get_int("TARIFF_30_DAYS_RUB", 300, minimum=1),
            "name": "1 месяц",
            "description": "Доступ на 1 месяц",
        },
        "90_days": {
            "days": 90,
            "stars_price": _get_int("TARIFF_90_DAYS_STARS", 500, minimum=1),
            "rub_price": _get_int("TARIFF_90_DAYS_RUB", 800, minimum=1),
            "name": "3 месяца",
            "description": "Доступ на 3 месяца",
        },
    }


# Initialize tariffs on import
TARIFFS = get_tariffs()
