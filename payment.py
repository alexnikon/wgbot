import logging
from typing import Optional, Dict, Any
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from aiogram.exceptions import TelegramAPIError
from config import TARIFFS, YOOKASSA_PROVIDER_TOKEN

logger = logging.getLogger(__name__)

class PaymentManager:
    """Менеджер для работы с оплатой через Telegram Stars и ЮKassa"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.tariffs = TARIFFS  # Конфигурация тарифов
        self.yookassa_provider_token = YOOKASSA_PROVIDER_TOKEN
    
    async def create_payment_selection_keyboard(self, user_id: int) -> InlineKeyboardMarkup:
        """
        Создает клавиатуру для выбора тарифов
        
        Args:
            user_id: ID пользователя Telegram
            
        Returns:
            InlineKeyboardMarkup с вариантами тарифов
        """
        # Проверяем доступность ЮKassa
        yookassa_available = bool(self.yookassa_provider_token)
        
        buttons = []
        
        # Создаем кнопки для каждого тарифа
        for tariff_key, tariff_data in self.tariffs.items():
            # Кнопка для оплаты через Stars
            buttons.append([InlineKeyboardButton(
                text=f"{tariff_data['name']} - {tariff_data['stars_price']} ⭐",
                callback_data=f"pay_stars_{tariff_key}_{user_id}"
            )])
            
            # Кнопка для оплаты через ЮKassa
            if yookassa_available:
                buttons.append([InlineKeyboardButton(
                    text=f"{tariff_data['name']} - {tariff_data['rub_price']} ₽",
                    callback_data=f"pay_yookassa_{tariff_key}_{user_id}"
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"{tariff_data['name']} - {tariff_data['rub_price']} ₽ (недоступно)",
                    callback_data=f"pay_yookassa_disabled_{user_id}"
                )])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        return keyboard
    
    async def create_stars_invoice(self, user_id: int, tariff_key: str, username: str = None) -> Optional[Dict[str, Any]]:
        """
        Создает инвойс для оплаты через Telegram Stars
        
        Args:
            user_id: ID пользователя Telegram
            tariff_key: Ключ тарифа (7_days или 30_days)
            username: Username пользователя (опционально)
            
        Returns:
            Словарь с информацией об инвойсе или None при ошибке
        """
        try:
            if tariff_key not in self.tariffs:
                logger.error(f"Неизвестный тариф: {tariff_key}")
                return None
                
            tariff_data = self.tariffs[tariff_key]
            
            # Создаем кнопку для оплаты
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"⭐ Оплатить {tariff_data['stars_price']} звезд",
                    pay=True
                )]
            ])
            
            # Создаем инвойс
            invoice_data = {
                'title': f'VPN доступ на {tariff_data["name"]} (Stars)',
                'description': f'{tariff_data["description"]}\n\n'
                              f'Пользователь: @{username}' if username else tariff_data['description'],
                'payload': f'vpn_access_stars_{tariff_key}_{user_id}',
                'provider_token': '',  # Для Telegram Stars не нужен
                'currency': 'XTR',  # Код валюты для Telegram Stars
                'prices': [LabeledPrice(label=f'VPN доступ {tariff_data["name"]}', amount=tariff_data['stars_price'])],
                'reply_markup': keyboard
            }
            
            return invoice_data
            
        except Exception as e:
            logger.error(f"Ошибка при создании инвойса Stars для пользователя {user_id}, тариф {tariff_key}: {e}")
            return None
    
    async def create_yookassa_invoice(self, user_id: int, tariff_key: str, username: str = None) -> Optional[Dict[str, Any]]:
        """
        Создает инвойс для оплаты через ЮKassa
        
        Args:
            user_id: ID пользователя Telegram
            tariff_key: Ключ тарифа (7_days или 30_days)
            username: Username пользователя (опционально)
            
        Returns:
            Словарь с информацией об инвойсе или None при ошибке
        """
        try:
            if not self.yookassa_provider_token:
                logger.error("Не настроен YooKassa provider token")
                return None
                
            if tariff_key not in self.tariffs:
                logger.error(f"Неизвестный тариф: {tariff_key}")
                return None
                
            tariff_data = self.tariffs[tariff_key]
            
            # Создаем кнопку для оплаты
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"💳 Оплатить {tariff_data['rub_price']} руб.",
                    pay=True
                )]
            ])
            
            # Создаем инвойс
            invoice_data = {
                'title': f'VPN доступ на {tariff_data["name"]} (ЮKassa)',
                'description': f'{tariff_data["description"]}\n\n'
                              f'Пользователь: @{username}' if username else tariff_data['description'],
                'payload': f'vpn_access_yookassa_{tariff_key}_{user_id}',
                'provider_token': self.yookassa_provider_token,
                'currency': 'RUB',  # Код валюты для рублей
                'prices': [LabeledPrice(label=f'VPN доступ {tariff_data["name"]}', amount=tariff_data['rub_price'] * 100)],  # В копейках
                'reply_markup': keyboard
            }
            
            return invoice_data
            
        except Exception as e:
            logger.error(f"Ошибка при создании инвойса ЮKassa для пользователя {user_id}, тариф {tariff_key}: {e}")
            return None
    
    async def send_payment_selection(self, chat_id: int, user_id: int) -> bool:
        """
        Отправляет сообщение с выбором способа оплаты
        
        Args:
            chat_id: ID чата
            user_id: ID пользователя Telegram
            
        Returns:
            True если сообщение отправлено успешно
        """
        try:
            keyboard = await self.create_payment_selection_keyboard(user_id)
            
            # Проверяем доступность ЮKassa
            yookassa_available = bool(self.yookassa_provider_token)
            
            # Формируем текст с доступными тарифами
            tariff_text = ""
            for tariff_key, tariff_data in self.tariffs.items():
                tariff_text += f"⭐ {tariff_data['name']} - {tariff_data['stars_price']} ⭐\n"
                tariff_text += f"💳 {tariff_data['name']} - {tariff_data['rub_price']} руб.\n\n"
            
            payment_text = f"""
⏰ Выбери тариф VPN доступа:

{tariff_text}Выбери удобный для тебя тариф:
            """
            
            await self.bot.send_message(
                chat_id=chat_id,
                text=payment_text,
                reply_markup=keyboard
            )
            
            logger.info(f"Выбор способа оплаты отправлен пользователю {user_id}")
            return True
            
        except TelegramAPIError as e:
            logger.error(f"Ошибка Telegram API при отправке выбора оплаты: {e}")
            return False
        except Exception as e:
            logger.error(f"Ошибка при отправке выбора оплаты пользователю {user_id}: {e}")
            return False
    
    async def send_stars_payment_request(self, chat_id: int, user_id: int, tariff_key: str, username: str = None) -> bool:
        """
        Отправляет запрос на оплату через Telegram Stars
        
        Args:
            chat_id: ID чата
            user_id: ID пользователя Telegram
            tariff_key: Ключ тарифа (7_days или 30_days)
            username: Username пользователя (опционально)
            
        Returns:
            True если запрос отправлен успешно
        """
        try:
            invoice_data = await self.create_stars_invoice(user_id, tariff_key, username)
            if not invoice_data:
                return False
            
            await self.bot.send_invoice(
                chat_id=chat_id,
                **invoice_data
            )
            
            logger.info(f"Запрос на оплату Stars отправлен пользователю {user_id}, тариф {tariff_key}")
            return True
            
        except TelegramAPIError as e:
            logger.error(f"Ошибка Telegram API при отправке запроса на оплату Stars: {e}")
            return False
        except Exception as e:
            logger.error(f"Ошибка при отправке запроса на оплату Stars пользователю {user_id}, тариф {tariff_key}: {e}")
            return False
    
    async def send_yookassa_payment_request(self, chat_id: int, user_id: int, tariff_key: str, username: str = None) -> bool:
        """
        Отправляет запрос на оплату через ЮKassa
        
        Args:
            chat_id: ID чата
            user_id: ID пользователя Telegram
            tariff_key: Ключ тарифа (7_days или 30_days)
            username: Username пользователя (опционально)
            
        Returns:
            True если запрос отправлен успешно
        """
        try:
            invoice_data = await self.create_yookassa_invoice(user_id, tariff_key, username)
            if not invoice_data:
                return False
            
            await self.bot.send_invoice(
                chat_id=chat_id,
                **invoice_data
            )
            
            logger.info(f"Запрос на оплату ЮKassa отправлен пользователю {user_id}, тариф {tariff_key}")
            return True
            
        except TelegramAPIError as e:
            logger.error(f"Ошибка Telegram API при отправке запроса на оплату ЮKassa: {e}")
            return False
        except Exception as e:
            logger.error(f"Ошибка при отправке запроса на оплату ЮKassa пользователю {user_id}, тариф {tariff_key}: {e}")
            return False
    
    async def process_payment(self, pre_checkout_query) -> bool:
        """
        Обрабатывает предварительную проверку платежа
        
        Args:
            pre_checkout_query: Объект предварительной проверки платежа
            
        Returns:
            True если платеж валиден
        """
        try:
            payload = pre_checkout_query.invoice_payload
            
            # Проверяем, что это наш платеж
            if not (payload.startswith('vpn_access_stars_') or payload.startswith('vpn_access_yookassa_')):
                await pre_checkout_query.answer(ok=False, error_message="Неверный тип платежа")
                return False
            
            # Проверяем сумму в зависимости от типа платежа
            if payload.startswith('vpn_access_stars_'):
                # Платеж через Stars - извлекаем тариф из payload
                payload_parts = payload.split('_')
                if len(payload_parts) >= 4:
                    tariff_key = f"{payload_parts[3]}_{payload_parts[4]}"
                    tariff_data = self.tariffs.get(tariff_key, {})
                    expected_amount = tariff_data.get('stars_price', 1)
                    if pre_checkout_query.total_amount != expected_amount:
                        await pre_checkout_query.answer(ok=False, error_message="Неверная сумма платежа")
                        return False
            elif payload.startswith('vpn_access_yookassa_'):
                # Платеж через ЮKassa - извлекаем тариф из payload
                payload_parts = payload.split('_')
                if len(payload_parts) >= 4:
                    tariff_key = f"{payload_parts[3]}_{payload_parts[4]}"
                    tariff_data = self.tariffs.get(tariff_key, {})
                    expected_amount = tariff_data.get('rub_price', 0) * 100  # В копейках
                    if pre_checkout_query.total_amount != expected_amount:
                        await pre_checkout_query.answer(ok=False, error_message="Неверная сумма платежа")
                        return False
            
            # Подтверждаем платеж
            await pre_checkout_query.answer(ok=True)
            logger.info(f"Платеж подтвержден для пользователя {pre_checkout_query.from_user.id}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при обработке платежа: {e}")
            await pre_checkout_query.answer(ok=False, error_message="Ошибка обработки платежа")
            return False
    
    async def confirm_payment(self, successful_payment) -> tuple[bool, str, int]:
        """
        Подтверждает успешный платеж
        
        Args:
            successful_payment: Объект успешного платежа
            
        Returns:
            Tuple (success, payment_type, amount_paid)
        """
        try:
            # Извлекаем user_id и тип платежа из payload
            payload = successful_payment.invoice_payload
            
            if payload.startswith('vpn_access_stars_'):
                # Извлекаем user_id из payload (формат: vpn_access_stars_7_days_123456789)
                payload_parts = payload.split('_')
                user_id = int(payload_parts[-1])  # Последняя часть - user_id
                payment_type = 'stars'
                amount_paid = successful_payment.total_amount
                logger.info(f"Платеж Stars подтвержден: пользователь {user_id}, звезд: {amount_paid}")
                
            elif payload.startswith('vpn_access_yookassa_'):
                # Извлекаем user_id из payload (формат: vpn_access_yookassa_7_days_123456789)
                payload_parts = payload.split('_')
                user_id = int(payload_parts[-1])  # Последняя часть - user_id
                payment_type = 'yookassa'
                amount_paid = successful_payment.total_amount
                logger.info(f"Платеж ЮKassa подтвержден: пользователь {user_id}, копеек: {amount_paid}")
                
            else:
                logger.error(f"Неверный payload платежа: {payload}")
                return False, '', 0
            
            # Здесь можно добавить дополнительную логику обработки платежа
            # Например, уведомление администратора, логирование в файл и т.д.
            
            return True, payment_type, amount_paid
            
        except Exception as e:
            logger.error(f"Ошибка при подтверждении платежа: {e}")
            return False, '', 0
    
    def get_payment_info(self) -> Dict[str, Any]:
        """
        Возвращает информацию о доступных тарифах
        
        Returns:
            Словарь с информацией о тарифах
        """
        # Получаем первый доступный тариф для отображения периода
        first_tariff = next(iter(self.tariffs.values())) if self.tariffs else None
        
        return {
            'tariffs': self.tariffs,
            'yookassa_available': bool(self.yookassa_provider_token),
            'period': first_tariff['name'] if first_tariff else '30 дней',
            'stars_price': first_tariff['stars_price'] if first_tariff else 200,
            'rub_price': first_tariff['rub_price'] if first_tariff else 300
        }
