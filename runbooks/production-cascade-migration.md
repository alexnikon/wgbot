# Production migration from WGDashboard to Cascade

This runbook moves the existing AWG2 interface and bot data without regenerating
client keys. Execute one checkpoint at a time. Do not continue when a check fails.

## Variables

Set these values in the maintenance shell. Do not store API tokens in shell history.

```bash
export WGBOT_ROOT=/home/alex/wgbot
export WGD_ROOT=/home/alex/wgdashboard
export CASCADE_ROOT=/home/alex/cascade
export MIGRATION_ROOT=/home/alex/migration/wgdashboard-to-cascade
export CASCADE_SERVER_KEY=production-1
export CASCADE_GROUP_ID=replace-with-basic-group-uuid
export TEMPORARY_UDP_PORT=51900
export LEGACY_UDP_PORT=47393
export MIGRATION_IMAGE=ghcr.io/alexnikon/wgbot:dev-REVIEWED_COMMIT_SHA
```

Run migration commands from the reviewed application image. This avoids depending
on a host Python/uv installation and pins tooling to the tested commit:

```bash
migrate() {
  docker run --rm --network host \
    -e CASCADE_SERVERS_FILE=/run/secrets/cascade_servers.json \
    -v "$WGBOT_ROOT/secrets:/run/secrets:ro" \
    -v "$MIGRATION_ROOT:/migration" \
    "$MIGRATION_IMAGE" python /app/migrate_to_cascade.py "$@"
}
docker image inspect "$MIGRATION_IMAGE" >/dev/null
```

The Cascade token, hidden admin URL, default interface ID, and capacity settings
belong in `$WGBOT_ROOT/secrets/cascade_servers.json`, mode `0600`. The migration
interface ID is written to migrated `client_peers`; it must not replace the default
interface ID used for new users.

Resolve and verify the configured default interface and Basic group without exposing
the API token:

```bash
cd "$WGBOT_ROOT"
migrate inspect-server \
  --server-key "$CASCADE_SERVER_KEY"
```

## Checkpoint 1: preflight

Keep the old bot, nginx, and WGDashboard running.

```bash
cd "$WGBOT_ROOT"
git status --short
docker compose ps
curl -fsS http://127.0.0.1:8001/health
sudo ss -lntup | grep -E ':(80|443|47393|51821|51900) '
sudo test -r "$WGD_ROOT/data/db/wgdashboard.db"
sudo test -r "$WGD_ROOT/data/db/wgdashboard_job.db"
sudo test -r "$WGD_ROOT/conf/awg0.conf"
test -r "$WGBOT_ROOT/DB/wgbot.db"
test -r "$WGBOT_ROOT/clients.json"
test -r "$WGBOT_ROOT/secrets/cascade_servers.json"
```

Confirm that the temporary UDP port is unused and that no payment is waiting for
completion. Announce the maintenance window before the final backup.

## Checkpoint 2: backups

```bash
sudo install -d -m 700 -o alex -g alex "$MIGRATION_ROOT/sources"
cd "$WGBOT_ROOT"
python3 scripts/backup_runtime.py --root "$WGBOT_ROOT" --label pre-cascade
sudo cp --preserve=mode,timestamps "$WGD_ROOT/data/db/wgdashboard.db" "$MIGRATION_ROOT/sources/"
sudo cp --preserve=mode,timestamps "$WGD_ROOT/data/db/wgdashboard_job.db" "$MIGRATION_ROOT/sources/"
sudo cp --preserve=mode,timestamps "$WGD_ROOT/conf/awg0.conf" "$MIGRATION_ROOT/sources/"
cp --preserve=mode,timestamps "$WGBOT_ROOT/DB/wgbot.db" "$MIGRATION_ROOT/sources/"
cp --preserve=mode,timestamps "$WGBOT_ROOT/clients.json" "$MIGRATION_ROOT/sources/"
chmod -R go-rwx "$MIGRATION_ROOT"
```

Create an encrypted Cascade system backup from its API/UI and store it outside the
deployment directory. Verify that all source files are non-empty before continuing.

## Checkpoint 3: read-only analysis

Run the key validation tool on the host where `awg pubkey` is available.

```bash
migrate analyze \
  --wg-database /migration/sources/wgdashboard.db \
  --jobs-database /migration/sources/wgdashboard_job.db \
  --wg-config /migration/sources/awg0.conf \
  --bot-database /migration/sources/wgbot.db \
  --clients-json /migration/sources/clients.json \
  --report /migration/analysis.json
```

Record the peer, enabled, disabled, private-key, and Telegram-owner counts from the
final report. They may change while production remains active. Resolve every
`conflict` before continuing. Warnings require explicit review but do not modify
source data.

When the bot database and `clients.json` disagree about the primary peer, create a
mode-600 resolution file instead of editing either source:

```json
{
  "primary_role_source": {
    "TELEGRAM_ID": "bot_database"
  }
}
```

Pass it to both `analyze` and `prepare` with `--resolutions`. Accepted values are
`bot_database` and `clients_json`; every unresolved disagreement remains a blocker.
Store it as `$MIGRATION_ROOT/resolutions.json` with mode `600`, then repeat analysis
with `--resolutions /migration/resolutions.json`.

## Checkpoint 4: prepare protected import files

```bash
migrate prepare \
  --wg-database /migration/sources/wgdashboard.db \
  --jobs-database /migration/sources/wgdashboard_job.db \
  --wg-config /migration/sources/awg0.conf \
  --bot-database /migration/sources/wgbot.db \
  --clients-json /migration/sources/clients.json \
  --resolutions /migration/resolutions.json \
  --group-id "$CASCADE_GROUP_ID" \
  --payload /migration/cascade-native-import.json \
  --manifest /migration/manifest.json \
  --report /migration/analysis.json
stat -c '%a %n' "$MIGRATION_ROOT"/*.json
```

Every generated JSON file must be mode `600`. The native import contains private
keys and must never be committed, uploaded to CI, or pasted into chat/logs.

## Checkpoint 5: Caddy webhook route

Patch Cascade Caddy while the old bot is still listening on `127.0.0.1:8001`.

```bash
cd "$WGBOT_ROOT"
python3 scripts/install_cascade_caddy_webhook.py \
  "$CASCADE_ROOT/deploy/caddy/Caddyfile"
python3 scripts/install_cascade_caddy_webhook.py --check \
  "$CASCADE_ROOT/deploy/caddy/Caddyfile"
cd "$CASCADE_ROOT/deploy/caddy"
docker compose config --quiet
```

Stop nginx only when the patched Caddy configuration is ready. Start/recreate
`cascade-caddy`, then verify both Cascade UI and the webhook before stopping wgbot:

```bash
curl -fsS https://PUBLIC_HOST/webhook/yookassa/health
```

Re-run the patch `--check` after every Cascade update.

## Checkpoint 6: dry-run and import

```bash
cd "$WGBOT_ROOT"
migrate import \
  --server-key "$CASCADE_SERVER_KEY" \
  --payload /migration/cascade-native-import.json \
  --listen-port "$TEMPORARY_UDP_PORT" \
  --receipt /migration/import-receipt.json
```

After approving the dry-run, stop only wgbot to freeze its database, repeat the
SQLite backup, and run the same command with `--apply`. Record the returned migration
interface ID as `MIGRATION_INTERFACE_ID`.

## Checkpoint 7: bind and verify

Use a copy of the final bot database first:

```bash
cp "$WGBOT_ROOT/DB/wgbot.db" "$MIGRATION_ROOT/wgbot-cascade.db"
migrate bind \
  --server-key "$CASCADE_SERVER_KEY" \
  --interface-id "$MIGRATION_INTERFACE_ID" \
  --manifest /migration/manifest.json \
  --database /migration/wgbot-cascade.db
migrate bind --apply \
  --server-key "$CASCADE_SERVER_KEY" \
  --interface-id "$MIGRATION_INTERFACE_ID" \
  --manifest /migration/manifest.json \
  --database /migration/wgbot-cascade.db
migrate verify \
  --server-key "$CASCADE_SERVER_KEY" \
  --interface-id "$MIGRATION_INTERFACE_ID" \
  --manifest /migration/manifest.json \
  --database /migration/wgbot-cascade.db
```

Replace the production bot database only after verification succeeds and retain the
original backup. Preserve owner and mode expected by the container.

## Checkpoint 8: VPN cutover

1. Verify all imported peers and the Basic client group in Cascade.
2. Stop the old `awg0` interface/WGDashboard VPN service.
3. Confirm UDP `47393` is free.
4. Patch the migration interface listen port from the temporary port to `47393`.
5. Start/restart the migration interface.
6. Test an existing configuration from an external client.
7. Verify one enabled and one disabled peer.

Do not run WGDashboard and Cascade simultaneously with the same server key, subnet,
or listen port.

## Checkpoint 9: bot deployment and E2E

Deploy the reviewed production image, then verify:

```bash
docker compose ps
docker compose logs --tail=200 wgbot
curl -fsS http://127.0.0.1:8001/health
curl -fsS https://PUBLIC_HOST/webhook/yookassa/health
```

Test `/start`, status, old configuration connectivity, configuration download,
renewal, YooKassa, Stars, expiration notifications, and a new Basic peer on the
default interface. A migrated user must remain on the migration interface.

## Rollback

If any gate fails after cutover:

1. Stop the new bot and migration interface.
2. Restore the pre-migration bot database.
3. Restore UDP `47393` to WGDashboard and start the old `awg0` service.
4. Run the previous production bot image.
5. Verify webhook routing and an existing client configuration.

Retain WGDashboard, source databases, old images, and encrypted backups for at least
72 hours. Delete the plaintext native import and manifest after the rollback window.
