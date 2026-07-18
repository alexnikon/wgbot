import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cascade_api import (
    CascadeAPI,
    CascadeError,
    CascadeNotFound,
    load_cascade_servers,
    to_rfc3339,
)
from database import Database


class MigrationError(RuntimeError):
    """Raised when migration input is incomplete or inconsistent."""


@dataclass(frozen=True)
class SourcePaths:
    wg_database: Path
    jobs_database: Path
    wg_config: Path
    bot_database: Path
    clients_json: Path | None


def load_resolutions(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"primary_role_source": {}}
    data = load_json(path, require_format=False)
    primary_sources = data.get("primary_role_source", {})
    if not isinstance(primary_sources, dict) or any(
        value not in {"bot_database", "clients_json"}
        for value in primary_sources.values()
    ):
        raise MigrationError(
            "resolution primary_role_source values must be bot_database or clients_json"
        )
    return {"primary_role_source": primary_sources}


def fingerprint(value: str) -> str:
    """Return a safe identifier suitable for reports and logs."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def protected_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


@contextmanager
def open_read_only(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise MigrationError(f"Required SQLite database not found: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def parse_interface_config(path: Path) -> dict[str, str]:
    """Parse only the server Interface section from a WireGuard config."""
    if not path.is_file():
        raise MigrationError(f"WireGuard configuration not found: {path}")
    values: dict[str, str] = {}
    in_interface = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_interface = line.casefold() == "[interface]"
            if values and not in_interface:
                break
            continue
        if in_interface and "=" in line:
            key, value = line.split("=", 1)
            normalized_key = key.strip().casefold()
            normalized_value = value.strip()
            if normalized_key == "address" and values.get(normalized_key):
                values[normalized_key] += f",{normalized_value}"
            else:
                values[normalized_key] = normalized_value
    required = ("privatekey", "address", "listenport")
    missing = [item for item in required if not values.get(item)]
    if missing:
        raise MigrationError(
            f"WireGuard Interface section is missing: {', '.join(missing)}"
        )
    return values


def derive_x25519_public_key(private_key: str) -> str:
    """Derive a WireGuard public key using the RFC 7748 X25519 ladder."""
    validate_key_shape(private_key, "private key")
    scalar_bytes = bytearray(base64.b64decode(private_key))
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 127
    scalar_bytes[31] |= 64
    scalar = int.from_bytes(scalar_bytes, "little")
    prime = 2**255 - 19
    x_1 = 9
    x_2, z_2 = 1, 0
    x_3, z_3 = 9, 1
    swap = 0
    for position in range(254, -1, -1):
        bit = (scalar >> position) & 1
        swap ^= bit
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = bit
        a = (x_2 + z_2) % prime
        aa = (a * a) % prime
        b = (x_2 - z_2) % prime
        bb = (b * b) % prime
        e = (aa - bb) % prime
        c = (x_3 + z_3) % prime
        d = (x_3 - z_3) % prime
        da = (d * a) % prime
        cb = (c * b) % prime
        x_3 = ((da + cb) ** 2) % prime
        z_3 = (x_1 * ((da - cb) ** 2)) % prime
        x_2 = (aa * bb) % prime
        z_2 = (e * (aa + 121665 * e)) % prime
    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2
    public_value = (x_2 * pow(z_2, prime - 2, prime)) % prime
    return base64.b64encode(public_value.to_bytes(32, "little")).decode()


def derive_public_key(private_key: str, executable: str = "internal") -> str:
    """Derive a public key without exposing the private key in argv or output."""
    if executable == "internal":
        return derive_x25519_public_key(private_key)
    try:
        result = subprocess.run(
            [executable, "pubkey"],
            input=(private_key + "\n").encode(),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MigrationError(f"Key validation tool not found: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MigrationError(f"Key validation tool timed out: {executable}") from exc
    if result.returncode:
        raise MigrationError(f"Key validation failed using {executable}")
    public_key = result.stdout.decode().strip()
    validate_key_shape(public_key, "derived public key")
    return public_key


def validate_key_shape(value: str, label: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise MigrationError(f"Invalid {label} encoding") from exc
    if len(decoded) != 32:
        raise MigrationError(f"Invalid {label} length")


def load_client_registry(path: Path | None) -> list[dict[str, Any]]:
    """Read both supported clients.json layouts without modifying the file."""
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(
            f"Invalid clients.json at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if isinstance(data, dict) and isinstance(data.get("clients"), list):
        result: list[dict[str, Any]] = []
        for client in data["clients"]:
            try:
                user_id = int(client.get("telegramId"))
            except (TypeError, ValueError):
                continue
            peers = client.get("peers") or []
            for peer in peers:
                public_key = str(peer.get("publicKey") or "").strip()
                if not public_key:
                    continue
                result.append(
                    {
                        "telegram_user_id": user_id,
                        "telegram_username": str(client.get("username") or "")
                        .strip()
                        .lstrip("@"),
                        "promo": int(client.get("promo") or 0),
                        "public_key": public_key,
                        "peer_name": str(peer.get("clientId") or "").strip(),
                        "role": "primary" if peer.get("role") == "bot" else "manual",
                    }
                )
        return result
    if isinstance(data, list):
        return [
            {
                "public_key": str(item.get("publicKey") or "").strip(),
                "peer_name": str(item.get("clientId") or "").strip(),
                "role": "manual",
            }
            for item in data
            if isinstance(item, dict) and item.get("publicKey")
        ]
    raise MigrationError("Unsupported clients.json format")


def load_wg_peers(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    peers: list[dict[str, Any]] = []
    for table, enabled in (("awg0", True), ("awg0_restrict_access", False)):
        if not table_exists(connection, table):
            raise MigrationError(f"WGDashboard table not found: {table}")
        rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
        for row in rows:
            item = dict(row)
            public_key = str(item.get("id") or "").strip()
            validate_key_shape(public_key, "peer public key")
            peers.append(
                {
                    "public_key": public_key,
                    "private_key": str(item.get("private_key") or "").strip(),
                    "preshared_key": str(item.get("preshared_key") or "").strip(),
                    "allowed_ips": str(item.get("allowed_ip") or "").strip(),
                    "name": str(item.get("name") or "").strip() or "migrated-peer",
                    "created_at": str(item.get("created_at") or "").strip(),
                    "enabled": enabled,
                    "source_table": table,
                }
            )
    keys = [item["public_key"] for item in peers]
    if len(keys) != len(set(keys)):
        raise MigrationError("Duplicate public keys found in WGDashboard peer tables")
    return peers


def load_latest_jobs(connection: sqlite3.Connection) -> dict[str, str]:
    if not table_exists(connection, "PeerJobs"):
        raise MigrationError("WGDashboard jobs table not found: PeerJobs")
    jobs: dict[str, str] = {}
    rows = connection.execute(
        """
        SELECT Peer, Value FROM PeerJobs
        WHERE Peer IS NOT NULL AND Value IS NOT NULL AND TRIM(Value) != ''
        ORDER BY datetime(CreationDate), rowid
        """
    ).fetchall()
    for row in rows:
        jobs[str(row["Peer"]).strip()] = str(row["Value"]).strip()
    return jobs


def load_legacy_bot_clients(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(connection, "peers"):
        raise MigrationError("Legacy bot table not found: peers")
    return [dict(row) for row in connection.execute("SELECT * FROM peers ORDER BY id")]


def normalize_expiry(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return to_rfc3339(value)
    except CascadeError as exc:
        raise MigrationError(f"Invalid expiration timestamp: {value}") from exc


def effective_peer_enabled(
    source_enabled: bool, expired_at: str | None, snapshot_time: datetime
) -> bool:
    """Apply Cascade expiration semantics to the legacy enabled flag."""
    if not source_enabled:
        return False
    if not expired_at:
        return True
    normalized = datetime.fromisoformat(expired_at.replace("Z", "+00:00"))
    return normalized > snapshot_time


def peer_address(value: str) -> str:
    if not value:
        raise MigrationError("Peer is missing allowed_ip")
    first = value.split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_interface(first))
    except ValueError as exc:
        raise MigrationError(f"Invalid peer address: {first}") from exc


def select_interface_address(value: str, peers: list[dict[str, Any]]) -> str:
    """Select the IPv4 server subnet containing the migrated IPv4 peer addresses."""
    candidates = []
    for raw_address in value.split(","):
        try:
            candidate = ipaddress.ip_interface(raw_address.strip())
        except ValueError as exc:
            raise MigrationError(f"Invalid server interface address: {raw_address}") from exc
        if candidate.version == 4:
            candidates.append(candidate)
    if not candidates:
        raise MigrationError("Server configuration has no IPv4 interface address")
    peer_ips = []
    for item in peers:
        address = ipaddress.ip_interface(peer_address(item["allowed_ips"]))
        if address.version == 4:
            peer_ips.append(address.ip)
    matching = [
        candidate
        for candidate in candidates
        if all(peer_ip in candidate.network for peer_ip in peer_ips)
    ]
    if len(matching) != 1:
        raise MigrationError(
            "Could not uniquely select the IPv4 server subnet containing all peers"
        )
    return str(matching[0])


def build_snapshot(
    paths: SourcePaths,
    key_tool: str = "awg",
    resolutions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an in-memory migration snapshot while keeping every source read-only."""
    snapshot_time = datetime.now(UTC)
    interface_values = parse_interface_config(paths.wg_config)
    server_private_key = interface_values["privatekey"]
    validate_key_shape(server_private_key, "server private key")
    server_public_key = derive_public_key(server_private_key, key_tool)

    with open_read_only(paths.wg_database) as wg_db:
        wg_peers = load_wg_peers(wg_db)
    server_address = select_interface_address(interface_values["address"], wg_peers)
    with open_read_only(paths.jobs_database) as jobs_db:
        jobs = load_latest_jobs(jobs_db)
    with open_read_only(paths.bot_database) as bot_db:
        bot_clients = load_legacy_bot_clients(bot_db)
    registry = load_client_registry(paths.clients_json)
    primary_resolutions = (resolutions or {}).get("primary_role_source", {})

    bot_by_key: dict[str, dict[str, Any]] = {}
    bot_primary_by_user: dict[int, str] = {}
    for item in bot_clients:
        public_key = str(item.get("peer_id") or "").strip()
        user_id = item.get("telegram_user_id")
        if public_key and user_id is not None:
            bot_by_key[public_key] = item
            bot_primary_by_user[int(user_id)] = public_key

    registry_by_key: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for item in registry:
        public_key = item["public_key"]
        previous = registry_by_key.get(public_key)
        if previous and previous.get("telegram_user_id") != item.get("telegram_user_id"):
            conflicts.append(
                {"type": "registry-owner-conflict", "key": fingerprint(public_key)}
            )
            continue
        registry_by_key[public_key] = item
        user_id = item.get("telegram_user_id")
        if (
            item.get("role") == "primary"
            and user_id in bot_primary_by_user
            and bot_primary_by_user[int(user_id)] != public_key
        ):
            selected_source = primary_resolutions.get(str(int(user_id)))
            if selected_source not in {"bot_database", "clients_json"}:
                conflicts.append(
                    {
                        "type": "primary-key-conflict",
                        "telegram_user_id": int(user_id),
                        "bot_key": fingerprint(bot_primary_by_user[int(user_id)]),
                        "registry_key": fingerprint(public_key),
                    }
                )

    warnings: list[dict[str, Any]] = []
    prepared_peers: list[dict[str, Any]] = []
    wg_keys = {item["public_key"] for item in wg_peers}
    for public_key in registry_by_key.keys() - wg_keys:
        warnings.append(
            {"type": "registry-peer-missing-in-wg", "key": fingerprint(public_key)}
        )

    for item in wg_peers:
        public_key = item["public_key"]
        private_key = item["private_key"]
        if private_key:
            validate_key_shape(private_key, "peer private key")
            if derive_public_key(private_key, key_tool) != public_key:
                conflicts.append(
                    {"type": "private-key-mismatch", "key": fingerprint(public_key)}
                )
        else:
            warnings.append(
                {"type": "missing-private-key", "key": fingerprint(public_key)}
            )

        bot_item = bot_by_key.get(public_key)
        registry_item = registry_by_key.get(public_key)
        telegram_user_id = None
        telegram_username = ""
        promo = 0
        role = "unassigned"
        bot_expiry = None
        if bot_item:
            telegram_user_id = int(bot_item["telegram_user_id"])
            telegram_username = str(bot_item.get("telegram_username") or "").strip()
            role = "primary"
            bot_expiry = str(bot_item.get("expire_date") or "").strip() or None
        if registry_item and registry_item.get("telegram_user_id") is not None:
            registry_user_id = int(registry_item["telegram_user_id"])
            if telegram_user_id is not None and registry_user_id != telegram_user_id:
                conflicts.append(
                    {"type": "owner-conflict", "key": fingerprint(public_key)}
                )
            else:
                telegram_user_id = registry_user_id
                telegram_username = (
                    registry_item.get("telegram_username") or telegram_username
                )
                promo = int(registry_item.get("promo") or 0)
                registry_role = registry_item.get("role") or role
                selected_source = primary_resolutions.get(str(registry_user_id))
                if (
                    bot_item
                    and selected_source == "clients_json"
                    and registry_role != "primary"
                ):
                    role = "manual"
                elif bot_item:
                    role = "primary"
                elif registry_role == "primary" and selected_source == "bot_database":
                    role = "manual"
                else:
                    role = registry_role

        job_expiry = jobs.get(public_key)
        if bot_expiry and job_expiry:
            normalized_bot = normalize_expiry(bot_expiry)
            normalized_job = normalize_expiry(job_expiry)
            if normalized_bot != normalized_job:
                warnings.append(
                    {
                        "type": "expiry-source-difference",
                        "key": fingerprint(public_key),
                        "selected": "bot_database",
                    }
                )
        expiry = normalize_expiry(bot_expiry or job_expiry)
        effective_enabled = effective_peer_enabled(
            bool(item["enabled"]), expiry, snapshot_time
        )
        if bool(item["enabled"]) and not effective_enabled:
            warnings.append(
                {
                    "type": "expired-peer-enabled-in-source",
                    "key": fingerprint(public_key),
                    "selected": "disabled",
                }
            )
        prepared_peers.append(
            {
                **item,
                "address": peer_address(item["allowed_ips"]),
                "expired_at": expiry,
                "effective_enabled": effective_enabled,
                "telegram_user_id": telegram_user_id,
                "telegram_username": telegram_username,
                "promo": promo,
                "role": role,
            }
        )

    settings: dict[str, Any] = {}
    int_fields = ("jc", "jmin", "jmax", "s1", "s2", "s3", "s4")
    string_fields = ("h1", "h2", "h3", "h4", "i1", "i2", "i3", "i4", "i5")
    for field in int_fields:
        if interface_values.get(field, "").strip():
            settings[field] = int(interface_values[field])
    for field in string_fields:
        if interface_values.get(field, "").strip():
            settings[field] = interface_values[field].strip()

    return {
        "format_version": 1,
        "generated_at": snapshot_time.isoformat(),
        "interface": {
            "private_key": server_private_key,
            "public_key": server_public_key,
            "address": server_address,
            "listen_port": int(interface_values["listenport"]),
            "protocol": "amneziawg-2.0" if settings else "wireguard-1.0",
            "dns": interface_values.get("dns", ""),
            "mtu": int(interface_values.get("mtu") or 0),
            "settings": settings,
        },
        "peers": prepared_peers,
        "conflicts": conflicts,
        "warnings": warnings,
        "stats": {
            "peers": len(prepared_peers),
            "enabled": sum(bool(item["enabled"]) for item in prepared_peers),
            "disabled": sum(not bool(item["enabled"]) for item in prepared_peers),
            "effective_enabled": sum(
                bool(item["effective_enabled"]) for item in prepared_peers
            ),
            "effective_disabled": sum(
                not bool(item["effective_enabled"]) for item in prepared_peers
            ),
            "private_keys": sum(bool(item["private_key"]) for item in prepared_peers),
            "telegram_owners": len(
                {
                    item["telegram_user_id"]
                    for item in prepared_peers
                    if item["telegram_user_id"] is not None
                }
            ),
        },
    }


def safe_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "format_version": snapshot["format_version"],
        "generated_at": snapshot["generated_at"],
        "interface": {
            "public_key_fingerprint": fingerprint(snapshot["interface"]["public_key"]),
            "address": snapshot["interface"]["address"],
            "listen_port": snapshot["interface"]["listen_port"],
            "protocol": snapshot["interface"]["protocol"],
        },
        "stats": snapshot["stats"],
        "conflicts": snapshot["conflicts"],
        "warnings": snapshot["warnings"],
    }


def native_backup(snapshot: dict[str, Any], group_id: str) -> dict[str, Any]:
    interface = snapshot["interface"]
    peers = []
    for item in snapshot["peers"]:
        peers.append(
            {
                "name": item["name"],
                "publicKey": item["public_key"],
                "privateKey": item["private_key"],
                "presharedKey": item["preshared_key"],
                "allowedIPs": item["allowed_ips"],
                "address": item["address"],
                "clientAllowedIPs": "0.0.0.0/0",
                "peerType": "client",
                "endpoint": "",
                "persistentKeepalive": 21,
                "groupId": group_id,
                "expiredAt": item["expired_at"],
                "enabled": bool(item["effective_enabled"]),
                "createdAt": item["created_at"],
            }
        )
    return {
        "interface": {
            "privateKey": interface["private_key"],
            "publicKey": interface["public_key"],
            "address": interface["address"],
            "protocol": interface["protocol"],
            "disableRoutes": False,
            "natDisabled": False,
            "dns": interface["dns"],
            "publicHost": "",
            "mtu": interface["mtu"],
            "mss": 0,
            "settings": interface["settings"] or None,
        },
        "peers": peers,
    }


def protected_manifest(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "format_version": snapshot["format_version"],
        "generated_at": snapshot["generated_at"],
        "interface": {
            "public_key": snapshot["interface"]["public_key"],
            "address": snapshot["interface"]["address"],
            "protocol": snapshot["interface"]["protocol"],
        },
        "peers": [
            {
                "public_key": item["public_key"],
                "name": item["name"],
                "enabled": item["effective_enabled"],
                "expired_at": item["expired_at"],
                "telegram_user_id": item["telegram_user_id"],
                "telegram_username": item["telegram_username"],
                "promo": item["promo"],
                "role": item["role"],
            }
            for item in snapshot["peers"]
        ],
        "stats": snapshot["stats"],
    }


def load_json(path: Path, require_format: bool = True) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationError(f"Migration artifact not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationError(f"Invalid migration artifact: {path}: {exc}") from exc
    if not isinstance(data, dict) or (
        require_format and data.get("format_version", 1) != 1
    ):
        raise MigrationError(f"Unsupported migration artifact: {path}")
    return data


def select_server(server_key: str):
    for server in load_cascade_servers():
        if server.server_key == server_key:
            return server
    raise MigrationError(f"Cascade server not found: {server_key}")


async def reconcile_imported_peer_states(
    api: CascadeAPI,
    interface_id: str,
    payload_peers: list[dict[str, Any]],
    *,
    apply: bool,
) -> None:
    """Validate imported identities and restore enabled states explicitly."""
    expected = {
        str(item.get("publicKey") or "").strip(): bool(item.get("enabled", True))
        for item in payload_peers
    }
    if "" in expected:
        raise MigrationError("Payload contains a peer without publicKey")

    actual_peers = await api.list_peers(interface_id)
    actual = {
        str(item.get("publicKey") or "").strip(): item for item in actual_peers
    }
    if set(actual) != set(expected):
        raise MigrationError(
            "Existing Cascade interface peer set differs from the migration payload"
        )

    mismatches = [
        public_key
        for public_key, enabled in expected.items()
        if bool(actual[public_key].get("enabled", True)) != enabled
    ]
    if mismatches and not apply:
        raise MigrationError(
            f"Cascade interface has {len(mismatches)} enabled-state mismatches"
        )
    for public_key in mismatches:
        peer_id = str(actual[public_key].get("id") or "").strip()
        if not peer_id:
            raise MigrationError(
                f"Cascade peer has no ID: {fingerprint(public_key)}"
            )
        if expected[public_key]:
            await api.enable_peer(peer_id, interface_id)
        else:
            await api.disable_peer(peer_id, interface_id)

    if mismatches:
        refreshed = {
            str(item.get("publicKey") or "").strip(): item
            for item in await api.list_peers(interface_id)
        }
        remaining = [
            public_key
            for public_key, enabled in expected.items()
            if public_key not in refreshed
            or bool(refreshed[public_key].get("enabled", True)) != enabled
        ]
        if remaining:
            raise MigrationError(
                f"Cascade kept {len(remaining)} peers in an unexpected enabled state"
            )


async def import_command(args: argparse.Namespace) -> int:
    payload = load_json(args.payload)
    payload_peers = payload.get("peers", [])
    if not isinstance(payload_peers, list):
        raise MigrationError("Payload peers must be a list")
    server = select_server(args.server_key)
    api = CascadeAPI(server)
    try:
        public_key = str(payload.get("interface", {}).get("publicKey") or "")
        if not public_key:
            raise MigrationError("Payload is missing interface.publicKey")
        existing = [
            item
            for item in await api.list_interfaces()
            if str(item.get("publicKey") or "") == public_key
        ]
        if len(existing) > 1:
            raise MigrationError("Multiple Cascade interfaces use the migration public key")
        if existing:
            interface = existing[0]
            interface_id = str(interface.get("id") or "").strip()
            if not interface_id:
                raise MigrationError("Existing Cascade interface has no ID")
            await reconcile_imported_peer_states(
                api, interface_id, payload_peers, apply=args.apply
            )
            print(
                "Interface already imported: "
                f"id={interface_id} key={fingerprint(public_key)}"
            )
            return 0
        print(
            f"Import ready: peers={len(payload_peers)} "
            f"listen_port={args.listen_port} key={fingerprint(public_key)}"
        )
        if not args.apply:
            print("DRY-RUN: no Cascade resources were changed")
            return 0
        result = await api.import_interface(
            json.dumps(payload, separators=(",", ":")), args.listen_port
        )
        interface = result["interface"]
        interface_id = str(interface.get("id") or "")
        if result.get("peersFailed"):
            if interface_id:
                await api.delete_interface(interface_id)
            raise MigrationError(
                f"Cascade reported {len(result['peersFailed'])} failed peer imports; "
                "the partial interface was removed"
            )
        try:
            await reconcile_imported_peer_states(
                api, interface_id, payload_peers, apply=True
            )
        except Exception as exc:
            if interface_id:
                await api.delete_interface(interface_id)
            raise MigrationError(
                "Imported interface did not match the migration payload and was removed"
            ) from exc
        receipt = {
            "format_version": 1,
            "server_key": server.server_key,
            "interface_id": str(interface.get("id") or ""),
            "public_key": public_key,
            "listen_port": args.listen_port,
            "peers_created": int(result.get("peersCreated") or 0),
            "started": bool(result.get("started")),
        }
        protected_write(args.receipt, json.dumps(receipt, indent=2))
        print(
            f"Imported interface id={receipt['interface_id']} "
            f"peers={receipt['peers_created']} started={receipt['started']}"
        )
        return 0
    finally:
        await api.close()


async def cascade_peer_map(server_key: str, interface_id: str) -> dict[str, dict[str, Any]]:
    server = select_server(server_key)
    api = CascadeAPI(server)
    try:
        peers = await api.list_peers(interface_id)
        peers_by_key: dict[str, list[dict[str, Any]]] = {}
        for peer in peers:
            public_key = str(peer.get("publicKey") or "").strip()
            peers_by_key.setdefault(public_key, []).append(peer)

        result: dict[str, dict[str, Any]] = {}
        for public_key, matches in peers_by_key.items():
            peer_ids = {str(peer.get("id") or "").strip() for peer in matches}
            if len(peer_ids) > 1 or "" in peer_ids:
                raise MigrationError(
                    f"Duplicate Cascade public key: {fingerprint(public_key)}"
                )
            peer_id = peer_ids.pop()
            try:
                result[public_key] = await api.get_peer(peer_id, interface_id)
            except CascadeNotFound:
                # Cascade keeps expired peers in the list response after removing
                # them from runtime/detail lookup. Preserve the list metadata but
                # treat the authoritative 404 as a disabled state.
                resolved = dict(matches[-1])
                resolved["enabled"] = False
                result[public_key] = resolved
        return result
    finally:
        await api.close()


def apply_bind_transaction(
    database_path: Path,
    manifest_peers: list[dict[str, Any]],
    cascade_by_key: dict[str, dict[str, Any]],
    server_key: str,
    interface_id: str,
) -> int:
    """Persist all Telegram bindings in one transaction."""
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            bound = 0
            for item in manifest_peers:
                user_id = item.get("telegram_user_id")
                if user_id is None:
                    continue
                public_key = item["public_key"]
                cascade_peer = cascade_by_key[public_key]
                existing_assignment = connection.execute(
                    """
                    SELECT server_key, interface_id FROM client_peers
                    WHERE telegram_user_id=? AND server_key IS NOT NULL
                      AND (server_key != ? OR interface_id != ?)
                    LIMIT 1
                    """,
                    (int(user_id), server_key, interface_id),
                ).fetchone()
                if existing_assignment:
                    raise MigrationError(
                        f"Telegram user {user_id} is already assigned to another server"
                    )
                connection.execute(
                    """
                    INSERT INTO clients(telegram_user_id, telegram_username, promo)
                    VALUES (?, ?, ?)
                    ON CONFLICT(telegram_user_id) DO UPDATE SET
                        telegram_username=CASE WHEN excluded.telegram_username != ''
                            THEN excluded.telegram_username ELSE clients.telegram_username END,
                        promo=excluded.promo,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        int(user_id),
                        str(item.get("telegram_username") or ""),
                        int(item.get("promo") or 0),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO client_peers(
                        telegram_user_id, server_key, interface_id, cascade_peer_id,
                        public_key, peer_name, role, enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id, public_key) DO UPDATE SET
                        server_key=excluded.server_key,
                        interface_id=excluded.interface_id,
                        cascade_peer_id=excluded.cascade_peer_id,
                        peer_name=excluded.peer_name,
                        role=excluded.role,
                        enabled=excluded.enabled,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        int(user_id),
                        server_key,
                        interface_id,
                        str(cascade_peer.get("id") or ""),
                        public_key,
                        str(cascade_peer.get("name") or item.get("name") or ""),
                        item.get("role") or "manual",
                        int(bool(cascade_peer.get("enabled", True))),
                    ),
                )
                bound += 1
            connection.execute(
                """
                INSERT INTO operation_logs(peer_name, operation, details)
                VALUES (?, 'cascade_migration_bind', ?)
                """,
                (
                    f"server:{server_key}",
                    json.dumps(
                        {"interface_id": interface_id, "bindings": bound},
                        separators=(",", ":"),
                    ),
                ),
            )
        return bound
    finally:
        connection.close()


async def bind_command(args: argparse.Namespace) -> int:
    manifest = load_json(args.manifest)
    peers = manifest.get("peers")
    if not isinstance(peers, list):
        raise MigrationError("Manifest is missing peers")
    cascade_by_key = await cascade_peer_map(args.server_key, args.interface_id)
    owned = [item for item in peers if item.get("telegram_user_id") is not None]
    missing = [item for item in owned if item["public_key"] not in cascade_by_key]
    if missing:
        raise MigrationError(
            f"Cascade is missing {len(missing)} owned peers; binding was not started"
        )
    print(f"Binding ready: owned_peers={len(owned)} database={args.database}")
    if not args.apply:
        print("DRY-RUN: database was opened read-only and was not initialized")
        with open_read_only(args.database):
            pass
        return 0
    Database(str(args.database))
    bound = apply_bind_transaction(
        args.database, peers, cascade_by_key, args.server_key, args.interface_id
    )
    print(f"Binding complete: persisted={bound}")
    return 0


async def verify_command(args: argparse.Namespace) -> int:
    manifest = load_json(args.manifest)
    expected = {item["public_key"]: item for item in manifest.get("peers", [])}
    actual = await cascade_peer_map(args.server_key, args.interface_id)
    errors: list[str] = []
    if set(expected) != set(actual):
        errors.append(
            f"peer key set differs: expected={len(expected)} actual={len(actual)}"
        )
    for public_key in set(expected) & set(actual):
        if bool(expected[public_key]["enabled"]) != bool(
            actual[public_key].get("enabled", True)
        ):
            errors.append(f"enabled mismatch: {fingerprint(public_key)}")
    if args.database:
        with open_read_only(args.database) as connection:
            if not table_exists(connection, "client_peers"):
                errors.append("target database has no client_peers table")
            else:
                rows = connection.execute(
                    """
                    SELECT public_key FROM client_peers
                    WHERE server_key=? AND interface_id=?
                    """,
                    (args.server_key, args.interface_id),
                ).fetchall()
                bound_keys = {str(row["public_key"]) for row in rows}
                expected_bound = {
                    key
                    for key, item in expected.items()
                    if item.get("telegram_user_id") is not None
                }
                if bound_keys != expected_bound:
                    errors.append(
                        "database bindings differ: "
                        f"expected={len(expected_bound)} actual={len(bound_keys)}"
                    )
    print(
        f"Verify: expected={len(expected)} cascade={len(actual)} errors={len(errors)}"
    )
    for error in errors:
        print(f"ERROR: {error}")
    return 2 if errors else 0


async def inspect_server_command(args: argparse.Namespace) -> int:
    server = select_server(args.server_key)
    api = CascadeAPI(server)
    try:
        health = await api.health()
        interface = await api.get_interface()
        group_id = await api.resolve_client_group_id()
        print(
            json.dumps(
                {
                    "server_key": server.server_key,
                    "health": health.get("status"),
                    "default_interface_id": str(interface.get("id") or ""),
                    "client_group": server.client_group,
                    "client_group_id": group_id,
                },
                indent=2,
            )
        )
        return 0
    finally:
        await api.close()


def source_paths(args: argparse.Namespace) -> SourcePaths:
    return SourcePaths(
        wg_database=args.wg_database,
        jobs_database=args.jobs_database,
        wg_config=args.wg_config,
        bot_database=args.bot_database,
        clients_json=args.clients_json,
    )


def analyze_command(args: argparse.Namespace) -> int:
    snapshot = build_snapshot(
        source_paths(args), args.key_tool, load_resolutions(args.resolutions)
    )
    report = safe_report(snapshot)
    print(json.dumps(report, indent=2))
    if args.report:
        protected_write(args.report, json.dumps(report, indent=2))
    return 2 if snapshot["conflicts"] else 0


def prepare_command(args: argparse.Namespace) -> int:
    snapshot = build_snapshot(
        source_paths(args), args.key_tool, load_resolutions(args.resolutions)
    )
    if snapshot["conflicts"]:
        print(json.dumps(safe_report(snapshot), indent=2))
        raise MigrationError("Conflicts must be resolved before preparing import files")
    payload = native_backup(snapshot, args.group_id)
    manifest = protected_manifest(snapshot)
    protected_write(args.payload, json.dumps(payload, indent=2))
    protected_write(args.manifest, json.dumps(manifest, indent=2))
    if args.report:
        protected_write(args.report, json.dumps(safe_report(snapshot), indent=2))
    print(
        f"Prepared payload={args.payload} manifest={args.manifest} "
        f"peers={snapshot['stats']['peers']}"
    )
    return 0


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wg-database", type=Path, required=True)
    parser.add_argument("--jobs-database", type=Path, required=True)
    parser.add_argument("--wg-config", type=Path, required=True)
    parser.add_argument("--bot-database", type=Path, required=True)
    parser.add_argument("--clients-json", type=Path)
    parser.add_argument(
        "--key-tool",
        default="internal",
        help="Use internal RFC 7748 validation or an external awg-compatible binary",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--resolutions", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-time WGDashboard to Cascade production migration"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze")
    add_source_arguments(analyze)

    prepare = subparsers.add_parser("prepare")
    add_source_arguments(prepare)
    prepare.add_argument("--group-id", required=True)
    prepare.add_argument("--payload", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--server-key", required=True)
    import_parser.add_argument("--payload", type=Path, required=True)
    import_parser.add_argument("--listen-port", type=int, required=True)
    import_parser.add_argument(
        "--receipt", type=Path, default=Path("migration-import-receipt.json")
    )
    import_parser.add_argument("--apply", action="store_true")

    bind = subparsers.add_parser("bind")
    bind.add_argument("--server-key", required=True)
    bind.add_argument("--interface-id", required=True)
    bind.add_argument("--manifest", type=Path, required=True)
    bind.add_argument("--database", type=Path, required=True)
    bind.add_argument("--apply", action="store_true")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--server-key", required=True)
    verify.add_argument("--interface-id", required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--database", type=Path)

    inspect_server = subparsers.add_parser("inspect-server")
    inspect_server.add_argument("--server-key", required=True)
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "import":
        return await import_command(args)
    if args.command == "bind":
        return await bind_command(args)
    if args.command == "verify":
        return await verify_command(args)
    if args.command == "inspect-server":
        return await inspect_server_command(args)
    raise MigrationError(f"Unsupported async command: {args.command}")


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "analyze":
            return analyze_command(args)
        if args.command == "prepare":
            return prepare_command(args)
        return asyncio.run(async_main(args))
    except (MigrationError, CascadeError, sqlite3.Error) as exc:
        print(f"MIGRATION ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
