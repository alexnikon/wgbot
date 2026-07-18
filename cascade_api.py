import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

import httpx

from config import (
    CASCADE_REQUEST_TIMEOUT,
    CASCADE_RESERVATION_MINUTES,
    CASCADE_SERVERS_FILE,
)
from database import Database
from runtime_metrics import RuntimeMetrics

logger = logging.getLogger(__name__)


class CascadeError(RuntimeError):
    """Base error raised by Cascade integration."""


class CascadeNotFound(CascadeError):
    """Requested Cascade resource does not exist."""


class CascadeCapacityError(CascadeError):
    """No configured Cascade server has free peer capacity."""


@dataclass(frozen=True)
class CascadeServer:
    server_key: str
    base_url: str
    api_token: str
    interface_id: str
    priority: int
    max_peers: int
    client_group: str = "Basic"
    enabled: bool = True
    verify_tls: bool = True

    @property
    def api_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/api") else f"{base}/api"


def load_cascade_servers(path: Path = CASCADE_SERVERS_FILE) -> list[CascadeServer]:
    """Load and validate the protected Cascade server registry."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CascadeError(f"Cascade server registry not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CascadeError(f"Invalid Cascade server registry JSON: {exc}") from exc

    entries = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise CascadeError("Cascade server registry must contain a non-empty servers list")

    servers: list[CascadeServer] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise CascadeError(f"Cascade server entry {index} must be an object")
        enabled = item.get("enabled", True)
        verify_tls = item.get("verify_tls", True)
        if not isinstance(enabled, bool) or not isinstance(verify_tls, bool):
            raise CascadeError(
                f"enabled and verify_tls must be JSON booleans for server entry {index}"
            )
        server = CascadeServer(
            server_key=str(item.get("server_key") or "").strip(),
            base_url=str(item.get("base_url") or "").strip(),
            api_token=str(item.get("api_token") or "").strip(),
            interface_id=str(item.get("interface_id") or "").strip(),
            priority=int(item.get("priority", 100)),
            max_peers=int(item.get("max_peers", 0)),
            client_group=str(item.get("client_group") or "Basic").strip(),
            enabled=enabled,
            verify_tls=verify_tls,
        )
        if not all((server.server_key, server.base_url, server.api_token, server.interface_id)):
            raise CascadeError(f"Cascade server entry {index} has missing required fields")
        if server.server_key in seen:
            raise CascadeError(f"Duplicate Cascade server_key: {server.server_key}")
        if server.max_peers <= 0:
            raise CascadeError(f"max_peers must be positive for {server.server_key}")
        try:
            parsed_url = httpx.URL(server.base_url)
        except Exception as exc:
            raise CascadeError(f"Invalid base_url for {server.server_key}") from exc
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
            raise CascadeError(f"Invalid base_url for {server.server_key}")
        if parsed_url.query or parsed_url.fragment:
            raise CascadeError(f"base_url must not contain query or fragment for {server.server_key}")
        if parsed_url.scheme != "https" and server.verify_tls:
            raise CascadeError(
                f"HTTPS is required for {server.server_key}; explicitly set verify_tls=false only for trusted development networks"
            )
        if len(server.api_token) < 16:
            raise CascadeError(f"API token is unexpectedly short for {server.server_key}")
        if not server.client_group:
            raise CascadeError(f"client_group must not be empty for {server.server_key}")
        seen.add(server.server_key)
        servers.append(server)

    return sorted(servers, key=lambda item: (item.priority, item.server_key))


class CascadeAPI:
    """Asynchronous REST client for one Cascade router."""

    def __init__(self, server: CascadeServer, metrics: RuntimeMetrics | None = None):
        self.server = server
        self.metrics = metrics
        self._client_group_id: str | None = None
        self.client = httpx.AsyncClient(
            base_url=server.api_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {server.api_token}"},
            timeout=httpx.Timeout(CASCADE_REQUEST_TIMEOUT, connect=10.0),
            verify=server.verify_tls,
            follow_redirects=False,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expect_bytes: bool = False,
    ) -> Any:
        started_at = time.monotonic()
        success = False
        try:
            try:
                response = await self.client.request(
                    method, path.lstrip("/"), json=json_body
                )
            except httpx.HTTPError as exc:
                raise CascadeError(
                    f"Cascade request failed for {self.server.server_key}: {exc}"
                ) from exc

            if response.status_code == 404:
                raise CascadeNotFound(
                    f"Cascade resource not found on {self.server.server_key}: {path}"
                )
            if response.is_error:
                try:
                    error_payload = response.json()
                    error_code = (
                        str(error_payload.get("error") or "").casefold()
                        if isinstance(error_payload, dict)
                        else ""
                    )
                except (TypeError, ValueError):
                    error_code = ""
                if response.status_code == 400 and error_code == "peer not found":
                    raise CascadeNotFound(
                        f"Cascade peer not found on {self.server.server_key}: {path}"
                    )
                detail = response.text[:500]
                raise CascadeError(
                    f"Cascade {method} {path} returned {response.status_code}: {detail}"
                )
            success = True
            if expect_bytes:
                return response.content
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        finally:
            if self.metrics:
                self.metrics.record_cascade(
                    self.server.server_key,
                    time.monotonic() - started_at,
                    success,
                )

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def get_interface(self, interface_id: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET", f"/tunnel-interfaces/{interface_id or self.server.interface_id}"
        )

    async def list_interfaces(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/tunnel-interfaces")
        return result.get("interfaces", []) if isinstance(result, dict) else []

    async def import_interface(
        self, raw_json: str, listen_port: int
    ) -> dict[str, Any]:
        """Restore a native Cascade interface export with its original keys."""
        result = await self._request(
            "POST",
            "/tunnel-interfaces/import-interface",
            json_body={"json": raw_json, "listenPort": listen_port},
        )
        if not isinstance(result, dict) or not isinstance(result.get("interface"), dict):
            raise CascadeError(
                f"Invalid import interface response from {self.server.server_key}"
            )
        return result

    async def delete_interface(self, interface_id: str) -> None:
        await self._request("DELETE", f"/tunnel-interfaces/{interface_id}")

    async def list_peers(self, interface_id: str | None = None) -> list[dict[str, Any]]:
        result = await self._request(
            "GET", f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers"
        )
        return result.get("peers", []) if isinstance(result, dict) else []

    async def list_client_groups(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/aliases/client-groups")
        return result.get("groups", []) if isinstance(result, dict) else []

    async def resolve_client_group_id(self) -> str:
        """Resolve the configured client-group name to its Cascade alias ID."""
        if self._client_group_id:
            return self._client_group_id
        expected = self.server.client_group.casefold()
        for group in await self.list_client_groups():
            if str(group.get("name") or "").casefold() == expected:
                group_id = str(group.get("id") or "").strip()
                if group_id:
                    self._client_group_id = group_id
                    return group_id
        raise CascadeError(
            f"Client group {self.server.client_group!r} was not found on "
            f"{self.server.server_key}"
        )

    async def get_peer(self, peer_id: str, interface_id: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers/{peer_id}",
        )

    async def create_peer(
        self,
        name: str,
        expired_at: str,
        interface_id: str | None = None,
    ) -> dict[str, Any]:
        group_id = await self.resolve_client_group_id()
        result = await self._request(
            "POST",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers",
            json_body={
                "name": name,
                "peerType": "client",
                "generateKeys": True,
                "autoAllocateIP": True,
                "expiredAt": to_rfc3339(expired_at),
                "groupId": group_id,
            },
        )
        peer = result.get("peer") if isinstance(result, dict) else None
        if not isinstance(peer, dict) or not peer.get("id"):
            raise CascadeError(f"Invalid create peer response from {self.server.server_key}")
        return peer

    async def update_expiry(
        self, peer_id: str, expired_at: str, interface_id: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers/{peer_id}",
            json_body={"expiredAt": to_rfc3339(expired_at)},
        )

    async def enable_peer(self, peer_id: str, interface_id: str | None = None) -> Any:
        return await self._request(
            "POST",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers/{peer_id}/enable",
        )

    async def disable_peer(self, peer_id: str, interface_id: str | None = None) -> Any:
        return await self._request(
            "POST",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers/{peer_id}/disable",
        )

    async def delete_peer(self, peer_id: str, interface_id: str | None = None) -> None:
        await self._request(
            "DELETE",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers/{peer_id}",
        )

    async def download_config(
        self, peer_id: str, interface_id: str | None = None
    ) -> bytes:
        return await self._request(
            "GET",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers/{peer_id}/config",
            expect_bytes=True,
        )

    async def close(self) -> None:
        await self.client.aclose()


def to_rfc3339(value: str) -> str:
    """Convert the bot's SQLite timestamp into Cascade's RFC3339 format."""
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CascadeError(f"Invalid expiration date: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CascadeRouter:
    """Select Cascade servers and execute user-scoped provisioning operations."""

    def __init__(
        self,
        db: Database,
        servers: list[CascadeServer] | None = None,
        metrics: RuntimeMetrics | None = None,
    ):
        self.db = db
        self.servers = servers if servers is not None else load_cascade_servers()
        self.metrics = metrics
        self.apis = {
            server.server_key: CascadeAPI(server, metrics=metrics)
            for server in self.servers
        }
        self._placement_lock = asyncio.Lock()
        self._user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    def get_server(self, server_key: str) -> CascadeServer:
        for server in self.servers:
            if server.server_key == server_key:
                return server
        raise CascadeError(f"Unknown Cascade server: {server_key}")

    def get_api(self, server_key: str) -> CascadeAPI:
        try:
            return self.apis[server_key]
        except KeyError as exc:
            raise CascadeError(f"Unknown Cascade server: {server_key}") from exc

    async def validate(self) -> dict[str, str]:
        """Validate health, token, and interface on every configured server."""
        async def check(server: CascadeServer) -> tuple[str, str]:
            try:
                health = await self.get_api(server.server_key).health()
                if health.get("status") != "ok":
                    raise CascadeError(
                        f"Unexpected health status: {health.get('status')}"
                    )
                interface = await self.get_api(server.server_key).get_interface()
                if str(interface.get("id")) != server.interface_id:
                    raise CascadeError("Configured interface ID does not match API response")
                await self.get_api(server.server_key).resolve_client_group_id()
                status = "ok" if server.enabled else "ok-disabled"
                return server.server_key, status
            except Exception as exc:
                return server.server_key, f"error: {exc}"

        checks = await asyncio.gather(*(check(server) for server in self.servers))
        return dict(checks)

    async def ensure_reservation(self, user_id: int) -> dict[str, Any] | None:
        """Reserve capacity for a new user; existing users stay on their server."""
        if self.db.get_primary_client_peer(user_id):
            return None
        current = self.db.get_active_reservation(user_id)
        if current:
            return current

        async with self._placement_lock:
            self.db.cleanup_expired_reservations()
            current = self.db.get_active_reservation(user_id)
            if current:
                return current
            for server in self.servers:
                if not server.enabled:
                    continue
                try:
                    peers = await self.get_api(server.server_key).list_peers()
                except CascadeError as exc:
                    logger.warning("Skipping unavailable Cascade server %s: %s", server.server_key, exc)
                    continue
                reserved = self.db.count_active_reservations(server.server_key)
                if len(peers) + reserved >= server.max_peers:
                    continue
                self.db.create_reservation(
                    user_id,
                    server.server_key,
                    server.interface_id,
                    CASCADE_RESERVATION_MINUTES,
                )
                return self.db.get_active_reservation(user_id)
        raise CascadeCapacityError("All Cascade servers are full or unavailable")

    async def create_user_peer(
        self, user_id: int, username: str | None, peer_name: str, expire_date: str
    ) -> tuple[dict[str, Any], bytes]:
        """Serialize peer reconciliation and creation for one Telegram user."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            existing = self.db.get_primary_client_peer(user_id)
            if existing:
                try:
                    api = self.get_api(existing["server_key"])
                    get_peer = getattr(api, "get_peer", None)
                    if get_peer is not None:
                        peer = await get_peer(
                            existing["cascade_peer_id"], existing["interface_id"]
                        )
                        config = await api.download_config(
                            existing["cascade_peer_id"], existing["interface_id"]
                        )
                        return peer, config
                except CascadeNotFound:
                    logger.warning(
                        "Stored Cascade peer %s for user %s no longer exists",
                        existing["cascade_peer_id"],
                        user_id,
                    )
            return await self._create_user_peer_unlocked(
                user_id, username, peer_name, expire_date
            )

    async def _create_user_peer_unlocked(
        self, user_id: int, username: str | None, peer_name: str, expire_date: str
    ) -> tuple[dict[str, Any], bytes]:
        """Create and persist a primary peer, failing over before creation if needed."""
        assigned_peer = self.db.get_primary_client_peer(user_id)
        reservation = None if assigned_peer else await self.ensure_reservation(user_id)
        candidates: list[CascadeServer] = []
        if assigned_peer:
            candidates.append(self.get_server(assigned_peer["server_key"]))
        elif reservation:
            candidates.append(self.get_server(reservation["server_key"]))
        if not assigned_peer:
            candidates.extend(
                server for server in self.servers if server.enabled and server not in candidates
            )
        last_error: Exception | None = None
        for server in candidates:
            peer: dict[str, Any] | None = None
            created_here = False
            try:
                api = self.get_api(server.server_key)
                interface_id = (
                    assigned_peer["interface_id"]
                    if assigned_peer and assigned_peer["server_key"] == server.server_key
                    else server.interface_id
                )
                peers = await api.list_peers()
                matches = [
                    item
                    for item in peers
                    if str(item.get("name") or "").strip() == peer_name
                ]
                if len(matches) > 1:
                    raise CascadeError(
                        f"Multiple Cascade peers named {peer_name!r} exist on {server.server_key}"
                    )
                if matches:
                    peer = matches[0]
                    public_key = str(peer.get("publicKey") or "").strip()
                    if not peer.get("id") or not public_key:
                        raise CascadeError("Reconciled Cascade peer has incomplete identity")
                    config = await api.download_config(str(peer["id"]), interface_id)
                    self.db.upsert_client(user_id, username)
                    if not self.db.save_client_peer(
                        user_id=user_id,
                        server_key=server.server_key,
                        interface_id=interface_id,
                        cascade_peer_id=str(peer["id"]),
                        public_key=public_key,
                        peer_name=peer_name,
                        role="primary",
                        enabled=bool(peer.get("enabled", True)),
                    ):
                        raise CascadeError("Failed to persist the reconciled Cascade peer")
                    self.db.release_reservation(user_id)
                    return peer, config

                if (
                    not assigned_peer
                    and (not reservation or reservation["server_key"] != server.server_key)
                    and len(peers) + self.db.count_active_reservations(server.server_key)
                    >= server.max_peers
                ):
                    continue
                peer = await api.create_peer(peer_name, expire_date, interface_id)
                created_here = True
                public_key = str(peer.get("publicKey") or "").strip()
                if not public_key:
                    raise CascadeError("Cascade create response has no public key")
                config = await api.download_config(str(peer["id"]), interface_id)
                self.db.upsert_client(user_id, username)
                saved = self.db.save_client_peer(
                    user_id=user_id,
                    server_key=server.server_key,
                    interface_id=interface_id,
                    cascade_peer_id=str(peer["id"]),
                    public_key=public_key,
                    peer_name=str(peer.get("name") or peer_name),
                    role="primary",
                    enabled=bool(peer.get("enabled", True)),
                )
                if not saved:
                    raise CascadeError("Failed to persist the created Cascade peer")
                self.db.release_reservation(user_id)
                return peer, config
            except Exception as exc:
                last_error = exc
                logger.error("Provisioning failed on %s for user %s: %s", server.server_key, user_id, exc)
                if created_here and peer and peer.get("id"):
                    try:
                        await self.get_api(server.server_key).delete_peer(
                            str(peer["id"]), interface_id
                        )
                    except Exception:
                        logger.exception("Failed to compensate Cascade peer creation")
        raise CascadeError(f"Failed to provision user on all Cascade servers: {last_error}")

    async def get_primary_config(self, user_id: int) -> bytes:
        peer = self.db.get_primary_client_peer(user_id)
        if not peer:
            raise CascadeNotFound(f"No primary Cascade peer for user {user_id}")
        return await self.get_api(peer["server_key"]).download_config(
            peer["cascade_peer_id"], peer["interface_id"]
        )

    async def primary_peer_exists(self, user_id: int) -> bool:
        peer = self.db.get_primary_client_peer(user_id)
        if not peer:
            return False
        try:
            await self.get_api(peer["server_key"]).get_peer(
                peer["cascade_peer_id"], peer["interface_id"]
            )
            return True
        except CascadeNotFound:
            return False

    async def sync_user_access(self, user_id: int, expire_date: str) -> dict[str, int]:
        peers = self.db.get_client_peers(user_id, bound_only=True)
        result = {"total": len(peers), "updated": 0, "missing": 0, "failed": 0}
        is_future = datetime.fromisoformat(expire_date).replace(tzinfo=UTC) > datetime.now(UTC)
        for peer in peers:
            try:
                api = self.get_api(peer["server_key"])
                await api.update_expiry(peer["cascade_peer_id"], expire_date, peer["interface_id"])
                if is_future:
                    await api.enable_peer(peer["cascade_peer_id"], peer["interface_id"])
                else:
                    await api.disable_peer(peer["cascade_peer_id"], peer["interface_id"])
                self.db.set_client_peer_enabled(peer["cascade_peer_id"], is_future)
                result["updated"] += 1
            except CascadeNotFound as exc:
                if peer["role"] != "manual":
                    result["failed"] += 1
                    logger.error(
                        "Primary Cascade peer %s is missing: %s",
                        peer["cascade_peer_id"],
                        exc,
                    )
                    continue
                self.db.set_client_peer_enabled(peer["cascade_peer_id"], False)
                result["missing"] += 1
                logger.warning(
                    "Skipping missing manual Cascade peer %s for user %s",
                    peer["cascade_peer_id"],
                    user_id,
                )
            except Exception as exc:
                result["failed"] += 1
                logger.error("Failed to sync Cascade peer %s: %s", peer["cascade_peer_id"], exc)
        return result

    async def close(self) -> None:
        await asyncio.gather(*(api.close() for api in self.apis.values()))
