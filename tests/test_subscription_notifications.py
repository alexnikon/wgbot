import unittest
from unittest.mock import AsyncMock, Mock

from subscription_notifications import SubscriptionNotificationWorker


class _NotificationDatabase:
    def __init__(self):
        self.marked = []

    def sync_expired_access_statuses(self):
        return 0

    def get_expired_peers(self):
        return [{"telegram_user_id": 10}]

    def get_users_for_hour_notification(self):
        return [{"telegram_user_id": 20}]

    def get_users_for_notification(self, days_before):
        assert days_before == 1
        return [{"telegram_user_id": 30}]

    def mark_expired_notification_sent(self, user_id):
        self.marked.append(("expired", user_id))
        return True

    def mark_hour_notification_sent(self, user_id):
        self.marked.append(("hour", user_id))
        return True

    def mark_notification_sent(self, user_id):
        self.marked.append(("day", user_id))
        return True


class SubscriptionNotificationWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_sends_each_notice_and_marks_delivery(self):
        database = _NotificationDatabase()
        bot = Mock()
        bot.send_message = AsyncMock()
        payment_manager = Mock()
        payment_manager.get_user_tariffs.return_value = {
            "14_days": {
                "name": "2 недели",
                "stars_price": 50,
                "rub_price": 125,
            }
        }
        worker = SubscriptionNotificationWorker(bot, database, payment_manager)

        await worker.run_once()

        self.assertEqual(bot.send_message.await_count, 3)
        self.assertEqual(
            database.marked,
            [("expired", 10), ("hour", 20), ("day", 30)],
        )
        self.assertEqual(payment_manager.get_user_tariffs.call_count, 2)
