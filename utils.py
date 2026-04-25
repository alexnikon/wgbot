import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def generate_peer_name(
    telegram_username: Optional[str] = None, user_id: Optional[int] = None
) -> str:
    """
    Generate a peer name in the username_telegramID format.

    Args:
        telegram_username: Telegram username
        user_id: Telegram user ID

    Returns:
        Peer name in username_telegramID format
    """
    if telegram_username:
        peer_name = f"{telegram_username}_{user_id}"
    else:
        peer_name = f"user_{user_id}"

    if len(peer_name) > 50:
        peer_name = peer_name[:50]

    return peer_name


def parse_date_flexible(date_str: str) -> datetime.datetime:
    """
    Parse a date string, supporting multiple formats.

    Args:
        date_str: Date string in "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"

    Returns:
        Parsed datetime object

    Raises:
        ValueError: If the date format is not recognized
    """
    if not date_str:
        raise ValueError("Empty date string")

    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid date format: {date_str}") from exc


def format_date_for_user(date_str: str) -> str:
    """
    Format a date for user display in DD-MM-YYYY.

    Args:
        date_str: Date string in "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"

    Returns:
        Formatted string in "DD-MM-YYYY"
    """
    try:
        dt = parse_date_flexible(date_str)
        return dt.strftime("%d-%m-%Y")
    except (ValueError, TypeError) as exc:
        logger.error(f"Failed to format date {date_str}: {exc}")
        return date_str


class ClientsJsonManager:
    def __init__(self, file_path: str):
        self.file_path = file_path

    def _read_clients(self) -> Any:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as file:
                return json.load(file)
        except (json.JSONDecodeError, IOError) as exc:
            logger.error(f"Error reading clients.json: {exc}")
            return []

    def _write_clients(self, clients: Any) -> bool:
        try:
            with open(self.file_path, "w", encoding="utf-8") as file:
                json.dump(clients, file, indent=2, ensure_ascii=False)
            return True
        except IOError as exc:
            logger.error(f"Error writing clients.json: {exc}")
            return False

    def _is_unified_format(self, data: Any) -> bool:
        return isinstance(data, dict) and isinstance(data.get("clients"), list)

    def _base_client_id(self, client_id: str, username: str | None = None) -> str:
        return (username or client_id or "").strip()

    def _create_unified_client(
        self,
        client_id: str,
        public_key: str,
        telegram_user_id: int | None = None,
        username: str | None = None,
    ) -> Dict[str, Any]:
        base_client_id = self._base_client_id(client_id, username)
        return {
            "telegramId": telegram_user_id if telegram_user_id is not None else "",
            "username": username or "",
            "promo": 0,
            "peers": [
                {
                    "role": "bot",
                    "clientId": base_client_id,
                    "publicKey": public_key,
                },
                {"role": "manual", "clientId": "", "publicKey": ""},
                {"role": "manual", "clientId": "", "publicKey": ""},
            ],
        }

    def _ensure_unified_client_shape(self, client: Dict[str, Any]) -> None:
        peers = client.get("peers")
        if not isinstance(peers, list):
            peers = []
            client["peers"] = peers

        if not peers:
            peers.append({"role": "bot", "clientId": "", "publicKey": ""})

        peers[0]["role"] = "bot"
        peers[0]["clientId"] = str(peers[0].get("clientId") or "").strip()
        peers[0]["publicKey"] = str(peers[0].get("publicKey") or "").strip()

        while len(peers) < 3:
            peers.append({"role": "manual", "clientId": "", "publicKey": ""})

        for peer in peers[1:]:
            peer["role"] = "manual"
            peer["clientId"] = str(peer.get("clientId") or "").strip()
            peer["publicKey"] = str(peer.get("publicKey") or "").strip()

        if "promo" not in client:
            client["promo"] = 0
        if "username" not in client:
            client["username"] = ""
        if "telegramId" not in client:
            client["telegramId"] = ""

    def _find_unified_client(
        self,
        clients: List[Dict[str, Any]],
        client_id: str,
        telegram_user_id: int | None = None,
        public_key: str | None = None,
    ) -> Dict[str, Any] | None:
        if telegram_user_id is not None:
            for client in clients:
                if str(client.get("telegramId")) == str(telegram_user_id):
                    return client

        for client in clients:
            peers = client.get("peers") or []
            for peer in peers:
                if public_key and peer.get("publicKey") == public_key:
                    return client
                if client_id and peer.get("clientId") == client_id:
                    return client

        return None

    def add_update_client(
        self,
        client_id: str,
        public_key: str,
        force_write: bool = False,
        telegram_user_id: int | None = None,
        username: str | None = None,
    ) -> bool:
        """
        Add or update a client in the JSON file.

        Args:
            client_id: Telegram username or ID used as identifier
            public_key: WireGuard public key
            force_write: Force rewriting the file even when values match
        """
        data = self._read_clients()

        if self._is_unified_format(data):
            clients = data["clients"]
            client = self._find_unified_client(
                clients,
                client_id=client_id,
                telegram_user_id=telegram_user_id,
                public_key=public_key,
            )
            if client is None:
                clients.append(
                    self._create_unified_client(
                        client_id,
                        public_key,
                        telegram_user_id=telegram_user_id,
                        username=username,
                    )
                )
                return self._write_clients(data)

            self._ensure_unified_client_shape(client)
            old_username = client.get("username")
            old_telegram_id = client.get("telegramId")
            old_client_id = client["peers"][0].get("clientId")
            old_public_key = client["peers"][0].get("publicKey")

            if telegram_user_id is not None:
                client["telegramId"] = telegram_user_id
            if username is not None:
                client["username"] = username or ""

            client["peers"][0]["role"] = "bot"
            client["peers"][0]["clientId"] = self._base_client_id(client_id, username)
            client["peers"][0]["publicKey"] = public_key

            changed = (
                old_username != client.get("username")
                or old_telegram_id != client.get("telegramId")
                or old_client_id != client["peers"][0].get("clientId")
                or old_public_key != public_key
                or force_write
            )
            if changed:
                return self._write_clients(data)
            return True

        clients = data if isinstance(data, list) else []

        updated = False
        found = False
        for client in clients:
            if not isinstance(client, dict):
                continue
            if client.get("clientId") == client_id:
                found = True
                old_public_key = client.get("publicKey")
                client["publicKey"] = public_key
                if old_public_key != public_key or force_write:
                    updated = True
                break

        if not found:
            clients.append({"clientId": client_id, "publicKey": public_key})
            updated = True

        if updated or force_write:
            return self._write_clients(clients)
        return True

    def remove_client(self, client_id: str) -> bool:
        """
        Remove a client from the JSON file by client_id.
        """
        data = self._read_clients()

        if self._is_unified_format(data):
            # Unified records are keyed by telegramId and can contain manual peers.
            # Keep the record intact to avoid losing manual bindings or promo data.
            return True

        clients = data if isinstance(data, list) else []
        original_length = len(clients)
        new_clients = [
            client
            for client in clients
            if isinstance(client, dict) and client.get("clientId") != client_id
        ]

        if len(new_clients) < original_length:
            return self._write_clients(new_clients)

        return True


class PromoManager:
    def __init__(self, promo_file_path: str, clients_json_path: str | None = None):
        self.promo_file_path = promo_file_path
        self.clients_json_path = clients_json_path

    def _get_unified_promo(self, user_id: int) -> Optional[int]:
        if not self.clients_json_path or not os.path.exists(self.clients_json_path):
            return None

        try:
            with open(self.clients_json_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, dict) or not isinstance(data.get("clients"), list):
                return None

            for client in data["clients"]:
                if not isinstance(client, dict):
                    continue
                if str(client.get("telegramId")) == str(user_id):
                    promo = client.get("promo", 0)
                    return int(promo or 0)
        except Exception as exc:
            logger.error(f"Error reading promo from clients.json: {exc}")

        return None

    def _promo_to_factor(self, value: int) -> float:
        if value <= 0:
            return 1.0
        if value <= 100:
            return 1.0 - (value / 100.0)
        return value / 100.0

    def get_user_promo_factor(self, user_id: int) -> float:
        """
        Return a user-specific price multiplier.

        If value <= 100, it is a discount percent (20 -> 0.8).
        If value > 100, it is a markup percent (150 -> 1.5).
        Reads promo.txt on each call to support hot reload.
        """
        unified_promo = self._get_unified_promo(user_id)
        if unified_promo is not None:
            return self._promo_to_factor(unified_promo)

        if not os.path.exists(self.promo_file_path):
            return 1.0

        try:
            with open(self.promo_file_path, "r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if not line or "=" not in line:
                        continue

                    try:
                        uid_str, val_str = line.split("=")
                        uid = int(uid_str.strip())
                        val = int(val_str.strip())

                        if uid == user_id:
                            return self._promo_to_factor(val)
                    except ValueError:
                        continue
        except Exception:
            # Fail open: ignore promo file errors and return the default multiplier.
            pass

        return 1.0
