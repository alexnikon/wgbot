# wgbot

Telegram bot for selling and managing nikonVPN access through WGDashboard, Telegram Stars, and YooKassa.

## Run Locally

Create the environment file:

```bash
cp env.docker.example .env
```

Edit `.env` and set the required values:

```env
TELEGRAM_BOT_TOKEN=
WG_DASHBOARD_URL=http://your-wgdashboard:10086
WG_DASHBOARD_API_KEY=
WG_CONFIG_NAME=awg0
SUPPORT_URL=
CLIENTS_JSON_PATH=clients.json
ADMIN_TELEGRAM_IDS=123456789
```

Optional YooKassa settings:

```env
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=
WEBHOOK_URL=https://your-domain.com/webhook/yookassa
DOMAIN=your-domain.com
```

Optional tariff overrides:

```env
TARIFF_14_DAYS_STARS=
TARIFF_14_DAYS_RUB=
TARIFF_30_DAYS_STARS=
TARIFF_30_DAYS_RUB=
TARIFF_90_DAYS_STARS=
TARIFF_90_DAYS_RUB=
TARIFF_180_DAYS_ENABLED=false
TARIFF_180_DAYS_STARS=
TARIFF_180_DAYS_RUB=
```

## clients.json

`clients.json` is the editable registry for clients, discounts, and additional WGDashboard peers.

```json
{
  "version": 1,
  "clients": [
    {
      "telegramId": 123456789,
      "username": "client_username",
      "promo": 0,
      "peers": [
        {
          "role": "bot",
          "clientId": "client_username",
          "publicKey": "botPeerPublicKey"
        },
        {
          "role": "manual",
          "clientId": "iPhone",
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

Notes:

- `promo: 20` means a 20% discount.
- `promo: 150` means 150% of the base price.
- `role: "bot"` is the peer created by the bot.
- `role: "manual"` is an additional peer created manually in WGDashboard.
- Empty manual peers are ignored.

## Admin

Set admin Telegram IDs in `.env`:

```env
ADMIN_TELEGRAM_IDS=123456789,987654321
```

Admins must open the bot and press `/start` once before the bot can send them private notifications.

Admin features:

- payment notifications;
- broadcast to all clients;
- direct message to a selected client.
