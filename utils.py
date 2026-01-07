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
    if len(filename) > 100:
        filename = filename[:100]
    
    return filename
