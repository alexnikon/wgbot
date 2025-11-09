import logging
import hashlib
import hmac
import json
import uuid
from typing import Dict, Any, Optional
from datetime import datetime
import httpx
from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY

logger = logging.getLogger(__name__)

class YooKassaClient:
    """Клиент для работы с API ЮKassa"""
    
    def __init__(self):
        self.shop_id = YOOKASSA_SHOP_ID
        self.secret_key = YOOKASSA_SECRET_KEY
        self.base_url = "https://api.yookassa.ru/v3"
        self.headers = {
            "Authorization": f"Basic {self._get_auth_token()}",
            "Content-Type": "application/json",
            "Idempotence-Key": ""
        }
    
    def _get_auth_token(self) -> str:
        """Создает токен авторизации для API"""
        import base64
        auth_string = f"{self.shop_id}:{self.secret_key}"
        return base64.b64encode(auth_string.encode()).decode()
    
    def _generate_idempotence_key(self) -> str:
        """Генерирует уникальный ключ идемпотентности"""
        return str(uuid.uuid4())
    
    async def create_payment(self, amount: int, currency: str, description: str, 
                           return_url: str, metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Создает платеж в ЮKassa
        
        Args:
            amount: Сумма в копейках
            currency: Валюта (RUB)
            description: Описание платежа
            return_url: URL для возврата после оплаты
            metadata: Дополнительные данные
            
        Returns:
            Данные платежа или None при ошибке
        """
        try:
            payment_data = {
                "amount": {
                    "value": f"{amount / 100:.2f}",
                    "currency": currency
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": return_url
                },
                "capture": True,
                "description": description,
                "metadata": metadata
            }
            
            headers = self.headers.copy()
            headers["Idempotence-Key"] = self._generate_idempotence_key()
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments",
                    headers=headers,
                    json=payment_data,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"Платеж создан: {result.get('id')}")
                    return result
                else:
                    logger.error(f"Ошибка создания платежа: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            logger.error(f"Ошибка при создании платежа: {e}")
            return None
    
    async def get_payment(self, payment_id: str) -> Optional[Dict[str, Any]]:
        """
        Получает информацию о платеже
        
        Args:
            payment_id: ID платежа
            
        Returns:
            Данные платежа или None при ошибке
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/payments/{payment_id}",
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Ошибка получения платежа: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            logger.error(f"Ошибка при получении платежа {payment_id}: {e}")
            return None
    
    def verify_webhook_signature(self, body: str, signature: str) -> bool:
        """
        Проверяет подпись webhook от ЮKassa
        
        Примечание: В документации ЮKassa не указан точный алгоритм проверки подписи.
        Этот метод реализует стандартную проверку HMAC-SHA256.
        
        Args:
            body: Тело запроса
            signature: Подпись из заголовка
            
        Returns:
            True если подпись валидна
        """
        try:
            if not signature:
                logger.warning("Отсутствует подпись в webhook")
                return False
                
            # Создаем подпись для проверки (HMAC-SHA256)
            expected_signature = hmac.new(
                self.secret_key.encode(),
                body.encode(),
                hashlib.sha256
            ).hexdigest()
            
            # Сравниваем подписи безопасным способом
            is_valid = hmac.compare_digest(signature, expected_signature)
            
            if not is_valid:
                logger.warning(f"Неверная подпись webhook. Ожидалось: {expected_signature[:8]}..., получено: {signature[:8]}...")
            
            return is_valid
            
        except Exception as e:
            logger.error(f"Ошибка при проверке подписи webhook: {e}")
            return False
    
    def parse_webhook(self, body: str) -> Optional[Dict[str, Any]]:
        """
        Парсит webhook от ЮKassa
        
        Args:
            body: Тело запроса
            
        Returns:
            Данные webhook или None при ошибке
        """
        try:
            data = json.loads(body)
            
            # Валидируем структуру webhook согласно документации
            if not self.validate_webhook_structure(data):
                return None
                
            return data
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга webhook: {e}")
            return None
    
    def validate_webhook_structure(self, data: Dict[str, Any]) -> bool:
        """
        Валидирует структуру webhook согласно документации ЮKassa
        
        Args:
            data: Распарсенные данные webhook
            
        Returns:
            True если структура валидна или может быть обработана
        """
        try:
            logger.debug(f"Валидация webhook структуры: ключи={list(data.keys())}")
            
            # Проверяем наличие type (может быть 'notification' или отсутствовать)
            notification_type = data.get('type', '')
            
            # Если type есть, проверяем что это 'notification'
            if notification_type and notification_type != 'notification':
                logger.warning(f"Неожиданный тип уведомления: {notification_type}, но продолжаем обработку")
            
            # Проверяем наличие event или event_type
            event = data.get('event') or data.get('event_type', '')
            if not event:
                # Если нет event, возможно это прямой объект платежа
                if 'id' in data and 'status' in data:
                    logger.info("Webhook содержит прямой объект платежа без обертки notification")
                    return True
                logger.error(f"Отсутствует поле 'event' в webhook. Доступные ключи: {list(data.keys())}")
                return False
            
            # Проверяем наличие object или payment
            event_object = data.get('object') or data.get('payment', data)
            if not event_object or not isinstance(event_object, dict):
                # Если object отсутствует, но есть прямые данные платежа в корне
                if 'id' in data and 'status' in data:
                    logger.info("Webhook содержит данные платежа в корне объекта")
                    return True
                logger.error(f"Отсутствует или неверный параметр 'object' в webhook. Тип: {type(event_object)}")
                return False
            
            logger.debug(f"Webhook структура валидна: type={notification_type}, event={event}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка валидации структуры webhook: {e}", exc_info=True)
            return False
    
    def is_payment_succeeded(self, payment_data: Dict[str, Any]) -> bool:
        """
        Проверяет, успешен ли платеж
        
        Args:
            payment_data: Данные платежа
            
        Returns:
            True если платеж успешен
        """
        return payment_data.get("status") == "succeeded"
    
    def is_payment_canceled(self, payment_data: Dict[str, Any]) -> bool:
        """
        Проверяет, отменен ли платеж
        
        Args:
            payment_data: Данные платежа
            
        Returns:
            True если платеж отменен
        """
        return payment_data.get("status") == "canceled"
    
    def is_payment_waiting_for_capture(self, payment_data: Dict[str, Any]) -> bool:
        """
        Проверяет, ожидает ли платеж подтверждения
        
        Args:
            payment_data: Данные платежа
            
        Returns:
            True если платеж ожидает подтверждения
        """
        return payment_data.get("status") == "waiting_for_capture"
    
    def is_refund_succeeded(self, refund_data: Dict[str, Any]) -> bool:
        """
        Проверяет, успешен ли возврат
        
        Args:
            refund_data: Данные возврата
            
        Returns:
            True если возврат успешен
        """
        return refund_data.get("status") == "succeeded"
    
    def get_payment_amount(self, payment_data: Dict[str, Any]) -> int:
        """
        Получает сумму платежа в копейках
        
        Args:
            payment_data: Данные платежа
            
        Returns:
            Сумма в копейках
        """
        try:
            amount_value = payment_data.get("amount", {}).get("value", "0")
            return int(float(amount_value) * 100)
        except (ValueError, TypeError):
            return 0
    
    def get_payment_metadata(self, payment_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Получает метаданные платежа
        
        Args:
            payment_data: Данные платежа
            
        Returns:
            Метаданные платежа
        """
        return payment_data.get("metadata", {})
