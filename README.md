# wgbot

Telegram bot for selling and managing nikonVPN access through Cascade, Telegram Stars, and YooKassa.

Cascade integration follows the documented [Cascade REST API](https://github.com/JohnnyVBut/cascade/blob/master/docs/API.en.md).

## Configuration

Create the environment file:

```bash
cp env.docker.example .env
```

Set the Telegram bot, YooKassa, webhook, tariff, support, and admin values in `.env`.
Set `PAYMENT_RETURN_URL` to the matching production or development Telegram bot URL.

Create the protected Cascade registry:

```bash
mkdir -p secrets
cp cascade_servers.example.json secrets/cascade_servers.json
chmod 600 secrets/cascade_servers.json
```

Each server entry requires a stable `server_key`, Cascade URL including the hidden admin path, API token, target interface UUID, priority, and `max_peers`. Servers are tried in ascending priority order. Tokens and real server data must not be committed.

Keep disabled server entries in the registry while any client is assigned to them. `enabled: false` stops new placement but still allows existing clients to download configs and synchronize expiration.

## Run

```bash
docker compose up -d --build
docker compose logs -f wgbot
curl -fsS http://localhost:8001/health
```

## Existing Client Migration

Import existing peers into the designated Cascade migration interface with their original keys first. Then inspect the mapping:

```bash
docker cp ./clients.json wgbot:/tmp/clients.json
docker compose exec wgbot python migrate_to_cascade.py \
  --server-key cascade-1 \
  --clients-json /tmp/clients.json
```

Apply only after every missing or conflicting public key has been reviewed:

```bash
docker compose exec wgbot python migrate_to_cascade.py \
  --server-key cascade-1 \
  --clients-json /tmp/clients.json \
  --apply
```

The migration matches by peer public key and never creates missing Cascade peers.

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
- `cascade-migration`: CI and deployment through the GitHub `dev` environment.
- Development uses an independent Telegram bot, YooKassa credentials, webhook domain, database, and VPS.
- Rollback remains manual through the existing `Rollback` workflow.

Repository variables for each GitHub Environment: `VPS_HOST`, `VPS_USER`, `VPS_PORT`, and `DEPLOY_PATH`. Store only `VPS_SSH_KEY` as an environment secret.

Before the first development deployment, prepare a clean checkout on the development VPS with its own `.env`, `secrets/cascade_servers.json`, empty `DB/`, TLS files, domain, Telegram bot token, and YooKassa credentials. Create the GitHub Environment named `dev` with the deployment variables and SSH key above.
