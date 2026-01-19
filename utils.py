import uuid
import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)

def generate_peer_name(telegram_username: Optional[str] = None, user_id: Optional[int] = None) -> str:
    """
    Генерирует уникальное имя для пира в формате username_telegramID
    
    Args:
        telegram_username: Username пользователя Telegram
        user_id: ID пользователя Telegram
        
    Returns:
        Сгенерированное имя пира в формате username_telegramID
    """
    # Используем формат username_telegramID для лучшей идентификации
    if telegram_username:
        peer_name = f"{telegram_username}_{user_id}"
    else:
        # Если username отсутствует, используем только user_id
        peer_name = f"user_{user_id}"
    
    # Ограничиваем длину имени
    if len(peer_name) > 50:
        peer_name = peer_name[:50]
    
    return peer_name

def generate_uuid() -> str:
    """Генерирует UUID для job"""
    return str(uuid.uuid4())

def format_datetime(dt: datetime.datetime) -> str:
    """
    Форматирует datetime в строку для WGDashboard API
    
    Args:
        dt: Объект datetime
        
    Returns:
        Отформатированная строка даты
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def parse_datetime(date_str: str) -> datetime.datetime:
    """
    Парсит строку даты из WGDashboard API
    
    Args:
        date_str: Строка даты
        
    Returns:
        Объект datetime
    """
    return datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")

def parse_date_flexible(date_str: str) -> datetime.datetime:
    """
    Парсит строку даты, поддерживая несколько форматов
    
    Args:
        date_str: Строка даты в формате "YYYY-MM-DD HH:MM:SS" или "YYYY-MM-DD"
        
    Returns:
        Объект datetime
        
    Raises:
        ValueError: Если формат даты не распознан
    """
    if not date_str:
        raise ValueError("Пустая строка даты")
    
    # Пробуем формат с временем
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    
    # Пробуем формат без времени
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Неверный формат даты: {date_str}")

def format_date_for_user(date_str: str) -> str:
    """
    Форматирует дату для отображения пользователю в формате ДД-ММ-ГГГГ
    
    Args:
        date_str: Строка даты в формате "YYYY-MM-DD HH:MM:SS" или "YYYY-MM-DD"
        
    Returns:
        Отформатированная строка в формате "ДД-ММ-ГГГГ"
    """
    try:
        dt = parse_date_flexible(date_str)
        return dt.strftime("%d-%m-%Y")
    except (ValueError, TypeError) as e:
        logger.error(f"Ошибка форматирования даты {date_str}: {e}")
        return date_str  # Возвращаем исходную строку в случае ошибки

def calculate_expiry_date(days: int = 30) -> str:
    """
    Вычисляет дату истечения через указанное количество дней
    
    Args:
        days: Количество дней до истечения
        
    Returns:
        Строка с датой истечения
    """
    expiry_date = datetime.datetime.now() + datetime.timedelta(days=days)
    return format_datetime(expiry_date)

def is_expired(expire_date_str: str) -> bool:
    """
    Проверяет, истек ли срок действия
    
    Args:
        expire_date_str: Строка с датой истечения
        
    Returns:
        True если срок истек
    """
    try:
        expire_date = parse_datetime(expire_date_str)
        return datetime.datetime.now() > expire_date
    except ValueError:
        logger.error(f"Неверный формат даты: {expire_date_str}")
        return False

def format_peer_info(peer_data: dict) -> str:
    """
    Форматирует информацию о пире для отображения пользователю
    
    Args:
        peer_data: Данные о пире
        
    Returns:
        Отформатированная строка
    """
    if not peer_data:
        return "Пир не найден"
    
    created_at = peer_data.get('created_at', 'Неизвестно')
    expire_date = peer_data.get('expire_date', 'Неизвестно')
    
    # Парсим даты для красивого отображения
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
    Форматирует список пиров для отображения
    
    Args:
        peers: Список пиров
        
    Returns:
        Отформатированная строка
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
    Проверяет валидность имени пира
    
    Args:
        name: Имя для проверки
        
    Returns:
        True если имя валидно
    """
    if not name or len(name) < 3:
        return False
    
    # Проверяем на допустимые символы
    allowed_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')
    return all(c in allowed_chars for c in name)

def sanitize_filename(filename: str) -> str:
    """
    Очищает имя файла от недопустимых символов
    
    Args:
        filename: Исходное имя файла
        
    Returns:
        Очищенное имя файла
    """
    import re
    # Удаляем недопустимые символы
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Удаляем пробелы в начале и конце
    filename = filename.strip()
    # Ограничиваем длину
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

    def add_update_client(self, client_id: str, public_key: str) -> bool:
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
                if client.get("publicKey") != public_key:
                    client["publicKey"] = public_key
                    updated = True
                break
        
        if not found:
            clients.append({"clientId": client_id, "publicKey": public_key})
            updated = True
            
        if updated:
            return self._write_clients(clients)
        return True

    def remove_client(self, client_id: str) -> bool:
        """
        Removes a client from the JSON file by client_id.
        """
        clients = self._read_clients()
        original_length = len(clients)
        
        # Фильтруем список, оставляя только тех, у кого не совпадает client_id
        new_clients = [c for c in clients if c.get("clientId") != client_id]
        
        if len(new_clients) < original_length:
            return self._write_clients(new_clients)
        
        return True

class PromoManager:
    def __init__(self, promo_file_path: str):
        self.promo_file_path = promo_file_path

    def get_user_discount(self, user_id: int) -> int:
        """
        Возвращает размер скидки для пользователя в процентах (0-100).
        Считывает файл promo.txt при каждом вызове для поддержки горячего обновления.
        """
        if not os.path.exists(self.promo_file_path):
            return 0
        
        try:
            with open(self.promo_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or '=' not in line:
                        continue
                    
                    try:
                        uid_str, discount_str = line.split('=')
                        uid = int(uid_str.strip())
                        discount = int(discount_str.strip())
                        
                        if uid == user_id:
                            # Ограничиваем скидку от 0 до 100
                            return max(0, min(100, discount))
                    except ValueError:
                        continue
        except Exception as e:
            logger.error(f"Ошибка при чтении файла промокодов: {e}")
            
        return 0
