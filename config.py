import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# WGDashboard API Configuration
WG_DASHBOARD_URL = os.getenv('WG_DASHBOARD_URL', 'http://localhost:10086')
WG_DASHBOARD_API_KEY = os.getenv('WG_DASHBOARD_API_KEY')
WG_CONFIG_NAME = os.getenv('WG_CONFIG_NAME', 'awg0')

# Database Configuration
DATABASE_FILE = 'data/wgbot.db'

# Peer Configuration
PEER_EXPIRY_DAYS = 30

# Payment Configuration
# YooKassa Configuration
YOOKASSA_SHOP_ID = os.getenv('YOOKASSA_SHOP_ID')
YOOKASSA_SECRET_KEY = os.getenv('YOOKASSA_SECRET_KEY')

# Webhook Configuration
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
DOMAIN = os.getenv('DOMAIN')

# Support
SUPPORT_URL = os.getenv('SUPPORT_URL', 'https://t.me/straycat0789')

# Tariff Configuration (env-driven with sensible defaults)
TARIFF_14_DAYS_STARS = int(os.getenv('TARIFF_14_DAYS_STARS', 100))
TARIFF_14_DAYS_RUB = int(os.getenv('TARIFF_14_DAYS_RUB', 150))
TARIFF_30_DAYS_STARS = int(os.getenv('TARIFF_30_DAYS_STARS', 200))
TARIFF_30_DAYS_RUB = int(os.getenv('TARIFF_30_DAYS_RUB', 300))

TARIFFS = {
    '14_days': {
        'days': 14,
        'stars_price': TARIFF_14_DAYS_STARS,
        'rub_price': TARIFF_14_DAYS_RUB,
        'name': '14 дней',
        'description': 'Доступ на 2 недели'
    },
    '30_days': {
        'days': 30,
        'stars_price': TARIFF_30_DAYS_STARS,
        'rub_price': TARIFF_30_DAYS_RUB,
        'name': '30 дней',
        'description': 'Доступ на месяц'
    }
}
