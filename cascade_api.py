import asyncio
import hashlib
import json
import logging
import time
from contextlib import suppress
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
from database import Database, normalize_config_name
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
    assignable_client_groups: tuple[str, ...] = ()
    enabled: bool = True
    verify_tls: bool = True
    server_name: str = ""

    @property
    def api_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/api") else f"{base}/api"

    @property
    def selectable_client_groups(self) -> tuple[str, ...]:
        return self.assignable_client_groups or (self.client_group,)


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
        server_key = str(item.get("server_key") or "").strip()
        raw_server_name = item.get("server_name", server_key)
        client_group = str(item.get("client_group") or "Basic").strip()
        raw_assignable_groups = item.get("assignable_client_groups")
        if raw_assignable_groups is None:
            assignable_groups = (client_group,)
        elif isinstance(raw_assignable_groups, list):
            assignable_groups = tuple(str(value).strip() for value in raw_assignable_groups)
        else:
            raise CascadeError(
                f"assignable_client_groups must be a list for server entry {index}"
            )
        server = CascadeServer(
            server_key=server_key,
            base_url=str(item.get("base_url") or "").strip(),
            api_token=str(item.get("api_token") or "").strip(),
            interface_id=str(item.get("interface_id") or "").strip(),
            priority=int(item.get("priority", 100)),
            max_peers=int(item.get("max_peers", 0)),
            client_group=client_group,
            assignable_client_groups=assignable_groups,
            enabled=enabled,
            verify_tls=verify_tls,
            server_name=str(raw_server_name or "").strip(),
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
        normalized_groups = [group.casefold() for group in server.selectable_client_groups]
        if (
            not normalized_groups
            or len(normalized_groups) != len(set(normalized_groups))
            or any(
                not group
                or len(group) > 64
                or any(ord(character) < 32 or ord(character) == 127 for character in group)
                for group in server.selectable_client_groups
            )
        ):
            raise CascadeError(
                f"assignable_client_groups must contain unique printable names for "
                f"{server.server_key}"
            )
        if server.client_group.casefold() not in normalized_groups:
            raise CascadeError(
                f"client_group must be included in assignable_client_groups for "
                f"{server.server_key}"
            )
        if (
            not server.server_name
            or len(server.server_name) > 64
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in server.server_name
            )
        ):
            raise CascadeError(
                f"server_name must contain 1-64 printable characters for "
                f"{server.server_key}"
            )
        seen.add(server.server_key)
        servers.append(server)

    return sorted(servers, key=lambda item: (item.priority, item.server_key))


class CascadeAPI:
    """Asynchronous REST client for one Cascade router."""

    def __init__(self, server: CascadeServer, metrics: RuntimeMetrics | None = None):
        self.server = server
        self.metrics = metrics
        self._client_group_ids: dict[str, str] = {}
        self._client_group_names: dict[str, str] = {}
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

    async def resolve_client_group_id(self, group_name: str | None = None) -> str:
        """Resolve one configured client-group name to its Cascade alias ID."""
        requested = (group_name or self.server.client_group).strip()
        expected = requested.casefold()
        if expected in self._client_group_ids:
            return self._client_group_ids[expected]
        for group in await self.list_client_groups():
            name = str(group.get("name") or "").strip()
            group_id = str(group.get("id") or "").strip()
            if name and group_id:
                self._client_group_ids[name.casefold()] = group_id
                self._client_group_names[group_id] = name
        if expected in self._client_group_ids:
            return self._client_group_ids[expected]
        raise CascadeError(
            f"Client group {requested!r} was not found on "
            f"{self.server.server_key}"
        )

    async def resolve_client_group_name(self, group_id: str) -> str | None:
        """Resolve a Cascade group ID to its current display name."""
        if group_id in self._client_group_names:
            return self._client_group_names[group_id]
        for group in await self.list_client_groups():
            name = str(group.get("name") or "").strip()
            current_id = str(group.get("id") or "").strip()
            if name and current_id:
                self._client_group_ids[name.casefold()] = current_id
                self._client_group_names[current_id] = name
        return self._client_group_names.get(group_id)

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
        client_group: str | None = None,
    ) -> dict[str, Any]:
        group_id = await self.resolve_client_group_id(client_group)
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

    async def update_client_group(
        self,
        peer_id: str,
        group_name: str,
        interface_id: str | None = None,
    ) -> dict[str, Any]:
        """Move an existing peer to one named Cascade client group."""
        group_id = await self.resolve_client_group_id(group_name)
        return await self._request(
            "PATCH",
            f"/tunnel-interfaces/{interface_id or self.server.interface_id}/peers/{peer_id}",
            json_body={"groupId": group_id},
        )

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

    def get_enabled_servers(self) -> list[CascadeServer]:
        return [server for server in self.servers if server.enabled]

    def get_server_name(self, server_key: str) -> str:
        server = self.get_server(server_key)
        return server.server_name or server.server_key

    async def list_server_interfaces(self, server_key: str) -> list[dict[str, Any]]:
        server = self.get_server(server_key)
        if not server.enabled:
            raise CascadeError(f"Cascade server is disabled: {server_key}")
        return await self.get_api(server_key).list_interfaces()

    async def list_assignable_client_groups(
        self, user_id: int, extra_server_key: str | None = None
    ) -> list[str]:
        """Return live assignable groups shared by every server used by a client."""
        server_keys = {
            str(peer["server_key"])
            for peer in self.db.get_managed_client_configs(user_id)
        }
        if extra_server_key:
            server_keys.add(extra_server_key)
        if not server_keys:
            return []
        shared: dict[str, str] | None = None
        for server_key in sorted(server_keys):
            server = self.get_server(server_key)
            if not server.enabled:
                raise CascadeError(f"Cascade server is disabled: {server_key}")
            live = {
                str(group.get("name") or "").strip().casefold():
                str(group.get("name") or "").strip()
                for group in await self.get_api(server_key).list_client_groups()
                if str(group.get("id") or "").strip()
                and str(group.get("name") or "").strip()
            }
            configured = {
                name.casefold(): live[name.casefold()]
                for name in server.selectable_client_groups
                if name.casefold() in live
            }
            shared = configured if shared is None else {
                key: shared[key] for key in shared.keys() & configured.keys()
            }
        return list(shared.values()) if shared else []

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
                for group_name in server.selectable_client_groups:
                    await self.get_api(server.server_key).resolve_client_group_id(
                        group_name
                    )
                status = "ok" if server.enabled else "ok-disabled"
                return server.server_key, status
            except Exception as exc:
                return server.server_key, f"error: {exc}"

        checks = await asyncio.gather(*(check(server) for server in self.servers))
        return dict(checks)

    @staticmethod
    def _peer_group_id(peer: dict[str, Any]) -> str:
        nested_group = peer.get("group") or peer.get("clientGroup")
        group_id = str(
            peer.get("groupId")
            or peer.get("clientGroupId")
            or peer.get("group_id")
            or (nested_group.get("id") if isinstance(nested_group, dict) else "")
            or ""
        ).strip()
        if not group_id:
            raise CascadeError("Cascade peer response has no client group ID")
        return group_id

    async def _read_peer_group(self, peer: dict[str, Any]) -> str:
        api = self.get_api(str(peer["server_key"]))
        live_peer = await api.get_peer(
            str(peer["cascade_peer_id"]), str(peer["interface_id"])
        )
        if isinstance(live_peer.get("peer"), dict):
            live_peer = live_peer["peer"]
        group_name = await api.resolve_client_group_name(
            self._peer_group_id(live_peer)
        )
        if not group_name:
            raise CascadeError("Cascade peer references an unknown client group")
        return group_name

    async def reconcile_client_groups(self) -> dict[str, int]:
        """Refresh stored group names without mutating Cascade peers."""
        result = {"total": 0, "updated": 0, "unknown": 0}
        peers_by_interface: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for peer in self.db.get_all_managed_client_peers():
            key = (str(peer["server_key"]), str(peer["interface_id"]))
            peers_by_interface.setdefault(key, []).append(peer)
        result["total"] = sum(len(peers) for peers in peers_by_interface.values())
        for (server_key, interface_id), stored_peers in peers_by_interface.items():
            try:
                api = self.get_api(server_key)
                live_peers = {
                    str(peer.get("id") or ""): peer
                    for peer in await api.list_peers(interface_id)
                    if peer.get("id")
                }
                for stored_peer in stored_peers:
                    try:
                        live_peer = live_peers.get(str(stored_peer["cascade_peer_id"]))
                        if not live_peer:
                            raise CascadeNotFound("Stored peer is missing from Cascade")
                        group_name = await api.resolve_client_group_name(
                            self._peer_group_id(live_peer)
                        )
                        if not group_name:
                            raise CascadeError(
                                "Cascade peer references an unknown client group"
                            )
                        self.db.set_client_peer_group(
                            int(stored_peer["id"]), group_name
                        )
                        result["updated"] += 1
                    except Exception:
                        self.db.set_client_peer_group(int(stored_peer["id"]), None)
                        result["unknown"] += 1
            except Exception as exc:
                for stored_peer in stored_peers:
                    self.db.set_client_peer_group(int(stored_peer["id"]), None)
                result["unknown"] += len(stored_peers)
                logger.warning(
                    "Unable to reconcile client groups on %s interface %s: %s",
                    server_key,
                    interface_id,
                    type(exc).__name__,
                )
        self.db.log_operation(
            "system",
            "reconcile_client_groups",
            json.dumps(result, sort_keys=True),
        )
        return result

    async def delete_peer_by_identity(
        self, server_key: str, interface_id: str, cascade_peer_id: str
    ) -> None:
        """Delete a compensating orphan created before local persistence."""
        await self.get_api(server_key).delete_peer(cascade_peer_id, interface_id)

    async def _restore_peer_groups(
        self, user_id: int, original_groups: dict[int, str]
    ) -> None:
        failures = 0
        for peer_id, group_name in original_groups.items():
            peer = self.db.get_client_peer(peer_id, user_id)
            if not peer or peer["role"] not in {"primary", "additional"}:
                failures += 1
                continue
            try:
                await self.get_api(str(peer["server_key"])).update_client_group(
                    str(peer["cascade_peer_id"]),
                    group_name,
                    str(peer["interface_id"]),
                )
                self.db.set_client_peer_group(peer_id, group_name)
            except Exception:
                failures += 1
                self.db.set_client_peer_group(peer_id, None)
        if failures:
            raise CascadeError(f"Failed to restore groups for {failures} peers")

    async def restore_peer_groups(
        self, user_id: int, original_groups: dict[str, str]
    ) -> None:
        await self._restore_peer_groups(
            user_id, {int(peer_id): group for peer_id, group in original_groups.items()}
        )

    async def _change_client_group_unlocked(
        self, user_id: int, group_name: str
    ) -> dict[int, str]:
        peers = self.db.get_managed_client_configs(user_id)
        if not peers:
            raise CascadeError("Client has no managed configurations")
        available = await self.list_assignable_client_groups(user_id)
        selected = next(
            (name for name in available if name.casefold() == group_name.casefold()),
            None,
        )
        if not selected:
            raise CascadeError(f"Client group {group_name!r} is not assignable")
        original_groups: dict[int, str] = {}
        for peer in peers:
            original_groups[int(peer["id"])] = await self._read_peer_group(peer)
        changed: dict[int, str] = {}
        try:
            for peer in peers:
                peer_id = int(peer["id"])
                if original_groups[peer_id].casefold() == selected.casefold():
                    continue
                await self.get_api(str(peer["server_key"])).update_client_group(
                    str(peer["cascade_peer_id"]), selected, str(peer["interface_id"])
                )
                changed[peer_id] = original_groups[peer_id]
            self.db.set_client_peer_groups(user_id, selected)
            return original_groups
        except Exception:
            try:
                await self._restore_peer_groups(user_id, changed)
            except CascadeError as rollback_error:
                self.db.add_provisioning_task(
                    user_id,
                    "restore_peer_groups",
                    {"groups": {str(key): value for key, value in changed.items()}},
                    str(rollback_error),
                )
            raise

    async def _verify_client_group_unlocked(
        self, user_id: int, group_name: str
    ) -> None:
        """Verify that every managed peer still has the inherited group."""
        peers = self.db.get_managed_client_configs(user_id)
        if not peers:
            raise CascadeError("Client has no managed configurations")
        available = await self.list_assignable_client_groups(user_id)
        selected = next(
            (name for name in available if name.casefold() == group_name.casefold()),
            None,
        )
        if not selected:
            raise CascadeError(f"Client group {group_name!r} is not assignable")
        for peer in peers:
            live_group = await self._read_peer_group(peer)
            if live_group.casefold() != selected.casefold():
                raise CascadeError("Managed client groups changed during creation")

    async def change_client_group(self, user_id: int, group_name: str) -> int:
        """Move every managed peer of one client to a single group."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            original = await self._change_client_group_unlocked(user_id, group_name)
            return len(original)

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
                    client_group = None
                    with suppress(CascadeError):
                        client_group = await api.resolve_client_group_name(
                            self._peer_group_id(peer)
                        )
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
                        client_group=client_group,
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
                    client_group=server.client_group,
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

    async def get_managed_config(self, user_id: int, peer_id: int) -> bytes:
        peer = self.db.get_client_peer(peer_id, user_id)
        if (
            not peer
            or peer["role"] not in {"primary", "additional"}
            or not peer["admin_enabled"]
        ):
            raise CascadeNotFound(f"No available configuration {peer_id} for user {user_id}")
        try:
            return await self.get_api(peer["server_key"]).download_config(
                peer["cascade_peer_id"], peer["interface_id"]
            )
        except CascadeNotFound:
            self.db.set_client_peer_enabled(peer["cascade_peer_id"], False)
            raise

    async def get_admin_managed_config(
        self, user_id: int, peer_id: int
    ) -> tuple[dict[str, Any], bytes]:
        """Download a paid client's managed config without changing access state."""
        peer = self.db.get_admin_managed_config(peer_id, user_id)
        if not peer or peer["payment_status"] != "paid":
            raise CascadeNotFound(
                f"No paid managed configuration {peer_id} for user {user_id}"
            )
        try:
            content = await self.get_api(peer["server_key"]).download_config(
                peer["cascade_peer_id"], peer["interface_id"]
            )
        except CascadeNotFound:
            self.db.set_client_peer_enabled(peer["cascade_peer_id"], False)
            raise
        return peer, content

    async def build_additional_peer_name(
        self,
        user_id: int,
        config_name: str,
        server_key: str,
        interface_id: str,
    ) -> str:
        """Build a readable, bounded peer name and avoid live name collisions."""
        config_name = normalize_config_name(config_name)
        client = self.db.get_admin_client_details(user_id)
        identity = str((client or {}).get("telegram_username") or user_id).strip().lstrip("@")
        base_peer_name = f"{identity}_{config_name}"
        existing_names = {
            str(item.get("name") or "").strip().casefold()
            for item in await self.get_api(server_key).list_peers(interface_id)
        }
        if len(base_peer_name) <= 50 and base_peer_name.casefold() not in existing_names:
            return base_peer_name
        seed = f"{user_id}\0{config_name}\0{server_key}\0{interface_id}"
        for attempt in range(100):
            suffix = hashlib.sha256(f"{seed}\0{attempt}".encode()).hexdigest()[:8]
            candidate = f"{base_peer_name[:41].rstrip()}-{suffix}"
            if candidate.casefold() not in existing_names:
                return candidate
        raise CascadeError("Unable to build a unique Cascade peer name")

    async def create_additional_config(
        self,
        user_id: int,
        config_name: str,
        server_key: str,
        interface_id: str,
        client_group: str | None = None,
        *,
        reassign_existing_group: bool = True,
    ) -> dict[str, Any]:
        config_name = normalize_config_name(config_name)
        primary = self.db.get_primary_client_peer(user_id)
        expire_date = self.db.get_subscription_expiry(user_id)
        if not primary or not expire_date:
            raise CascadeError(
                "An additional configuration requires a primary peer and expiration date"
            )
        server = self.get_server(server_key)
        explicit_group = client_group is not None
        client_group = client_group or server.client_group
        if not server.enabled:
            raise CascadeError(f"Cascade server is disabled: {server_key}")
        api = self.get_api(server_key)
        interfaces = await api.list_interfaces()
        if not any(str(item.get("id") or "") == interface_id for item in interfaces):
            raise CascadeNotFound(
                f"Interface {interface_id} was not found on {server_key}"
            )
        peer_total = 0
        for interface in interfaces:
            current_interface_id = str(interface.get("id") or "")
            if current_interface_id:
                peer_total += len(await api.list_peers(current_interface_id))
        if peer_total >= server.max_peers:
            raise CascadeCapacityError(f"Cascade server {server_key} is full")

        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            original_groups: dict[int, str] = {}
            peer: dict[str, Any] | None = None
            saved = False
            try:
                if explicit_group and reassign_existing_group:
                    original_groups = await self._change_client_group_unlocked(
                        user_id, client_group
                    )
                elif explicit_group:
                    await self._verify_client_group_unlocked(user_id, client_group)
                current_interfaces = await api.list_interfaces()
                if not any(
                    str(item.get("id") or "") == interface_id
                    for item in current_interfaces
                ):
                    raise CascadeNotFound(
                        f"Interface {interface_id} was not found on {server_key}"
                    )
                current_total = 0
                for interface in current_interfaces:
                    current_interface_id = str(interface.get("id") or "")
                    if current_interface_id:
                        current_total += len(
                            await api.list_peers(current_interface_id)
                        )
                if current_total >= server.max_peers:
                    raise CascadeCapacityError(f"Cascade server {server_key} is full")
                peer_name = await self.build_additional_peer_name(
                    user_id, config_name, server_key, interface_id
                )
                if explicit_group:
                    peer = await api.create_peer(
                        peer_name,
                        expire_date,
                        interface_id,
                        client_group=client_group,
                    )
                else:
                    peer = await api.create_peer(peer_name, expire_date, interface_id)
                public_key = str(peer.get("publicKey") or "").strip()
                if not public_key:
                    raise CascadeError("Cascade create response has no public key")
                await api.download_config(str(peer["id"]), interface_id)
                is_future = (
                    datetime.fromisoformat(expire_date).replace(tzinfo=UTC)
                    > datetime.now(UTC)
                )
                if not is_future:
                    await api.disable_peer(str(peer["id"]), interface_id)
                saved = self.db.save_client_peer(
                    user_id=user_id,
                    server_key=server_key,
                    interface_id=interface_id,
                    cascade_peer_id=str(peer["id"]),
                    public_key=public_key,
                    peer_name=str(peer.get("name") or peer_name),
                    role="additional",
                    enabled=is_future,
                    config_name=config_name,
                    admin_enabled=True,
                    client_group=client_group,
                )
                if not saved:
                    raise CascadeError("Failed to persist the additional Cascade peer")
                stored = self.db.get_client_peer_by_cascade_id(
                    server_key, interface_id, str(peer["id"])
                )
                if not stored:
                    raise CascadeError("Stored additional Cascade peer could not be read")
                return stored
            except Exception:
                if peer and peer.get("id") and not saved:
                    try:
                        await api.delete_peer(str(peer["id"]), interface_id)
                    except Exception as delete_error:
                        logger.exception("Failed to compensate additional peer creation")
                        self.db.add_provisioning_task(
                            user_id,
                            "delete_cascade_peer",
                            {
                                "server_key": server_key,
                                "interface_id": interface_id,
                                "cascade_peer_id": str(peer["id"]),
                            },
                            str(delete_error),
                        )
                if original_groups:
                    try:
                        await self._restore_peer_groups(user_id, original_groups)
                    except CascadeError as rollback_error:
                        self.db.add_provisioning_task(
                            user_id,
                            "restore_peer_groups",
                            {
                                "groups": {
                                    str(key): value
                                    for key, value in original_groups.items()
                                }
                            },
                            str(rollback_error),
                        )
                raise

    async def set_additional_config_active(
        self, user_id: int, peer_id: int, active: bool
    ) -> dict[str, Any]:
        peer = self.db.get_client_peer(peer_id, user_id)
        if not peer or peer["role"] != "additional":
            raise CascadeNotFound(f"No additional configuration {peer_id}")
        api = self.get_api(peer["server_key"])
        try:
            await api.get_peer(peer["cascade_peer_id"], peer["interface_id"])
            expire_date = self.db.get_subscription_expiry(user_id)
            is_future = bool(
                expire_date
                and datetime.fromisoformat(expire_date).replace(tzinfo=UTC)
                > datetime.now(UTC)
            )
            enabled = active and is_future
            if enabled:
                await api.enable_peer(peer["cascade_peer_id"], peer["interface_id"])
            else:
                await api.disable_peer(peer["cascade_peer_id"], peer["interface_id"])
        except CascadeNotFound:
            self.db.set_client_peer_enabled(peer["cascade_peer_id"], False)
            raise
        if not self.db.set_config_admin_enabled(peer_id, user_id, active):
            raise CascadeError("Failed to persist configuration state")
        self.db.set_client_peer_enabled(peer["cascade_peer_id"], enabled)
        updated = self.db.get_client_peer(peer_id, user_id)
        if not updated:
            raise CascadeError("Updated configuration could not be read")
        return updated

    async def delete_additional_config(
        self, user_id: int, peer_id: int
    ) -> tuple[dict[str, Any], bool]:
        """Permanently delete an additional peer from Cascade and local storage."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            peer = self.db.get_client_peer(peer_id, user_id)
            if not peer or peer["role"] != "additional":
                raise CascadeNotFound(f"No additional configuration {peer_id}")
            api = self.get_api(str(peer["server_key"]))
            cascade_peer_missing = False
            try:
                await api.get_peer(
                    str(peer["cascade_peer_id"]), str(peer["interface_id"])
                )
            except CascadeNotFound:
                cascade_peer_missing = True
            if not cascade_peer_missing:
                try:
                    await api.delete_peer(
                        str(peer["cascade_peer_id"]), str(peer["interface_id"])
                    )
                except CascadeNotFound:
                    cascade_peer_missing = True
            if not self.db.delete_additional_config(peer_id, user_id):
                raise CascadeError("Failed to remove the deleted configuration locally")
            return peer, cascade_peer_missing

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
                should_enable = is_future and bool(peer.get("admin_enabled", 1))
                if should_enable:
                    await api.enable_peer(peer["cascade_peer_id"], peer["interface_id"])
                else:
                    await api.disable_peer(peer["cascade_peer_id"], peer["interface_id"])
                self.db.set_client_peer_enabled(peer["cascade_peer_id"], should_enable)
                result["updated"] += 1
            except CascadeNotFound as exc:
                if peer["role"] == "primary":
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
                    "Skipping missing additional Cascade peer %s for user %s",
                    peer["cascade_peer_id"],
                    user_id,
                )
            except Exception as exc:
                result["failed"] += 1
                logger.error("Failed to sync Cascade peer %s: %s", peer["cascade_peer_id"], exc)
        return result

    async def close(self) -> None:
        await asyncio.gather(*(api.close() for api in self.apis.values()))
