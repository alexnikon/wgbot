import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import Database
from payment import PaymentManager
from telegram_runtime import TelegramSender

logger = logging.getLogger(__name__)


def renewal_keyboard(payment_text: str = "💵 Продлить доступ") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=payment_text, callback_data="extend")],
            [InlineKeyboardButton(text="На главную", callback_data="main")],
        ]
    )


class SubscriptionNotificationWorker:
    """Send subscription expiry notifications without blocking the event loop."""

    def __init__(
        self,
        bot: Bot,
        db: Database,
        payment_manager: PaymentManager,
        interval_seconds: int = 30 * 60,
        telegram_sender: TelegramSender | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.payment_manager = payment_manager
        self.interval_seconds = interval_seconds
        self.telegram_sender = telegram_sender or TelegramSender(bot, db)

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Subscription notification cycle failed")
                await asyncio.sleep(60)

    async def run_once(self) -> None:
        await asyncio.to_thread(self.db.sync_expired_access_statuses)
        expired = await asyncio.to_thread(self.db.get_expired_peers)
        for subscription in expired:
            await self._send_expired(subscription)

        hour_notifications = await asyncio.to_thread(
            self.db.get_users_for_hour_notification
        )
        for subscription in hour_notifications:
            await self._send_reminder(subscription, "через 1 час")

        day_notifications = await asyncio.to_thread(self.db.get_users_for_notification, 1)
        for subscription in day_notifications:
            await self._send_reminder(subscription, "завтра")

    async def _send_expired(self, subscription: dict) -> None:
        user_id = int(subscription["telegram_user_id"])
        try:
            sent = await self.telegram_sender.call(
                user_id,
                lambda: self.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ Оплаченный период закончился, для возобновления доступа "
                        "к сервису, необходимо оплатить доступ."
                    ),
                    reply_markup=renewal_keyboard("💳 Купить доступ"),
                ),
            )
            if sent:
                await asyncio.to_thread(self.db.mark_expired_notification_sent, user_id)
        except TelegramAPIError:
            logger.warning("Failed to send expiration notice to user %s", user_id)

    async def _send_reminder(self, subscription: dict, deadline: str) -> None:
        user_id = int(subscription["telegram_user_id"])
        tariffs = self.payment_manager.get_user_tariffs(user_id)
        tariff_text = "".join(
            f"⭐ {tariff['name']} - {tariff['stars_price']} Stars\n"
            f"💳 {tariff['name']} - {tariff['rub_price']} руб.\n\n"
            for tariff in tariffs.values()
        )
        try:
            sent = await self.telegram_sender.call(
                user_id,
                lambda: self.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"⏰ Доступ к nikonVPN истекает {deadline}!\n\n"
                        f"💎 Доступные тарифы для продления:\n{tariff_text}"
                    ),
                    reply_markup=renewal_keyboard(),
                ),
            )
            marker = (
                self.db.mark_hour_notification_sent
                if deadline == "через 1 час"
                else self.db.mark_notification_sent
            )
            if sent:
                await asyncio.to_thread(marker, user_id)
        except TelegramAPIError:
            logger.warning("Failed to send %s reminder to user %s", deadline, user_id)
