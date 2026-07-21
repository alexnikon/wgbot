import logging
import uuid
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

from callbacks import PaymentAction, PaymentActionCallback, PaymentMethod, PaymentMethodCallback
from config import DOMAIN, PAYMENT_RETURN_URL, WEBHOOK_URL, get_tariffs
from database import Database
from yookassa_client import YooKassaClient

logger = logging.getLogger(__name__)

class PaymentManager:
    """Payment manager for Telegram Stars and YooKassa."""
    
    def __init__(
        self,
        bot: Bot,
        yookassa_client: YooKassaClient | None = None,
        db: Database | None = None,
        cascade_router: Any | None = None,
    ):
        self.bot = bot
        self.yookassa_client = yookassa_client or YooKassaClient()
        self.db = db or Database()
        self.webhook_url = WEBHOOK_URL
        self.domain = DOMAIN
        self.cascade_router = cascade_router
    
    @property
    def tariffs(self):
        """Get current tariffs from config (hot-reloaded)."""
        return get_tariffs()

    @property
    def visible_tariffs(self):
        """Return tariffs that should be displayed to clients."""
        return self.tariffs.copy()

    def is_tariff_enabled(self, tariff_key: str) -> bool:
        """Return whether a tariff exists for client-facing actions."""
        return tariff_key in self.tariffs

    @staticmethod
    def parse_invoice_payload(payload: str) -> tuple[str, str, int] | None:
        """Parse and validate a bot invoice payload."""
        if payload.startswith("vpn2:"):
            try:
                _, payment_id, tariff_key, raw_user_id = payload.split(":", 3)
                uuid.UUID(payment_id)
                user_id = int(raw_user_id)
            except (ValueError, TypeError):
                return None
            if tariff_key not in {"14_days", "30_days", "90_days"} or user_id <= 0:
                return None
            return "stars", tariff_key, user_id
        for payment_type in ("stars", "yookassa"):
            prefix = f"vpn_access_{payment_type}_"
            if not payload.startswith(prefix):
                continue
            remainder = payload[len(prefix):]
            try:
                tariff_key, raw_user_id = remainder.rsplit("_", 1)
                user_id = int(raw_user_id)
            except (ValueError, TypeError):
                return None
            if tariff_key not in {"14_days", "30_days", "90_days"} or user_id <= 0:
                return None
            return payment_type, tariff_key, user_id
        return None
        
    def get_user_tariffs(self, user_id: int) -> dict[str, Any]:
        """
        Return tariffs with user-specific discount/markup applied.
        """
        base_tariffs = self.visible_tariffs.copy()
        factor = self.db.get_user_promo_factor(user_id)
        
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
        yookassa_available = bool(self.yookassa_client.shop_id and self.yookassa_client.secret_key)
        buttons = []
        user_tariffs = self.get_user_tariffs(user_id)

        for tariff_key, tariff_data in user_tariffs.items():
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=tariff_data["name"],
                        callback_data=f"tariff_label_{tariff_key}",
                    )
                ]
            )
            if yookassa_available:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"⭐ {tariff_data['stars_price']} Stars",
                            callback_data=PaymentMethodCallback(
                                method=PaymentMethod.STARS,
                                tariff=tariff_key,
                                user_id=user_id,
                            ).pack(),
                        ),
                        InlineKeyboardButton(
                            text=f"💳 {tariff_data['rub_price']} руб.",
                            callback_data=PaymentMethodCallback(
                                method=PaymentMethod.YOOKASSA,
                                tariff=tariff_key,
                                user_id=user_id,
                            ).pack(),
                        ),
                    ]
                )
            else:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"⭐ {tariff_data['stars_price']} Stars",
                            callback_data=PaymentMethodCallback(
                                method=PaymentMethod.STARS,
                                tariff=tariff_key,
                                user_id=user_id,
                            ).pack(),
                        ),
                        InlineKeyboardButton(
                            text=f"💳 {tariff_data['rub_price']} руб.",
                            callback_data=PaymentMethodCallback(
                                method=PaymentMethod.YOOKASSA_DISABLED,
                                tariff=tariff_key,
                                user_id=user_id,
                            ).pack(),
                        ),
                    ]
                )

        buttons.append(
            [InlineKeyboardButton(text="🔙 Вернуться в меню", callback_data="main")]
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        return keyboard

    async def get_payment_selection_view(
        self, user_id: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Build text and keyboard for the tariff selection screen."""
        keyboard = await self.create_payment_selection_keyboard(user_id)
        payment_text = """
⏰ Выбери тариф VPN доступа:

Выбери удобный для тебя тариф:
        """
        return payment_text, keyboard
    
    async def create_stars_invoice(self, user_id: int, tariff_key: str, username: str = None) -> dict[str, Any] | None:
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
            payment_id = str(uuid.uuid4())
            invoice_payload = f"vpn2:{payment_id}:{tariff_key}:{user_id}"
            if not self.db.create_stars_payment_intent(
                payment_id,
                user_id,
                int(tariff_data["stars_price"]),
                tariff_key,
                invoice_payload,
                {"username": username or "", "source": "telegram_stars"},
            ):
                logger.error("Failed to persist Stars payment intent %s", payment_id)
                return None

            # Build payment button
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"⭐ Оплатить {tariff_data['stars_price']} звезд",
                            pay=True,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=PaymentActionCallback(
                                action=PaymentAction.CANCEL_STARS,
                                tariff=tariff_key,
                                user_id=user_id,
                            ).pack(),
                        )
                    ],
                ]
            )
            
            # Build invoice
            invoice_data = {
                'title': f'Доступ к сервису на {tariff_data["name"]} (Stars)',
                'description': (
                    f'Доступ к сервису на {tariff_data["name"]}\n\n'
                    f'Пользователь: @{username}'
                ) if username else f'Доступ к сервису на {tariff_data["name"]}',
                'payload': invoice_payload,
                'start_parameter': f'vpn-{user_id}-{uuid.uuid4().hex[:16]}',
                'provider_token': '',  # Not required for Telegram Stars
                'currency': 'XTR',  # Currency code for Telegram Stars
                'prices': [LabeledPrice(label=f'Доступ к сервису {tariff_data["name"]}', amount=tariff_data['stars_price'])],
                'reply_markup': keyboard
            }
            
            return invoice_data
            
        except Exception as e:
            logger.error(f"Failed to create Stars invoice for user {user_id}, tariff {tariff_key}: {e}")
            return None
    
    async def create_yookassa_payment(
        self,
        user_id: int,
        tariff_key: str,
        username: str = None,
        payment_chat_id: int | None = None,
        payment_message_id: int | None = None,
    ) -> str | None:
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
            reservation = self.db.get_active_reservation(user_id)
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
            return_url = PAYMENT_RETURN_URL

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
                'description': f'Доступ к сервису на {tariff_data["name"]}'
            }
            if reservation:
                metadata["server_key"] = reservation["server_key"]
                metadata["interface_id"] = reservation["interface_id"]
            if payment_chat_id is not None:
                metadata["payment_chat_id"] = str(payment_chat_id)
            if payment_message_id is not None:
                metadata["payment_message_id"] = str(payment_message_id)
            
            logger.info(f"Creating YooKassa payment for user {user_id}, tariff {tariff_key}, amount {amount} kopeks, metadata: {metadata}")
            
            # Create YooKassa payment
            payment_data = await self.yookassa_client.create_payment(
                amount=amount,
                currency='RUB',
                description=f'Доступ к сервису на {tariff_data["name"]}',
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
                metadata=metadata,
                currency="RUB",
                provider_payment_charge_id=payment_id,
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
            payment_text, keyboard = await self.get_payment_selection_view(user_id)
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
    
    async def send_stars_payment_request(
        self, chat_id: int, user_id: int, tariff_key: str, username: str = None
    ):
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
            
            sent = await self.bot.send_invoice(
                chat_id=chat_id,
                **invoice_data
            )
            self.db.set_stars_invoice_message(
                invoice_data["payload"], sent.message_id
            )
            
            logger.info(f"Stars payment request sent to user {user_id}, tariff {tariff_key}")
            return sent
            
        except TelegramAPIError as e:
            logger.error(f"Telegram API error while sending Stars payment request: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send Stars payment request to user {user_id}, tariff {tariff_key}: {e}")
            return False
    
    async def get_yookassa_payment_view(
        self,
        user_id: int,
        tariff_key: str,
        username: str = None,
        payment_chat_id: int | None = None,
        payment_message_id: int | None = None,
    ) -> tuple[str, InlineKeyboardMarkup] | None:
        """
        Build text and keyboard for the YooKassa payment screen inside the current message.
        """
        try:
            payment_url = await self.create_yookassa_payment(
                user_id,
                tariff_key,
                username,
                payment_chat_id=payment_chat_id,
                payment_message_id=payment_message_id,
            )
            if not payment_url:
                return None

            user_tariffs = self.get_user_tariffs(user_id)
            tariff_data = user_tariffs.get(tariff_key)
            if not tariff_data:
                logger.error(f"Unknown tariff for user {user_id}: {tariff_key}")
                return None

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"💳 Оплатить {tariff_data['rub_price']} руб.",
                            url=payment_url,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=PaymentActionCallback(
                                action=PaymentAction.CANCEL_YOOKASSA,
                                tariff=tariff_key,
                                user_id=user_id,
                            ).pack(),
                        )
                    ],
                ]
            )
            text = (
                "💳 Оплата через банковскую карту\n\n"
                f"📋 Тариф: {tariff_data['name']}\n"
                f"💰 Сумма: {tariff_data['rub_price']} руб.\n\n"
                "Нажмите кнопку ниже для перехода к оплате:"
            )
            return text, keyboard
        except Exception as e:
            logger.error(
                f"Failed to build YooKassa payment view for user {user_id}, tariff {tariff_key}: {e}"
            )
            return None
    
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
            parsed = self.parse_invoice_payload(payload)
            if not parsed:
                await pre_checkout_query.answer(ok=False, error_message="Неверный тип платежа")
                return False
            payment_type, tariff_key, user_id = parsed
            if pre_checkout_query.from_user.id != user_id:
                logger.warning(
                    "Rejected forwarded invoice: payer=%s owner=%s",
                    pre_checkout_query.from_user.id,
                    user_id,
                )
                await pre_checkout_query.answer(
                    ok=False, error_message="Этот счет создан для другого пользователя"
                )
                return False
            expected_currency = "XTR" if payment_type == "stars" else "RUB"
            if pre_checkout_query.currency != expected_currency:
                await pre_checkout_query.answer(
                    ok=False, error_message="Неверная валюта платежа"
                )
                return False
            if not self.db.get_active_reservation(user_id):
                await pre_checkout_query.answer(
                    ok=False,
                    error_message="Счет устарел. Вернитесь в бот и создайте новый счет.",
                )
                return False

            tariff_data = self.get_user_tariffs(user_id).get(tariff_key)
            if not tariff_data:
                await pre_checkout_query.answer(ok=False, error_message="Тариф недоступен")
                return False
            intent = self.db.get_payment_by_invoice_payload(payload)
            expected_amount = int(intent["amount"]) if intent else (
                tariff_data["stars_price"]
                if payment_type == "stars"
                else tariff_data["rub_price"] * 100
            )
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
    
    async def confirm_payment(
        self, successful_payment, payer_user_id: int | None = None
    ) -> tuple[bool, str, int]:
        """
        Confirm a successful payment.
        
        Args:
            successful_payment: Successful payment object
            
        Returns:
            Tuple (success, payment_type, amount_paid)
        """
        try:
            payload = successful_payment.invoice_payload
            parsed = self.parse_invoice_payload(payload)
            if not parsed:
                logger.error(f"Invalid payment payload: {payload}")
                return False, '', 0
            payment_type, _, user_id = parsed
            if payer_user_id is not None and payer_user_id != user_id:
                logger.error(
                    "Successful payment payer mismatch: payer=%s owner=%s",
                    payer_user_id,
                    user_id,
                )
                return False, "", 0
            expected_currency = "XTR" if payment_type == "stars" else "RUB"
            if successful_payment.currency != expected_currency:
                logger.error(
                    "Successful payment currency mismatch: expected=%s actual=%s",
                    expected_currency,
                    successful_payment.currency,
                )
                return False, "", 0
            amount_paid = successful_payment.total_amount
            logger.info(
                "%s payment confirmed: user %s, amount %s",
                payment_type,
                user_id,
                amount_paid,
            )
            return True, payment_type, amount_paid
            
        except Exception as e:
            logger.error(f"Payment confirmation error: {e}")
            return False, '', 0
    
    def get_payment_info(self) -> dict[str, Any]:
        """
        Return available tariff info.
        
        Returns:
            Dict with tariff info
        """
        # Use first tariff to display a default period
        first_tariff = (
            next(iter(self.visible_tariffs.values())) if self.visible_tariffs else None
        )
        
        return {
            'tariffs': self.visible_tariffs,
            'yookassa_available': bool(self.yookassa_client.shop_id and self.yookassa_client.secret_key),
            'period': first_tariff['name'] if first_tariff else '30 дней',
            'stars_price': first_tariff['stars_price'] if first_tariff else 200,
            'rub_price': first_tariff['rub_price'] if first_tariff else 300
        }
