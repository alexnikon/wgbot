import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import webhook_server
from callbacks import PaymentAction, PaymentActionCallback, YooKassaCancelCallback
from database import Database
from handlers.payments import handle_cancel_yookassa_callback
from payment import PaymentManager


class YooKassaCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)
        self.original_webhook_database = getattr(webhook_server, "db", None)
        webhook_server.db = self.db

    def tearDown(self):
        if self.original_webhook_database is None:
            del webhook_server.db
        else:
            webhook_server.db = self.original_webhook_database
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def add_payment(self, payment_id: str = "payment-1", user_id: int = 7):
        self.db.add_payment(
            payment_id,
            user_id,
            15000,
            "yookassa",
            "14_days",
            currency="RUB",
            provider_payment_charge_id=payment_id,
        )

    async def test_payment_view_uses_compact_payment_specific_callback(self):
        manager = PaymentManager(
            SimpleNamespace(),
            yookassa_client=SimpleNamespace(shop_id="shop", secret_key="secret"),
            db=SimpleNamespace(get_user_promo_factor=lambda _user_id: 1.0),
        )
        payment_id = "2f9d15c0-000f-5000-9000-1a2b3c4d5e6f"
        manager.create_yookassa_payment = AsyncMock(
            return_value=(payment_id, "https://example.test/pay")
        )

        _, keyboard = await manager.get_yookassa_payment_view(7, "14_days")

        cancel_data = keyboard.inline_keyboard[1][0].callback_data
        self.assertLessEqual(len(cancel_data.encode("utf-8")), 64)
        self.assertEqual(
            YooKassaCancelCallback.unpack(cancel_data),
            YooKassaCancelCallback(payment_id=payment_id),
        )

    async def test_customer_metadata_is_only_persisted_locally(self):
        client = SimpleNamespace(
            shop_id="shop",
            secret_key="secret",
            create_payment=AsyncMock(
                return_value={
                    "id": "payment-private",
                    "status": "pending",
                    "confirmation": {"confirmation_url": "https://example.test/pay"},
                }
            ),
        )
        manager = PaymentManager(SimpleNamespace(), yookassa_client=client, db=self.db)

        result = await manager.create_yookassa_payment(
            424242,
            "14_days",
            "test_customer",
            payment_chat_id=424242,
            payment_message_id=99,
        )

        self.assertEqual(
            result,
            ("payment-private", "https://example.test/pay"),
        )
        provider_request = client.create_payment.await_args.kwargs
        self.assertNotIn("metadata", provider_request)
        serialized_request = json.dumps(provider_request)
        self.assertNotIn("424242", serialized_request)
        self.assertNotIn("test_customer", serialized_request)
        payment = self.db.get_payment_by_id("payment-private")
        metadata = json.loads(payment["metadata"])
        self.assertEqual(metadata["user_id"], "424242")
        self.assertEqual(metadata["username"], "test_customer")
        self.assertEqual(metadata["tariff_key"], "14_days")
        self.assertEqual(metadata["payment_chat_id"], "424242")
        self.assertEqual(metadata["payment_message_id"], "99")

    async def test_cancel_callback_marks_attempt(self):
        self.add_payment()
        manager = SimpleNamespace(
            db=self.db,
            get_payment_selection_view=AsyncMock(return_value=("Tariffs", "Keyboard")),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            data=YooKassaCancelCallback(payment_id="payment-1").pack(),
            message=SimpleNamespace(),
        )
        answer = AsyncMock()
        edit = AsyncMock()

        await handle_cancel_yookassa_callback(
            callback,
            manager,
            answer,
            edit,
            YooKassaCancelCallback(payment_id="payment-1"),
        )

        self.assertEqual(self.db.get_payment_by_id("payment-1")["status"], "canceled")
        answer.assert_awaited_once_with(callback, "✅ Платеж отменен")
        edit.assert_awaited_once_with(
            callback.message,
            "Tariffs",
            reply_markup="Keyboard",
        )

    async def test_cancel_callback_rejects_another_user(self):
        self.add_payment()
        manager = SimpleNamespace(
            db=self.db,
            get_payment_selection_view=AsyncMock(),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=8),
            data=YooKassaCancelCallback(payment_id="payment-1").pack(),
            message=SimpleNamespace(),
        )
        answer = AsyncMock()
        edit = AsyncMock()

        await handle_cancel_yookassa_callback(
            callback,
            manager,
            answer,
            edit,
            YooKassaCancelCallback(payment_id="payment-1"),
        )

        self.assertEqual(self.db.get_payment_by_id("payment-1")["status"], "pending")
        answer.assert_awaited_once_with(callback, "❌ Ошибка: неверный пользователь")
        edit.assert_not_awaited()

    async def test_repeated_and_completed_cancellations_are_idempotent(self):
        for status, expected_answer in (
            ("canceled", "Платеж уже отменен"),
            ("succeeded", "✅ Платеж уже обработан"),
        ):
            with self.subTest(status=status):
                payment_id = f"payment-{status}"
                self.add_payment(payment_id)
                self.db.update_payment_status_by_id(payment_id, status)
                manager = SimpleNamespace(
                    db=self.db,
                    get_payment_selection_view=AsyncMock(
                        return_value=("Tariffs", "Keyboard")
                    ),
                )
                callback = SimpleNamespace(
                    from_user=SimpleNamespace(id=7),
                    data=YooKassaCancelCallback(payment_id=payment_id).pack(),
                    message=SimpleNamespace(),
                )
                answer = AsyncMock()
                edit = AsyncMock()

                await handle_cancel_yookassa_callback(
                    callback,
                    manager,
                    answer,
                    edit,
                    YooKassaCancelCallback(payment_id=payment_id),
                )

                self.assertEqual(
                    self.db.get_payment_by_id(payment_id)["status"],
                    status,
                )
                answer.assert_awaited_once_with(callback, expected_answer)
                edit.assert_awaited_once()

    async def test_legacy_cancel_button_only_returns_to_tariffs(self):
        manager = SimpleNamespace(
            db=SimpleNamespace(),
            get_payment_selection_view=AsyncMock(
                return_value=("Tariffs", "Keyboard")
            ),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            data="cancel_yookassa_7",
            message=SimpleNamespace(),
        )
        answer = AsyncMock()
        edit = AsyncMock()

        await handle_cancel_yookassa_callback(
            callback,
            manager,
            answer,
            edit,
            PaymentActionCallback(
                action=PaymentAction.CANCEL_YOOKASSA,
                tariff="14_days",
                user_id=7,
            ),
        )

        answer.assert_awaited_once_with(callback)
        edit.assert_awaited_once_with(
            callback.message,
            "Tariffs",
            reply_markup="Keyboard",
        )

    async def test_late_provider_cancellation_does_not_notify_user(self):
        self.add_payment()
        self.assertTrue(self.db.cancel_pending_payment("payment-1"))

        with patch.object(
            webhook_server,
            "send_telegram_message",
            AsyncMock(),
        ) as send_message:
            await webhook_server.process_canceled_payment({"id": "payment-1"})

        send_message.assert_not_awaited()

    def test_verified_payment_wins_after_local_cancellation_exactly_once(self):
        self.add_payment()
        self.assertTrue(self.db.cancel_pending_payment("payment-1"))

        first = self.db.apply_verified_payment(
            "payment-1",
            7,
            "alice",
            15000,
            "yookassa",
            "14_days",
            14,
        )
        second = self.db.apply_verified_payment(
            "payment-1",
            7,
            "alice",
            15000,
            "yookassa",
            "14_days",
            14,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.db.get_payment_by_id("payment-1")["status"], "succeeded")
        self.assertEqual(self.db.get_peer_by_telegram_id(7)["payment_status"], "paid")

    def test_canceled_stars_intent_cannot_be_applied(self):
        self.db.add_payment("stars-1", 7, 100, "stars", "14_days")
        self.assertTrue(self.db.cancel_pending_payment("stars-1"))

        result = self.db.apply_verified_payment(
            "stars-1",
            7,
            "alice",
            100,
            "stars",
            "14_days",
            14,
        )

        self.assertIsNone(result)
        self.assertEqual(self.db.get_payment_by_id("stars-1")["status"], "canceled")


if __name__ == "__main__":
    unittest.main()
