# wgbot

Telegram-бот для продажи VPN-доступа с интеграцией WGDashboard, Telegram Stars и YooKassa.

## Быстрый запуск

1. Подготовьте `.env`:

```bash
cp env.docker.example .env
```

2. Заполните обязательные параметры в `.env`:

```env
# Telegram
TELEGRAM_BOT_TOKEN=

# WGDashboard
WG_DASHBOARD_URL=http://your-wg-dashboard:10086
WG_DASHBOARD_API_KEY=
WG_CONFIG_NAME=awg0

# YooKassa (если используете оплату картой)
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=

# Webhook / домен (для YooKassa)
WEBHOOK_URL=https://your-domain.com/webhook/yookassa
DOMAIN=your-domain.com

# Ссылки и файлы
SUPPORT_URL=
CLIENTS_JSON_PATH=clients.json
CUSTOM_CLIENTS_PATH=custom_clients.txt
PROMO_FILE_PATH=promo.txt
```

3. При необходимости настройте тарифы в `.env`:

```env
TARIFF_14_DAYS_STARS=100
TARIFF_14_DAYS_RUB=150
TARIFF_30_DAYS_STARS=200
TARIFF_30_DAYS_RUB=300
TARIFF_90_DAYS_STARS=500
TARIFF_90_DAYS_RUB=800
TARIFF_180_DAYS_STARS=900
TARIFF_180_DAYS_RUB=1500
```

4. Запустите контейнеры:

```bash
docker-compose up -d --build
```

## Управление

```bash
# Логи
docker-compose logs -f

# Перезапуск
docker-compose restart

# Остановка
docker-compose down
```

## Дополнительные файлы

### `promo.txt`

Персональные скидки/наценки.

Пример:

```txt
123456789=20
```

### `custom_clients.txt`

Ручная привязка дополнительных peer из WGDashboard к `telegram_id` пользователя.
Используется для управления несколькими устройствами одного пользователя.

Форматы строк:

```txt
123456789=peerPublicKey1,peerPublicKey2
123456789:peerPublicKey3 peerPublicKey4
```

Пустые строки и строки с `#` игнорируются.
