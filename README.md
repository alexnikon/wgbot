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

# Админ-уведомления
ADMIN_TELEGRAM_IDS=123456789
```

3. При необходимости настройте тарифы в `.env`:

```env
TARIFF_14_DAYS_STARS=
TARIFF_14_DAYS_RUB=
TARIFF_30_DAYS_STARS=
TARIFF_30_DAYS_RUB=
TARIFF_90_DAYS_STARS=
TARIFF_90_DAYS_RUB=
TARIFF_180_DAYS_STARS=
TARIFF_180_DAYS_RUB=
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

## Админ-уведомления

Укажите один или несколько Telegram ID через запятую:

```env
ADMIN_TELEGRAM_IDS=123456789,987654321
```

Админ должен один раз открыть бота в Telegram и нажать `/start`, иначе Telegram не разрешит боту отправить личное сообщение.

## `clients.json`

Единый файл управления клиентами, скидками и дополнительными устройствами.

```json
{
  "version": 1,
  "clients": [
    {
      "telegramId": ,
      "username": "",
      "promo": 0,
      "peers": [
        {
          "role": "bot",
          "clientId": "",
          "publicKey": "botPeerPublicKey"
        },
        {
          "role": "manual",
          "clientId": "",
          "publicKey": "manualPeerPublicKey",
          "jobId": "optional-created-by-bot"
        },
        {
          "role": "manual",
          "clientId": "",
          "publicKey": ""
        }
      ]
    }
  ]
}
```

`promo` указывается в процентах: `20` означает скидку 20%, `150` означает цену 150% от базовой. `role: "bot"` используется для peer, созданного ботом. `role: "manual"` используется для дополнительных peer из WGDashboard. Пустые `clientId` и `publicKey` у manual peer игнорируются.
