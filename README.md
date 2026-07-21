# wgbot

Telegram bot for selling and managing nikonVPN access through Cascade, Telegram Stars, and YooKassa.

Cascade integration follows the documented [Cascade REST API](https://github.com/JohnnyVBut/cascade/blob/master/docs/API.en.md).

Telegram handlers are grouped by feature under `handlers/`: navigation, access,
payments, administration, and the final fallback router. `bot.py` owns shared UI
operations, dependency wiring, middleware, and background worker startup.

## Configuration

Create the environment file:

```bash
cp env.docker.example .env
```

Set the Telegram bot, YooKassa, webhook, tariff, support, and admin values in `.env`.
Set `PAYMENT_RETURN_URL` to the matching production or development Telegram bot URL.
Deployment backups use SQLite's online backup API. `BACKUP_RETENTION_DAYS=30`
removes older managed backups, while `BACKUP_MAX_FILES=20` keeps at most that many
database backups. Set either value to `0` to disable that limit.
Deploy workflows run the backup automatically. For daily backups between deployments,
add a host cron entry and replace the path with the runtime directory:

```cron
15 3 * * * mkdir -p /home/alex/wgbot/backups && cd /home/alex/wgbot && /usr/bin/python3 scripts/backup_runtime.py --root /home/alex/wgbot --label scheduled >> /home/alex/wgbot/backups/backup.log 2>&1
```

Retention is applied per source file. Keep a separate encrypted off-site backup as well;
local retention protects against application mistakes, but not loss of the VPS.

Create the protected Cascade registry:

```bash
mkdir -p secrets
cp cascade_servers.example.json secrets/cascade_servers.json
chmod 600 secrets/cascade_servers.json
```

Each server entry requires a stable `server_key`, Cascade URL including the hidden admin path, API token, target interface UUID, priority, `max_peers`, and `client_group`. New peers are assigned to that Cascade client group (`Basic` by default). Servers are tried in ascending priority order. Tokens and real server data must not be committed.

Keep disabled server entries in the registry while any client is assigned to them. `enabled: false` stops new placement but still allows existing clients to download configs and synchronize expiration.

## Run

```bash
mkdir -p DB
sudo chown -R 1000:1000 DB
docker compose up -d --build
docker compose logs -f wgbot
curl -fsS http://localhost:8001/health
```

The container runs as UID/GID `1000`, has a read-only root filesystem, and exposes the webhook only on `127.0.0.1:8001`. Terminate TLS with the host's Caddy instance. Example:

```caddyfile
vpn-bot.example.com {
    reverse_proxy 127.0.0.1:8001
}
```

Do not expose port `8001` publicly. Allow public inbound traffic only to Caddy on ports `80/443`.

Set `INTERNAL_METRICS_TOKEN` to a long random value to enable protected operational
diagnostics. The endpoint contains counters and queue gauges, but no API tokens or payment
data:

```bash
curl -H "Authorization: Bearer $INTERNAL_METRICS_TOKEN" \
  http://127.0.0.1:8001/internal/metrics
```

Leave `INTERNAL_METRICS_TOKEN` empty to disable the endpoint with a `404` response.

## Existing Client Migration

The staged migration CLI reads WGDashboard, its jobs database, the legacy bot
database, `clients.json`, and the original AWG2 server configuration. It supports
`analyze`, `prepare`, `import`, `bind`, and `verify`. Source databases are opened
read-only during analysis, while every mutating stage requires an explicit `--apply`.

Follow [the production migration runbook](runbooks/production-cascade-migration.md).
Generated native Cascade imports contain private keys and must remain outside the
repository with mode `0600`.

## Development Validation

Check health, authentication, interfaces, and capacity without changing peers:

```bash
docker compose exec wgbot python cascade_smoke_test.py
```

Exercise create, get, update, disable, enable, config, and delete on every configured dev interface:

```bash
docker compose exec wgbot python cascade_smoke_test.py --exercise-peer
```

Use `--exercise-peer` only on development interfaces. The temporary peer is deleted in a cleanup block.

## CI/CD

- `main`: production CI and deployment.
- Rollback remains manual through the existing `Rollback` workflow.

Production deployment is artifact-only: GitHub builds the image and uploads only
`docker-compose.yml` and `scripts/backup_runtime.py`. The VPS does not need a Git
checkout after the first successful runtime deployment.

Repository variables for the production GitHub Environment: `VPS_HOST`, `VPS_USER`,
`VPS_PORT`, and `DEPLOY_PATH`. Set `DEPLOY_PATH=/home/alex/wgbot`. The runtime directory
must contain `.env`, `DB/wgbot.db`, and `secrets/cascade_servers.json`; deployments upload
the Compose file and backup script automatically. Environment secrets: `VPS_SSH_KEY`
and `VPS_KNOWN_HOSTS`. Generate the pinned host entry from a trusted workstation and
verify its fingerprint before saving it:

```bash
ssh-keyscan -p 22 example.com
```

Restrict the `production` GitHub Environment to the `main` branch and require a reviewer.

## Dependency Updates

Dependencies are declared in `pyproject.toml` and fully pinned in `uv.lock`.

```bash
uv lock --upgrade
uv sync --frozen
uv run ruff check .
uv run python -m unittest discover -s tests -v
```

## Telegram Runtime Controls

The polling process preserves pending updates across restarts and limits concurrently
executing updates. The following optional environment variables are validated at startup:

```bash
TELEGRAM_TASKS_CONCURRENCY_LIMIT=100
STARS_RECONCILIATION_INTERVAL_SECONDS=3600
LOG_TELEGRAM_CONTENT=false
```

Keep `LOG_TELEGRAM_CONTENT` disabled in production. When enabled, debug previews are
still passed through credential redaction.

Administrative Telegram flows are persisted in SQLite for 24 hours. Telegram Stars
payments use a local intent before an invoice is sent, and the hourly reconciliation
worker compares these intents with `getStarTransactions`. Refund events are recorded
for manual access review; they never shorten VPN access automatically.

Admin command aliases are `/clients`, `/broadcast`, `/payments`, `/stars_reconcile`,
and `/refund_stars <telegram_charge_id>`.
