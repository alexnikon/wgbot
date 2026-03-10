import datetime
import json
import logging
import os
from typing import Dict, List, Optional

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

    def _read_clients(self) -> List[Dict[str, str]]:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as file:
                return json.load(file)
        except (json.JSONDecodeError, IOError) as exc:
            logger.error(f"Error reading clients.json: {exc}")
            return []

    def _write_clients(self, clients: List[Dict[str, str]]) -> bool:
        try:
            with open(self.file_path, "w", encoding="utf-8") as file:
                json.dump(clients, file, indent=2, ensure_ascii=False)
            return True
        except IOError as exc:
            logger.error(f"Error writing clients.json: {exc}")
            return False

    def add_update_client(
        self, client_id: str, public_key: str, force_write: bool = False
    ) -> bool:
        """
        Add or update a client in the JSON file.

        Args:
            client_id: Telegram username or ID used as identifier
            public_key: WireGuard public key
            force_write: Force rewriting the file even when values match
        """
        clients = self._read_clients()

        updated = False
        found = False
        for client in clients:
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
        clients = self._read_clients()
        original_length = len(clients)
        new_clients = [client for client in clients if client.get("clientId") != client_id]

        if len(new_clients) < original_length:
            return self._write_clients(new_clients)

        return True


class PromoManager:
    def __init__(self, promo_file_path: str):
        self.promo_file_path = promo_file_path

    def get_user_promo_factor(self, user_id: int) -> float:
        """
        Return a user-specific price multiplier.

        If value <= 100, it is a discount percent (20 -> 0.8).
        If value > 100, it is a markup percent (150 -> 1.5).
        Reads promo.txt on each call to support hot reload.
        """
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
                            if val <= 100:
                                return 1.0 - (val / 100.0)
                            return val / 100.0
                    except ValueError:
                        continue
        except Exception:
            # Fail open: ignore promo file errors and return the default multiplier.
            pass

        return 1.0
