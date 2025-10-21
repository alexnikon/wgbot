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

# Tariff Configuration
TARIFFS = {
    '14_days': {
        'days': 14,
        'stars_price': 100,  # 100 звезд
        'rub_price': 150,    # 150 рублей
        'name': '14 дней',
        'description': 'Доступ на 2 недели'
    },
    '30_days': {
        'days': 30,
        'stars_price': 200,  # 200 звезд
        'rub_price': 300,    # 300 рублей
        'name': '30 дней',
        'description': 'Доступ на месяц'
    }
    # Тариф 90 дней временно отключен
    # '90_days': {
    #     'days': 90,
    #     'stars_price': 500,  # 500 звезд
    #     'rub_price': 750,    # 750 рублей
    #     'name': '90 дней',
    #     'description': 'Доступ на 3 месяца'
    # }
}
