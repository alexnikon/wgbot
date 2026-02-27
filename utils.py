import uuid
import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)

def generate_peer_name(telegram_username: Optional[str] = None, user_id: Optional[int] = None) -> str:
    """
    Generate a unique peer name in the username_telegramID format.

    Args:
        telegram_username: Telegram username
        user_id: Telegram user ID

    Returns:
        Peer name in username_telegramID format
    """
    # Use username_telegramID for better identification
    if telegram_username:
        peer_name = f"{telegram_username}_{user_id}"
    else:
        # If username is missing, use only user_id
        peer_name = f"user_{user_id}"
    
    # Enforce max length
    if len(peer_name) > 50:
        peer_name = peer_name[:50]
    
    return peer_name

def generate_uuid() -> str:
    """Generate a UUID for a job."""
    return str(uuid.uuid4())

def format_datetime(dt: datetime.datetime) -> str:
    """
    Format datetime for the WGDashboard API.
    
    Args:
        dt: datetime object
        
    Returns:
        Formatted date string
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def parse_datetime(date_str: str) -> datetime.datetime:
    """
    Parse a date string from the WGDashboard API.
    
    Args:
        date_str: Date string
        
    Returns:
        datetime object
    """
    return datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")

def parse_date_flexible(date_str: str) -> datetime.datetime:
    """
    Parse a date string, supporting multiple formats.
    
    Args:
        date_str: Date string in "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
        
    Returns:
        datetime object
        
    Raises:
        ValueError: If date format is not recognized
    """
    if not date_str:
        raise ValueError("Empty date string")
    
    # Try format with time
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    
    # Try format without time
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}")

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
    except (ValueError, TypeError) as e:
        logger.error(f"Failed to format date {date_str}: {e}")
        return date_str  # Return original on error

def calculate_expiry_date(days: int = 30) -> str:
    """
    Calculate expiration date after the specified number of days.
    
    Args:
        days: Days until expiration
        
    Returns:
        Expiration date string
    """
    expiry_date = datetime.datetime.now() + datetime.timedelta(days=days)
    return format_datetime(expiry_date)

def is_expired(expire_date_str: str) -> bool:
    """
    Check if the access has expired.
    
    Args:
        expire_date_str: Expiration date string
        
    Returns:
        True if expired
    """
    try:
        expire_date = parse_datetime(expire_date_str)
        return datetime.datetime.now() > expire_date
    except ValueError:
        logger.error(f"Invalid date format: {expire_date_str}")
        return False

def format_peer_info(peer_data: dict) -> str:
    """
    Format peer info for user display.
    
    Args:
        peer_data: Peer data
        
    Returns:
        Formatted string
    """
    if not peer_data:
        return "Пир не найден"
    
    created_at = peer_data.get('created_at', 'Неизвестно')
    expire_date = peer_data.get('expire_date', 'Неизвестно')
    
    # Parse dates for display
    try:
        if created_at != 'Неизвестно':
            created_dt = parse_datetime(created_at)
            created_at = created_dt.strftime("%d.%m.%Y %H:%M")
    except:
        pass
    
    try:
        if expire_date != 'Неизвестно':
            expire_dt = parse_datetime(expire_date)
            expire_date = expire_dt.strftime("%d.%m.%Y %H:%M")
    except:
        pass
    
    status = "🟢 Активен" if not is_expired(peer_data.get('expire_date', '')) else "🔴 Истек"
    
    return f"""
📋 **Информация о пире**

👤 **Имя:** `{peer_data.get('peer_name', 'Неизвестно')}`
🆔 **ID:** `{peer_data.get('peer_id', 'Неизвестно')[:20]}...`
📅 **Создан:** {created_at}
⏰ **Истекает:** {expire_date}
📊 **Статус:** {status}
    """

def format_peer_list(peers: list) -> str:
    """
    Format peer list for display.
    
    Args:
        peers: List of peers
        
    Returns:
        Formatted string
    """
    if not peers:
        return "📭 У вас пока нет активных пиров"
    
    result = "📋 **Ваши пиры:**\n\n"
    
    for i, peer in enumerate(peers, 1):
        status = "🟢" if not is_expired(peer.get('expire_date', '')) else "🔴"
        expire_date = peer.get('expire_date', 'Неизвестно')
        
        try:
            if expire_date != 'Неизвестно':
                expire_dt = parse_datetime(expire_date)
                expire_date = expire_dt.strftime("%d.%m.%Y")
        except:
            pass
        
        result += f"{i}. {status} `{peer.get('peer_name', 'Неизвестно')}` - до {expire_date}\n"
    
    return result

def validate_peer_name(name: str) -> bool:
    """
    Validate a peer name.
    
    Args:
        name: Name to validate
        
    Returns:
        True if valid
    """
    if not name or len(name) < 3:
        return False
    
    # Check allowed characters
    allowed_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')
    return all(c in allowed_chars for c in name)

def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename by removing invalid characters.
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename
    """
    import re
    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Trim leading and trailing spaces
    filename = filename.strip()
    # Limit length
    return filename


import json
import os
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

class ClientsJsonManager:
    def __init__(self, file_path: str):
        self.file_path = file_path

    def _read_clients(self) -> List[Dict[str, str]]:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error reading clients.json: {e}")
            return []

    def _write_clients(self, clients: List[Dict[str, str]]) -> bool:
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(clients, f, indent=2, ensure_ascii=False)
            return True
        except IOError as e:
            logger.error(f"Error writing clients.json: {e}")
            return False

    def add_update_client(self, client_id: str, public_key: str, force_write: bool = False) -> bool:
        """
        Adds or updates a client in the JSON file.
        client_id: The Telegram username (or ID) used as identifier.
        public_key: The WireGuard public key.
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
        Removes a client from the JSON file by client_id.
        """
        clients = self._read_clients()
        original_length = len(clients)
        
        # Filter out matching client_id
        new_clients = [c for c in clients if c.get("clientId") != client_id]
        
        if len(new_clients) < original_length:
            return self._write_clients(new_clients)
        
        return True

class PromoManager:
    def __init__(self, promo_file_path: str):
        self.promo_file_path = promo_file_path

    def get_user_promo_factor(self, user_id: int) -> float:
        """
        Return user-specific price multiplier.
        If value <= 100, it's a discount percent (e.g., 20 -> 0.8).
        If value > 100, it's a markup percent (e.g., 150 -> 1.5).
        Reads promo.txt on each call to support hot reload.
        """
        if not os.path.exists(self.promo_file_path):
            return 1.0
        
        try:
            with open(self.promo_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or '=' not in line:
                        continue
                    
                    try:
                        uid_str, val_str = line.split('=')
                        uid = int(uid_str.strip())
                        val = int(val_str.strip())
                        
                        if uid == user_id:
                            if val <= 100:
                                # Discount (20 -> 0.8)
                                return 1.0 - (val / 100.0)
                            else:
                                # Markup (150 -> 1.5)
                                return val / 100.0
                    except ValueError:
                        continue
        except Exception as e:
            # logger.error(f"Failed to read promo file: {e}")
            # Fail open: ignore promo errors and return default multiplier
            pass
            
        return 1.0
