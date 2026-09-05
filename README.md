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
device configurations, while administrators retain control over configuration and
access lifecycle.

## Designed for reliable operation

WGBot keeps payment and subscription state in SQLite independently from device
configurations, synchronizes existing access with Cascade, and preserves the customer
journey across restarts. Persistent Telegram panels keep chats tidy instead of
creating a new message for every action.

## Quick start

Prepare the environment and Cascade server registry:

```bash
cp env.docker.example .env
mkdir -p DB secrets
cp cascade_servers.example.json secrets/cascade_servers.json
```

Each server entry uses `client_group` as the default for a client's first explicitly
created configuration. `assignable_client_groups` is the protected allowlist shown to
administrators when creating configurations or changing a client's unified group.
Every allowlisted group must already exist on the corresponding Cascade server.

Use `client_interfaces` to offer protocol versions after customers choose a
location. Each entry contains `interface_id` (the exact Cascade interface ID),
`name` (the button label), and `description` (a short customer-facing explanation).
An ID is an opaque string such as `wg13`, not necessarily a UUID. Read the `id`
field from `GET /api/tunnel-interfaces`; do not use its editable `name` field.
See `cascade_servers.example.json`; replace the example IDs with real IDs and
localize the labels and descriptions for your customers. Labels must match the
actual protocol versions configured in Cascade. Renaming an interface does not
affect selection or annotations. The bot checks that the selected ID exists and
is allowed before creating a peer; missing or ambiguous IDs block creation.

Omitting `client_interfaces` preserves the legacy single-`interface_id` flow. An
empty list disables customer creation at that location. Keep the existing
`interface_id` for default API operations and compatibility. The list supports up
to 10 entries; interface IDs and labels allow 64 characters, descriptions 240.
All fields must be non-empty printable strings, with unique interface IDs.
Restart the bot after changing the registry. Existing configurations retain their
stored IDs, and the three-configuration limit applies across all versions.

The former `client_interfaces.interface_name` format is rejected. Back up the
registry and migrate it together with the application update: the previous
application cannot read the new list format. Verify the server-specific IDs via
Cascade before switching, and keep the matching application and registry for
rollback. Customers with unfinished creation flows must restart their selection.

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
