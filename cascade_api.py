import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

import httpx

from config import CASCADE_REQUEST_TIMEOUT, CASCADE_SERVERS_FILE
from database import (
    MANAGED_CONFIG_ROLE,
    ActiveSubscriptionError,
    Database,
    normalize_config_name,
)
from runtime_metrics import RuntimeMetrics

logger = logging.getLogger(__name__)


class CascadeError(RuntimeError):
    """Base error raised by Cascade integration."""


class CascadeNotFound(CascadeError):
    """Requested Cascade resource does not exist."""


class CascadeCapacityError(CascadeError):
    """No configured Cascade server has free peer capacity."""


@dataclass(frozen=True)
class ClientDeletionResult:
    """Summarize an administrative client deletion attempt."""

    deleted: int = 0
    already_missing: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ManagedConfigRebindResult:
    """Describe a manual replacement of one managed config binding."""

    previous: dict[str, Any]
    current: dict[str, Any]
    sync: dict[str, int]


@dataclass(frozen=True)
class ClientInterface:
    interface_id: str
    name: str
    description: str


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
    client_interfaces: tuple[ClientInterface, ...] | None = None

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
        client_interfaces = None
        if "client_interfaces" in item:
            raw_interfaces = item["client_interfaces"]
            if not isinstance(raw_interfaces, list) or len(raw_interfaces) > 10:
                raise CascadeError("client_interfaces must be a list of at most 10 entries")
            parsed_interfaces = []
            interface_ids = set()
            for entry in raw_interfaces:
                if not isinstance(entry, dict):
                    raise CascadeError("Each client interface must be an object")
                if "interface_name" in entry:
                    raise CascadeError(
                        f"client_interfaces.interface_name is no longer supported for {server_key}; "
                        "use interface_id with the Cascade interface ID"
                    )
                values = []
                for field, limit in (("interface_id", 64), ("name", 64), ("description", 240)):
                    value = entry.get(field)
                    if (
                        not isinstance(value, str) or not value.strip()
                        or len(value.strip()) > limit
                        or any(ord(char) < 32 or ord(char) == 127 for char in value)
                    ):
                        raise CascadeError(f"Invalid client interface {field} for {server_key}")
                    values.append(value.strip())
                if values[0] in interface_ids:
                    raise CascadeError(f"Duplicate client interface ID for {server_key}")
                interface_ids.add(values[0])
                parsed_interfaces.append(ClientInterface(*values))
            client_interfaces = tuple(parsed_interfaces)
        server = CascadeServer(
            client_interfaces=client_interfaces,
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

    def get_client_production_locations(self) -> list[dict[str, str]]:
        """Return enabled locations bound to configured production interfaces."""
        return [
            {
                "server_key": server.server_key,
                "server_name": server.server_name or server.server_key,
                "interface_id": server.interface_id,
            }
            for server in self.get_enabled_servers()
            if server.client_interfaces is None or server.client_interfaces
        ]

    async def get_client_interfaces(self, server_key: str) -> list[dict[str, str]]:
        """Validate the explicit client allowlist against opaque Cascade IDs."""
        server = self.get_server(server_key)
        if not server.enabled:
            raise CascadeError("Location is disabled")
        if not server.client_interfaces:
            return []
        live = await self.get_api(server_key).list_interfaces()
        result = []
        for option in server.client_interfaces:
            matches = [item for item in live if item.get("id") == option.interface_id]
            if len(matches) != 1 or not matches[0].get("id"):
                raise CascadeError(f"Client interface {option.interface_id} is missing or ambiguous")
            result.append({
                "interface_id": str(matches[0]["id"]),
                "name": option.name,
                "description": option.description,
            })
        if len({item["interface_id"] for item in result}) != len(result):
            raise CascadeError("Client interfaces resolve to duplicate IDs")
        return result

    async def validate_client_interface(
        self, server_key: str, interface_id: str
    ) -> None:
        server = self.get_server(server_key)
        if not server.enabled:
            raise CascadeError("Location is disabled")
        if server.client_interfaces is None:
            if interface_id != server.interface_id:
                raise CascadeError("Self-service requires the configured production interface")
            return
        options = await self.get_client_interfaces(server_key)
        if not any(
            item["interface_id"] == interface_id
            for item in options
        ):
            raise CascadeError("Selected client interface changed or is no longer allowed")

    async def get_client_interface_annotation(
        self, server_key: str, interface_id: str
    ) -> dict[str, str] | None:
        """Never infer a version from an unknown or replaced interface."""
        try:
            options = await self.get_client_interfaces(server_key)
        except CascadeError:
            return None
        return next((item for item in options if item["interface_id"] == interface_id), None)

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
        """Validate every enabled server and report disabled servers."""

        async def check(server: CascadeServer) -> tuple[str, str]:
            if not server.enabled:
                return server.server_key, "disabled"
            try:
                health = await self.get_api(server.server_key).health()
                if health.get("status") != "ok":
                    raise CascadeError(
                        f"Unexpected health status: {health.get('status')}"
                    )
                interface = await self.get_api(server.server_key).get_interface()
                if str(interface.get("id")) != server.interface_id:
                    raise CascadeError("Configured interface ID does not match API response")
                if server.client_interfaces:
                    await self.get_client_interfaces(server.server_key)
                for group_name in server.selectable_client_groups:
                    await self.get_api(server.server_key).resolve_client_group_id(
                        group_name
                    )
                return server.server_key, "ok"
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
            if not peer or peer["role"] != MANAGED_CONFIG_ROLE:
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
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
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

    async def get_managed_config(self, user_id: int, peer_id: int) -> bytes:
        peer = self.db.get_client_peer(peer_id, user_id)
        if (
            not peer
            or peer["role"] != MANAGED_CONFIG_ROLE
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
        if not peer or not self.db.has_active_access(user_id):
            raise CascadeNotFound(
                f"No active managed configuration {peer_id} for user {user_id}"
            )
        try:
            content = await self.get_api(peer["server_key"]).download_config(
                peer["cascade_peer_id"], peer["interface_id"]
            )
        except CascadeNotFound:
            self.db.set_client_peer_enabled(peer["cascade_peer_id"], False)
            raise
        return peer, content

    async def rebind_managed_config(
        self,
        user_id: int,
        peer_id: int,
        server_key: str,
        interface_id: str,
        public_key: str,
    ) -> ManagedConfigRebindResult:
        """Repair one missing managed config by binding an existing Cascade peer."""
        public_key = public_key.strip()
        if not public_key:
            raise CascadeError("Public key is required")

        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            previous = self.db.get_client_peer(peer_id, user_id)
            if (
                not previous
                or previous.get("role") != MANAGED_CONFIG_ROLE
                or not previous.get("server_key")
                or not previous.get("interface_id")
                or not previous.get("cascade_peer_id")
            ):
                raise CascadeNotFound(f"No managed configuration {peer_id}")
            if not previous.get("admin_enabled"):
                raise CascadeError("Deactivated configurations cannot be rebound")
            if not self.db.has_active_access(user_id):
                raise CascadeError("Active access is required")

            try:
                await self.get_api(str(previous["server_key"])).get_peer(
                    str(previous["cascade_peer_id"]),
                    str(previous["interface_id"]),
                )
            except CascadeNotFound:
                pass
            else:
                raise CascadeError("The existing Cascade peer is still available")

            target = await self.inspect_rebind_target(
                server_key, interface_id, public_key
            )
            target_id = str(target["cascade_peer_id"])
            target_public_key = str(target["public_key"])
            client_group = str(target["client_group"])

            other_configs = [
                item
                for item in self.db.get_managed_client_configs(user_id)
                if int(item["id"]) != peer_id
            ]
            if other_configs:
                groups = {
                    str(item.get("client_group") or "").strip().casefold()
                    for item in other_configs
                }
                if "" in groups or len(groups) != 1:
                    raise CascadeError("Client configurations have no confirmed group")
                if client_group.casefold() not in groups:
                    raise CascadeError("Cascade peer client group does not match the client")

            rebound = self.db.rebind_managed_config(
                peer_id,
                user_id,
                server_key=server_key,
                interface_id=interface_id,
                cascade_peer_id=target_id,
                public_key=target_public_key,
                peer_name=str(target["peer_name"]),
                client_group=client_group,
            )
            if not rebound:
                raise CascadeError("Cascade peer is already bound or the config changed")

            access = self.db.get_client_access_state(user_id)
            expire_date = access.cascade_expiry or "1970-01-01 00:00:00"
            sync = await self._sync_user_access_unlocked(user_id, expire_date)
            current = self.db.get_client_peer(peer_id, user_id)
            if not current:
                raise CascadeError("Rebound managed configuration could not be read")
            return ManagedConfigRebindResult(
                previous=dict(previous),
                current=current,
                sync=sync,
            )

    async def inspect_rebind_target(
        self,
        server_key: str,
        interface_id: str,
        public_key: str,
    ) -> dict[str, str]:
        """Validate and describe one existing Cascade peer without binding it."""
        public_key = public_key.strip()
        if not public_key:
            raise CascadeError("Public key is required")
        server = self.get_server(server_key)
        if not server.enabled:
            raise CascadeError(f"Cascade server is disabled: {server_key}")
        api = self.get_api(server_key)
        interfaces = await api.list_interfaces()
        if not any(str(item.get("id") or "") == interface_id for item in interfaces):
            raise CascadeNotFound(
                f"Interface {interface_id} was not found on {server_key}"
            )

        matches = [
            item
            for item in await api.list_peers(interface_id)
            if str(item.get("publicKey") or "").strip() == public_key
        ]
        if not matches:
            raise CascadeNotFound("No Cascade peer has the supplied public key")
        if len(matches) != 1:
            raise CascadeError("Multiple Cascade peers have the supplied public key")
        listed_peer = matches[0]
        target_id = str(listed_peer.get("id") or "").strip()
        if not target_id:
            raise CascadeError("Matched Cascade peer has no ID")

        target = {**listed_peer, **await api.get_peer(target_id, interface_id)}
        target_public_key = str(target.get("publicKey") or public_key).strip()
        if target_public_key != public_key:
            raise CascadeError("Cascade peer public key changed during validation")
        peer_type = str(target.get("peerType") or "").strip().casefold()
        if peer_type and peer_type != "client":
            raise CascadeError("Only client peers can be bound to configurations")
        group_id = str(target.get("groupId") or "").strip()
        if not group_id:
            raise CascadeError("Cascade peer has no client group")
        client_group = await api.resolve_client_group_name(group_id)
        if not client_group:
            raise CascadeError("Cascade peer client group could not be resolved")
        allowed_groups = {
            value.casefold(): value for value in server.selectable_client_groups
        }
        if client_group.casefold() not in allowed_groups:
            raise CascadeError("Cascade peer client group is not assignable")
        await api.download_config(target_id, interface_id)
        return {
            "cascade_peer_id": target_id,
            "public_key": target_public_key,
            "peer_name": str(target.get("name") or target_id),
            "client_group": allowed_groups[client_group.casefold()],
        }

    async def build_managed_peer_name(
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

    async def create_managed_config(
        self,
        user_id: int,
        config_name: str,
        server_key: str,
        interface_id: str,
        client_group: str | None = None,
        *,
        reassign_existing_group: bool = True,
        self_service_limit: int | None = None,
        production_only: bool = False,
    ) -> tuple[dict[str, Any], bytes]:
        config_name = normalize_config_name(config_name)
        access = self.db.get_client_access_state(user_id)
        expire_date = access.cascade_expiry or access.paid_expiry
        if not access.active or not expire_date:
            raise CascadeError("Active access with an expiration date is required")
        server = self.get_server(server_key)
        if production_only or self_service_limit is not None:
            await self.validate_client_interface(server_key, interface_id)
        if self_service_limit is not None:
            if self.db.is_client_banned(user_id):
                raise CascadeError("Banned clients cannot create configurations")
            if not self.db.has_active_access(user_id):
                raise CascadeError("Active access is required")
            if self.db.count_managed_configs(user_id) >= self_service_limit:
                raise CascadeCapacityError("Configuration limit reached")
            reassign_existing_group = False
        existing_configs = self.db.get_managed_client_configs(user_id)
        if existing_configs:
            inherited_groups = {
                str(item["client_group"])
                for item in existing_configs
                if item.get("client_group")
            }
            if len(inherited_groups) != 1 or len(inherited_groups) != len(
                {str(item.get("client_group")) for item in existing_configs}
            ):
                raise CascadeError("Client configurations do not have one confirmed group")
            if client_group is None:
                client_group = next(iter(inherited_groups))
        else:
            client_group = server.client_group
            reassign_existing_group = False
        explicit_group = client_group is not None
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
                access = self.db.get_client_access_state(user_id)
                expire_date = access.cascade_expiry or access.paid_expiry
                if not access.active or not expire_date:
                    raise CascadeError("Active access with an expiration date is required")
                if self_service_limit is not None:
                    if self.db.is_client_banned(user_id):
                        raise CascadeError("Banned clients cannot create configurations")
                    if not self.db.has_active_access(user_id):
                        raise CascadeError("Active access is required")
                    if self.db.count_managed_configs(user_id) >= self_service_limit:
                        raise CascadeCapacityError("Configuration limit reached")
                    current_server = self.get_server(server_key)
                    if not current_server.enabled:
                        raise CascadeError("Production location is no longer available")
                    current_configs = self.db.get_managed_client_configs(user_id)
                    inherited_groups = {
                        str(item["client_group"])
                        for item in current_configs
                        if item.get("client_group")
                    }
                    if current_configs and (
                        inherited_groups != {str(client_group)}
                        or any(not item.get("client_group") for item in current_configs)
                    ):
                        raise CascadeError("Client group changed during creation")
                current_configs = self.db.get_managed_client_configs(user_id)
                if current_configs and explicit_group and reassign_existing_group:
                    original_groups = await self._change_client_group_unlocked(
                        user_id, client_group
                    )
                elif current_configs and explicit_group:
                    await self._verify_client_group_unlocked(user_id, client_group)
                if production_only or self_service_limit is not None:
                    await self.validate_client_interface(server_key, interface_id)
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
                peer_name = await self.build_managed_peer_name(
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
                config_content = await api.download_config(str(peer["id"]), interface_id)
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
                    role=MANAGED_CONFIG_ROLE,
                    enabled=is_future,
                    config_name=config_name,
                    admin_enabled=True,
                    client_group=client_group,
                )
                if not saved:
                    raise CascadeError("Failed to persist the managed Cascade peer")
                stored = self.db.get_client_peer_by_cascade_id(
                    server_key, interface_id, str(peer["id"])
                )
                if not stored:
                    raise CascadeError("Stored managed Cascade peer could not be read")
                return stored, config_content
            except Exception:
                if peer and peer.get("id") and not saved:
                    try:
                        await api.delete_peer(str(peer["id"]), interface_id)
                    except Exception as delete_error:
                        logger.exception("Failed to compensate managed peer creation")
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

    async def set_managed_config_active(
        self, user_id: int, peer_id: int, active: bool
    ) -> dict[str, Any]:
        peer = self.db.get_client_peer(peer_id, user_id)
        if not peer or peer["role"] != MANAGED_CONFIG_ROLE:
            raise CascadeNotFound(f"No managed configuration {peer_id}")
        api = self.get_api(peer["server_key"])
        try:
            await api.get_peer(peer["cascade_peer_id"], peer["interface_id"])
            access = self.db.get_client_access_state(user_id)
            enabled = active and access.active
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

    async def delete_managed_config(
        self, user_id: int, peer_id: int
    ) -> tuple[dict[str, Any], bool]:
        """Permanently delete a managed peer from Cascade and local storage."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            peer = self.db.get_client_peer(peer_id, user_id)
            if not peer or peer["role"] != MANAGED_CONFIG_ROLE:
                raise CascadeNotFound(f"No managed configuration {peer_id}")
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
            if not self.db.delete_managed_config(peer_id, user_id):
                raise CascadeError("Failed to remove the deleted configuration locally")
            return peer, cascade_peer_missing

    async def delete_client(
        self,
        user_id: int,
        admin_id: int,
        *,
        allow_active_subscription: bool = False,
    ) -> ClientDeletionResult:
        """Delete all Cascade peers and then the client's operational database state."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            client = self.db.get_admin_client_details(user_id)
            if not client:
                raise CascadeNotFound(f"No client profile for user {user_id}")
            paid_active = self.db.has_active_subscription(user_id)
            if paid_active and not allow_active_subscription:
                raise ActiveSubscriptionError(
                    f"Client {user_id} still has an active paid subscription"
                )
            subscription_snapshot = {
                "expire_date": client.get("expire_date"),
                "is_active": bool(client.get("is_active")),
                "payment_status": client.get("payment_status"),
                "payment_method": client.get("payment_method"),
            }

            deleted = 0
            already_missing = 0
            failed = 0
            for peer in self.db.get_client_cascade_peers(user_id):
                try:
                    api = self.get_api(str(peer["server_key"]))
                    try:
                        await api.get_peer(
                            str(peer["cascade_peer_id"]), str(peer["interface_id"])
                        )
                    except CascadeNotFound:
                        already_missing += 1
                        continue
                    try:
                        await api.delete_peer(
                            str(peer["cascade_peer_id"]), str(peer["interface_id"])
                        )
                        deleted += 1
                    except CascadeNotFound:
                        already_missing += 1
                except Exception as exc:
                    failed += 1
                    logger.error(
                        "Failed to delete Cascade peer %s for user %s: %s",
                        peer.get("cascade_peer_id"),
                        user_id,
                        type(exc).__name__,
                    )

            result = ClientDeletionResult(
                deleted=deleted,
                already_missing=already_missing,
                failed=failed,
            )
            if failed:
                self.db.log_admin_client_deletion(
                    admin_id,
                    user_id,
                    "admin_delete_client_failed",
                    deleted=deleted,
                    already_missing=already_missing,
                    failed=failed,
                    forced_without_refund=bool(
                        paid_active and allow_active_subscription
                    ),
                    subscription_snapshot=subscription_snapshot,
                )
                return result

            try:
                removed = self.db.delete_client_operational_data(
                    admin_id,
                    user_id,
                    deleted=deleted,
                    already_missing=already_missing,
                    allow_active_subscription=allow_active_subscription,
                )
            except ActiveSubscriptionError:
                raise
            except Exception as exc:
                raise CascadeError("Failed to remove client data locally") from exc
            if removed is None:
                raise CascadeNotFound(f"No client profile for user {user_id}")
            return result

    async def sync_user_access(self, user_id: int, expire_date: str) -> dict[str, int]:
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            access = self.db.get_client_access_state(user_id)
            if access.source == "complimentary" and access.cascade_expiry:
                expire_date = access.cascade_expiry
            return await self._sync_user_access_unlocked(user_id, expire_date)

    async def sync_client_state(self, user_id: int) -> dict[str, int]:
        """Synchronize every peer from the client's current subscription and ban state."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            if not self.db.get_admin_client_details(user_id):
                raise CascadeNotFound(f"No client profile for user {user_id}")
            access = self.db.get_client_access_state(user_id)
            expire_date = access.cascade_expiry or "1970-01-01 00:00:00"
            return await self._sync_user_access_unlocked(user_id, expire_date)

    async def set_client_complimentary(
        self, user_id: int, admin_id: int, enabled: bool
    ) -> dict[str, int]:
        """Set complimentary access and synchronize existing configurations."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            client = self.db.get_admin_client_details(user_id)
            if not client or not self.db.set_client_complimentary(
                user_id, admin_id, enabled
            ):
                raise CascadeNotFound(f"No client profile for user {user_id}")
            access = self.db.get_client_access_state(user_id)
            expiry = access.cascade_expiry or "1970-01-01 00:00:00"
            result = await self._sync_user_access_unlocked(user_id, expiry)
            result["created"] = 0
            self.db.log_client_state_sync(
                admin_id,
                user_id,
                "admin_enable_complimentary_sync"
                if enabled
                else "admin_disable_complimentary_sync",
                result,
            )
            return result

    async def ensure_client_access(self, user_id: int) -> dict[str, int]:
        """Synchronize the current effective access for existing configurations."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            client = self.db.get_admin_client_details(user_id)
            access = self.db.get_client_access_state(user_id)
            if not client or not access.active:
                raise CascadeError("Client access is not active")
            result = await self._sync_user_access_unlocked(
                user_id, str(access.cascade_expiry)
            )
            result["created"] = 0
            return result

    async def activate_preadded_client(
        self, user_id: int, username: str | None
    ) -> dict[str, Any] | None:
        """Verify a pre-added Telegram ID and reconcile access under one user lock."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            verification = self.db.verify_preadded_client(user_id, username)
            if not verification:
                return None
            access = self.db.get_client_access_state(user_id)
            result = {
                "total": 0,
                "updated": 0,
                "missing": 0,
                "failed": 0,
                "created": 0,
            }
            error: str | None = None
            if access.active:
                try:
                    result = await self._sync_user_access_unlocked(
                        user_id, str(access.cascade_expiry)
                    )
                    result["created"] = 0
                except Exception as exc:
                    logger.exception(
                        "Failed to activate verified pre-added client %s", user_id
                    )
                    result = {
                        "total": 1,
                        "updated": 0,
                        "missing": 0,
                        "failed": 1,
                        "created": 0,
                    }
                    error = str(exc)
            self.db.log_identity_activation_sync(user_id, result)
            return {"verification": verification, "sync": result, "error": error}

    async def set_client_ban(
        self,
        user_id: int,
        admin_id: int,
        banned: bool,
        reason: str | None = None,
    ) -> dict[str, int]:
        """Persist a ban transition and immediately synchronize all managed peers."""
        user_lock = self._user_locks.get(user_id)
        if user_lock is None:
            user_lock = asyncio.Lock()
            self._user_locks[user_id] = user_lock
        async with user_lock:
            if not self.db.get_admin_client_details(user_id):
                raise CascadeNotFound(f"No client profile for user {user_id}")
            if not self.db.set_client_ban(user_id, admin_id, banned, reason):
                raise CascadeNotFound(f"No client profile for user {user_id}")
            access = self.db.get_client_access_state(user_id)
            expire_date = access.cascade_expiry or "1970-01-01 00:00:00"
            result = await self._sync_user_access_unlocked(user_id, expire_date)
            self.db.log_client_state_sync(
                admin_id,
                user_id,
                "admin_ban_client_sync" if banned else "admin_unban_client_sync",
                result,
            )
            return result

    async def _sync_user_access_unlocked(
        self, user_id: int, expire_date: str
    ) -> dict[str, int]:
        peers = self.db.get_client_peers(user_id, bound_only=True)
        result = {"total": len(peers), "updated": 0, "missing": 0, "failed": 0}
        is_future = datetime.fromisoformat(expire_date).replace(tzinfo=UTC) > datetime.now(UTC)
        is_banned = self.db.is_client_banned(user_id)
        for peer in peers:
            try:
                api = self.get_api(peer["server_key"])
                await api.update_expiry(peer["cascade_peer_id"], expire_date, peer["interface_id"])
                should_enable = (
                    is_future and not is_banned and bool(peer.get("admin_enabled", 1))
                )
                if should_enable:
                    await api.enable_peer(peer["cascade_peer_id"], peer["interface_id"])
                else:
                    await api.disable_peer(peer["cascade_peer_id"], peer["interface_id"])
                self.db.set_client_peer_enabled(peer["cascade_peer_id"], should_enable)
                result["updated"] += 1
            except CascadeNotFound:
                self.db.set_client_peer_enabled(peer["cascade_peer_id"], False)
                result["missing"] += 1
                logger.warning(
                    "Skipping missing managed Cascade peer %s for user %s",
                    peer["cascade_peer_id"],
                    user_id,
                )
            except Exception as exc:
                result["failed"] += 1
                logger.error("Failed to sync Cascade peer %s: %s", peer["cascade_peer_id"], exc)
        return result

    async def close(self) -> None:
        await asyncio.gather(*(api.close() for api in self.apis.values()))
