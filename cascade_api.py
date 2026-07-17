import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from config import (
    CASCADE_REQUEST_TIMEOUT,
    CASCADE_RESERVATION_MINUTES,
    CASCADE_SERVERS_FILE,
)
from database import Database


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
        server = CascadeServer(
            server_key=str(item.get("server_key") or "").strip(),
            base_url=str(item.get("base_url") or "").strip(),
            api_token=str(item.get("api_token") or "").strip(),
            interface_id=str(item.get("interface_id") or "").strip(),
            priority=int(item.get("priority", 100)),
            max_peers=int(item.get("max_peers", 0)),
            enabled=bool(item.get("enabled", True)),
            verify_tls=bool(item.get("verify_tls", True)),
        )
        if not all((server.server_key, server.base_url, server.api_token, server.interface_id)):
            raise CascadeError(f"Cascade server entry {index} has missing required fields")
        if server.server_key in seen:
            raise CascadeError(f"Duplicate Cascade server_key: {server.server_key}")
        if server.max_peers <= 0:
            raise CascadeError(f"max_peers must be positive for {server.server_key}")
        seen.add(server.server_key)
        servers.append(server)

    return sorted(servers, key=lambda item: (item.priority, item.server_key))


class CascadeAPI:
    """Asynchronous REST client for one Cascade router."""

    def __init__(self, server: CascadeServer):
        self.server = server
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
            detail = response.text[:500]
            raise CascadeError(
                f"Cascade {method} {path} returned {response.status_code}: {detail}"
            )
        if expect_bytes:
            return response.content
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def get_interface(self, interface_id: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET", f"/tunnel-interfaces/{interface_id or self.server.interface_id}"
        )

    async def list_peers(self, interface_id: str | None = None) -> list[dict[str, Any]]:
        result = await self._request(
            "GET", f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers"
        )
        return result.get("peers", []) if isinstance(result, dict) else []

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
        result = await self._request(
            "POST",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers",
            json_body={
                "name": name,
                "peerType": "client",
                "generateKeys": True,
                "autoAllocateIP": True,
                "expiredAt": to_rfc3339(expired_at),
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CascadeRouter:
    """Select Cascade servers and execute user-scoped provisioning operations."""

    def __init__(self, db: Database, servers: list[CascadeServer] | None = None):
        self.db = db
        self.servers = servers if servers is not None else load_cascade_servers()
        self.apis = {server.server_key: CascadeAPI(server) for server in self.servers}
        self._placement_lock = asyncio.Lock()

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
            try:
                api = self.get_api(server.server_key)
                interface_id = (
                    assigned_peer["interface_id"]
                    if assigned_peer and assigned_peer["server_key"] == server.server_key
                    else server.interface_id
                )
                if not assigned_peer and (
                    not reservation or reservation["server_key"] != server.server_key
                ):
                    peers = await api.list_peers()
                    if len(peers) + self.db.count_active_reservations(server.server_key) >= server.max_peers:
                        continue
                peer = await api.create_peer(peer_name, expire_date, interface_id)
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
                if peer and peer.get("id"):
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
        result = {"total": len(peers), "updated": 0, "failed": 0}
        is_future = datetime.fromisoformat(expire_date).replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
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
            except Exception as exc:
                result["failed"] += 1
                logger.error("Failed to sync Cascade peer %s: %s", peer["cascade_peer_id"], exc)
        return result

    async def close(self) -> None:
        await asyncio.gather(*(api.close() for api in self.apis.values()))
