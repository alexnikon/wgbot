import logging
from typing import Optional, Dict, Any
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from aiogram.exceptions import TelegramAPIError
from config import get_tariffs, WEBHOOK_URL, DOMAIN, PROMO_FILE_PATH
from yookassa_client import YooKassaClient
from database import Database
from utils import PromoManager

logger = logging.getLogger(__name__)

class PaymentManager:
    """Payment manager for Telegram Stars and YooKassa."""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.yookassa_client = YooKassaClient()
        self.db = Database()
        self.webhook_url = WEBHOOK_URL
        self.domain = DOMAIN
        self.promo_manager = PromoManager(PROMO_FILE_PATH)
    
    @property
    def tariffs(self):
        """Get current tariffs from config (hot-reloaded)."""
        return get_tariffs()
        
    def get_user_tariffs(self, user_id: int) -> Dict[str, Any]:
        """
        Return tariffs with user-specific discount/markup applied.
        """
        base_tariffs = self.tariffs.copy()
        factor = self.promo_manager.get_user_promo_factor(user_id)
        
        if factor == 1.0:
            return base_tariffs
            
        # Apply multiplier
        discounted_tariffs = {}
        for key, data in base_tariffs.items():
            # Copy dict to avoid mutating global tariffs
            new_data = data.copy()
            
            # Compute new price
            new_stars = int(data['stars_price'] * factor)
            new_rub = int(data['rub_price'] * factor)
            
            # Ensure minimum price is 1
            new_data['stars_price'] = max(1, new_stars)
            new_data['rub_price'] = max(1, new_rub)
            
            discounted_tariffs[key] = new_data
            
        return discounted_tariffs
    
    async def create_payment_selection_keyboard(self, user_id: int) -> InlineKeyboardMarkup:
        """
        Create a keyboard with tariff options.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            InlineKeyboardMarkup with tariff options
        """
        # Check YooKassa availability
        yookassa_available = bool(self.yookassa_client.shop_id and self.yookassa_client.secret_key)
        
        buttons = []
        
        # Get user-specific tariffs
        user_tariffs = self.get_user_tariffs(user_id)
        
        # Build buttons for each tariff
        for tariff_key, tariff_data in user_tariffs.items():
            # Button for Stars payment
            buttons.append([InlineKeyboardButton(
                text=f"{tariff_data['name']} - {tariff_data['stars_price']} ⭐",
                callback_data=f"pay_stars_{tariff_key}_{user_id}"
            )])
            
            # Button for YooKassa payment
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
        Create an invoice for Telegram Stars payment.
        
        Args:
            user_id: Telegram user ID
            tariff_key: Tariff key (7_days or 30_days)
            username: Telegram username (optional)
            
        Returns:
            Invoice data dict or None on error
        """
        try:
            # Get user tariffs
            user_tariffs = self.get_user_tariffs(user_id)
            if tariff_key not in user_tariffs:
                logger.error(f"Unknown tariff: {tariff_key}")
                return None
                
            tariff_data = user_tariffs[tariff_key]
            
            # Build payment button
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"⭐ Оплатить {tariff_data['stars_price']} звезд",
                    pay=True
                )]
            ])
            
            # Build invoice
            invoice_data = {
                'title': f'VPN доступ на {tariff_data["name"]} (Stars)',
                'description': f'{tariff_data["description"]}\n\n'
                              f'Пользователь: @{username}' if username else tariff_data['description'],
                'payload': f'vpn_access_stars_{tariff_key}_{user_id}',
                'provider_token': '',  # Not required for Telegram Stars
                'currency': 'XTR',  # Currency code for Telegram Stars
                'prices': [LabeledPrice(label=f'VPN доступ {tariff_data["name"]}', amount=tariff_data['stars_price'])],
                'reply_markup': keyboard
            }
            
            return invoice_data
            
        except Exception as e:
            logger.error(f"Failed to create Stars invoice for user {user_id}, tariff {tariff_key}: {e}")
            return None
    
    async def create_yookassa_payment(self, user_id: int, tariff_key: str, username: str = None) -> Optional[str]:
        """
        Create a YooKassa payment and return the checkout URL.
        
        Args:
            user_id: Telegram user ID
            tariff_key: Tariff key (14_days or 30_days)
            username: Telegram username (optional)
            
        Returns:
            Payment URL or None on error
        """
        try:
            if not self.yookassa_client.shop_id or not self.yookassa_client.secret_key:
                logger.error("YooKassa is not configured")
                return None
                
            # Get user tariffs
            user_tariffs = self.get_user_tariffs(user_id)
            if tariff_key not in user_tariffs:
                logger.error(f"Unknown tariff: {tariff_key}")
                return None
                
            tariff_data = user_tariffs[tariff_key]
            amount = tariff_data['rub_price'] * 100  # In kopeks
            
            # Return URL
            return_url = "https://t.me/nikonvpn_bot"

            effective_username = (username or "").strip()
            if effective_username.startswith("@"):
                effective_username = effective_username[1:]
            if not effective_username:
                existing_peer = self.db.get_peer_by_telegram_id(user_id)
                if existing_peer:
                    effective_username = (existing_peer.get("telegram_username") or "").strip()
            if effective_username.startswith("@"):
                effective_username = effective_username[1:]
            
            # Payment metadata
            metadata = {
                'user_id': str(user_id),
                'tariff_key': tariff_key,
                'username': effective_username,
                'description': f'VPN доступ на {tariff_data["name"]}'
            }
            
            logger.info(f"Creating YooKassa payment for user {user_id}, tariff {tariff_key}, amount {amount} kopeks, metadata: {metadata}")
            
            # Create YooKassa payment
            payment_data = await self.yookassa_client.create_payment(
                amount=amount,
                currency='RUB',
                description=f'VPN доступ на {tariff_data["name"]}',
                return_url=return_url,
                metadata=metadata
            )
            
            if payment_data:
                logger.info(f"Payment created: ID={payment_data.get('id')}, status={payment_data.get('status')}, metadata in response={payment_data.get('metadata', {})}")
            
            if not payment_data:
                logger.error("Failed to create YooKassa payment")
                return None
            
            payment_id = payment_data.get('id')
            if not payment_id:
                logger.error("No payment ID returned by YooKassa")
                return None
            
            # Persist payment in database
            self.db.add_payment(
                payment_id=payment_id,
                user_id=user_id,
                amount=amount,
                payment_method='yookassa',
                tariff_key=tariff_key,
                metadata=metadata
            )
            
            # Extract payment URL
            confirmation = payment_data.get('confirmation', {})
            payment_url = confirmation.get('confirmation_url')
            
            if not payment_url:
                logger.error("No payment URL returned by YooKassa")
                return None
            
            logger.info(f"Created YooKassa payment {payment_id} for user {user_id}")
            return payment_url
            
        except Exception as e:
            logger.error(f"Failed to create YooKassa payment for user {user_id}, tariff {tariff_key}: {e}")
            return None
    
    async def send_payment_selection(self, chat_id: int, user_id: int) -> bool:
        """
        Send a message with payment method selection.
        
        Args:
            chat_id: Chat ID
            user_id: Telegram user ID
            
        Returns:
            True if sent successfully
        """
        try:
            keyboard = await self.create_payment_selection_keyboard(user_id)
            
            # Check YooKassa availability
            yookassa_available = bool(self.yookassa_client.shop_id and self.yookassa_client.secret_key)
            
            payment_text = """
⏰ Выбери тариф VPN доступа:

Выбери удобный для тебя тариф:
            """
            
            await self.bot.send_message(
                chat_id=chat_id,
                text=payment_text,
                reply_markup=keyboard
            )
            
            logger.info(f"Payment selection sent to user {user_id}")
            return True
            
        except TelegramAPIError as e:
            logger.error(f"Telegram API error while sending payment selection: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send payment selection to user {user_id}: {e}")
            return False
    
    async def send_stars_payment_request(self, chat_id: int, user_id: int, tariff_key: str, username: str = None) -> bool:
        """
        Send a Telegram Stars payment request.
        
        Args:
            chat_id: Chat ID
            user_id: Telegram user ID
            tariff_key: Tariff key (7_days or 30_days)
            username: Telegram username (optional)
            
        Returns:
            True if request sent successfully
        """
        try:
            invoice_data = await self.create_stars_invoice(user_id, tariff_key, username)
            if not invoice_data:
                return False
            
            await self.bot.send_invoice(
                chat_id=chat_id,
                **invoice_data
            )
            
            logger.info(f"Stars payment request sent to user {user_id}, tariff {tariff_key}")
            return True
            
        except TelegramAPIError as e:
            logger.error(f"Telegram API error while sending Stars payment request: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send Stars payment request to user {user_id}, tariff {tariff_key}: {e}")
            return False
    
    async def send_yookassa_payment_request(self, chat_id: int, user_id: int, tariff_key: str, username: str = None) -> bool:
        """
        Send a YooKassa payment request.
        
        Args:
            chat_id: Chat ID
            user_id: Telegram user ID
            tariff_key: Tariff key (14_days or 30_days)
            username: Telegram username (optional)
            
        Returns:
            True if request sent successfully
        """
        try:
            # Create payment and get URL
            payment_url = await self.create_yookassa_payment(user_id, tariff_key, username)
            if not payment_url:
                return False
            
            user_tariffs = self.get_user_tariffs(user_id)
            tariff_data = user_tariffs.get(tariff_key)
            if not tariff_data:
                logger.error(f"Unknown tariff for user {user_id}: {tariff_key}")
                return False
            
            # Build keyboard with payment button
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"💳 Оплатить {tariff_data['rub_price']} руб.",
                    url=payment_url
                )]
            ])
            
            # Send message with payment button
            await self.bot.send_message(
                chat_id=chat_id,
                text=f"💳 Оплата через банковскую карту\n\n"
                     f"📋 Тариф: {tariff_data['name']}\n"
                     f"💰 Сумма: {tariff_data['rub_price']} руб.\n\n"
                     f"Нажмите кнопку ниже для перехода к оплате:",
                reply_markup=keyboard
            )
            
            logger.info(f"YooKassa payment request sent to user {user_id}, tariff {tariff_key}")
            return True
            
        except TelegramAPIError as e:
            logger.error(f"Telegram API error while sending YooKassa payment request: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send YooKassa payment request to user {user_id}, tariff {tariff_key}: {e}")
            return False
    
    async def process_payment(self, pre_checkout_query) -> bool:
        """
        Process pre-checkout validation.
        
        Args:
            pre_checkout_query: Pre-checkout query object
            
        Returns:
            True if payment is valid
        """
        try:
            payload = pre_checkout_query.invoice_payload
            
            # Ensure it's our payment
            if not (payload.startswith('vpn_access_stars_') or payload.startswith('vpn_access_yookassa_')):
                await pre_checkout_query.answer(ok=False, error_message="Неверный тип платежа")
                return False
            
            # Validate amount based on payment type
            if payload.startswith('vpn_access_stars_'):
                # Stars payment: extract tariff from payload
                payload_parts = payload.split('_')
                if len(payload_parts) >= 4:
                    tariff_key = f"{payload_parts[3]}_{payload_parts[4]}" # type: ignore
                    user_id = int(payload_parts[-1]) # type: ignore
                    user_tariffs = self.get_user_tariffs(user_id)
                    tariff_data = user_tariffs.get(tariff_key, {})
                    expected_amount = tariff_data.get('stars_price', 1)
                    if pre_checkout_query.total_amount != expected_amount:
                        await pre_checkout_query.answer(ok=False, error_message="Неверная сумма платежа")
                        return False
            elif payload.startswith('vpn_access_yookassa_'):
                # YooKassa payment: extract tariff from payload
                payload_parts = payload.split('_')
                if len(payload_parts) >= 4:
                    tariff_key = f"{payload_parts[3]}_{payload_parts[4]}" # type: ignore
                    user_id = int(payload_parts[-1]) # type: ignore
                    user_tariffs = self.get_user_tariffs(user_id)
                    tariff_data = user_tariffs.get(tariff_key, {})
                    expected_amount = tariff_data.get('rub_price', 0) * 100  # In kopeks
                    if pre_checkout_query.total_amount != expected_amount:
                        await pre_checkout_query.answer(ok=False, error_message="Неверная сумма платежа")
                        return False
            
            # Confirm payment
            await pre_checkout_query.answer(ok=True)
            logger.info(f"Payment confirmed for user {pre_checkout_query.from_user.id}")
            return True
            
        except Exception as e:
            logger.error(f"Payment processing error: {e}")
            await pre_checkout_query.answer(ok=False, error_message="Ошибка обработки платежа")
            return False
    
    async def confirm_payment(self, successful_payment) -> tuple[bool, str, int]:
        """
        Confirm a successful payment.
        
        Args:
            successful_payment: Successful payment object
            
        Returns:
            Tuple (success, payment_type, amount_paid)
        """
        try:
            # Extract user_id and payment type from payload
            payload = successful_payment.invoice_payload
            
            if payload.startswith('vpn_access_stars_'):
                # Extract user_id from payload (format: vpn_access_stars_7_days_123456789)
                payload_parts = payload.split('_')
                user_id = int(payload_parts[-1])  # Last part is user_id
                payment_type = 'stars'
                amount_paid = successful_payment.total_amount
                logger.info(f"Stars payment confirmed: user {user_id}, stars: {amount_paid}")
                
            elif payload.startswith('vpn_access_yookassa_'):
                # Extract user_id from payload (format: vpn_access_yookassa_7_days_123456789)
                payload_parts = payload.split('_')
                user_id = int(payload_parts[-1])  # Last part is user_id
                payment_type = 'yookassa'
                amount_paid = successful_payment.total_amount
                logger.info(f"YooKassa payment confirmed: user {user_id}, kopeks: {amount_paid}")
                
            else:
                logger.error(f"Invalid payment payload: {payload}")
                return False, '', 0
            
            # Additional post-payment logic can be added here (admin notify, extra logging, etc.)
            
            return True, payment_type, amount_paid
            
        except Exception as e:
            logger.error(f"Payment confirmation error: {e}")
            return False, '', 0
    
    def get_payment_info(self) -> Dict[str, Any]:
        """
        Return available tariff info.
        
        Returns:
            Dict with tariff info
        """
        # Use first tariff to display a default period
        first_tariff = next(iter(self.tariffs.values())) if self.tariffs else None
        
        return {
            'tariffs': self.tariffs,
            'yookassa_available': bool(self.yookassa_client.shop_id and self.yookassa_client.secret_key),
            'period': first_tariff['name'] if first_tariff else '30 дней',
            'stars_price': first_tariff['stars_price'] if first_tariff else 200,
            'rub_price': first_tariff['rub_price'] if first_tariff else 300
        }
