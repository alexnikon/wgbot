# WGBot

### Sell and manage VPN access directly in Telegram

WGBot turns Telegram into a simple storefront for your VPN service. Customers can
choose a plan, pay, receive their configuration, and manage their subscription
without leaving the chat.

The bot connects Telegram payments, YooKassa, and
[Cascade](https://github.com/JohnnyVBut/cascade) in one smooth customer journey.

## Why WGBot

- **Fewer steps to purchase.** Plans, payment, setup instructions, and access are
  available in one familiar interface.
- **Instant delivery.** A customer receives an AmneziaWG configuration after a
  successful payment.
- **Flexible payments.** Support for Telegram Stars and bank cards through YooKassa.
- **Easy renewals.** Additional paid time is added to an active subscription.
- **Clear subscription status.** Customers can check connected devices, remaining
  time, and expiration details at any moment.
- **Helpful reminders.** The bot notifies customers before access expires and makes
  renewal easy.
- **Built-in administration.** Manage customers, access periods, discounts,
  configurations, payments, refunds, and broadcasts from Telegram.

## Customer experience

1. Open the bot and choose an access period.
2. Pay with Telegram Stars or a bank card.
3. Download the personal configuration.
4. Import it into AmneziaWG and connect.
5. Return to the bot whenever you need to renew access or check the subscription.

Each configuration is intended for one device. A subscription can include multiple
device configurations, while administrators retain control over limits and access.

## Designed for reliable operation

WGBot keeps payment and subscription state in SQLite, synchronizes access with
Cascade, retries temporary provisioning failures, and preserves the customer journey
across restarts. Persistent Telegram panels keep chats tidy instead of creating a new
message for every action.

## Quick start

Prepare the environment and Cascade server registry:

```bash
cp env.docker.example .env
mkdir -p DB secrets
cp cascade_servers.example.json secrets/cascade_servers.json
```

Add your Telegram, YooKassa, tariff, support, administrator, and Cascade settings,
then start the service:

```bash
docker compose up -d --build
docker compose logs -f wgbot
```

The project is designed for self-hosted Docker deployment behind an HTTPS reverse
proxy. Keep `.env`, the database, and the server registry private and backed up.

## Validation

```bash
uv sync --frozen
uv run ruff check .
uv run python -m unittest discover -s tests
```

WGBot is a practical foundation for a subscription-based VPN service that feels
simple to customers and stays manageable for operators.
