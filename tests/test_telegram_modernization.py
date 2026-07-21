import asyncio
import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import bot as bot_module
from callbacks import PaymentMethod, PaymentMethodCallback
from database import Database
from handlers.admin import AdminWorkflowService
from payment import PaymentManager
from stars import StarsReconciler
from telegram_runtime import (
    TelegramSender,
    TelegramUIRenderer,
    UserActionLocks,
    redact_telegram_content,
)


class TelegramModernizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_action_locks_serialize_and_cleanup(self):
        locks = UserActionLocks()
        active = 0
        maximum = 0

        async def operation():
            nonlocal active, maximum
            async with locks.hold(10):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(operation(), operation())
        self.assertEqual(maximum, 1)
        self.assertEqual(locks.active_keys, 0)

    async def test_different_users_are_not_serialized(self):
        locks = UserActionLocks()
        entered = asyncio.Event()
        both_entered = asyncio.Event()
        count = 0

        async def operation(user_id):
            nonlocal count
            async with locks.hold(user_id):
                count += 1
                if count == 1:
                    entered.set()
                if count == 2:
                    both_entered.set()
                await asyncio.wait_for(both_entered.wait(), timeout=0.2)

        first = asyncio.create_task(operation(10))
        await entered.wait()
        second = asyncio.create_task(operation(20))
        await asyncio.gather(first, second)
        self.assertEqual(count, 2)

    def test_typed_payment_callback_round_trip(self):
        packed = PaymentMethodCallback(
            method=PaymentMethod.STARS, tariff="30_days", user_id=123
        ).pack()
        unpacked = PaymentMethodCallback.unpack(packed)
        self.assertEqual(unpacked.method, PaymentMethod.STARS)
        self.assertEqual(unpacked.tariff, "30_days")
        self.assertEqual(unpacked.user_id, 123)

    def test_v2_invoice_payload_is_owner_bound(self):
        payment_id = str(uuid.uuid4())
        self.assertEqual(
            PaymentManager.parse_invoice_payload(f"vpn2:{payment_id}:30_days:123"),
            ("stars", "30_days", 123),
        )
        self.assertIsNone(
            PaymentManager.parse_invoice_payload(f"vpn2:{payment_id}:unknown:123")
        )

    def test_log_preview_redacts_credentials(self):
        preview = redact_telegram_content(
            "PrivateKey = abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        )
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", preview)
        self.assertIn("[REDACTED]", preview)

    async def test_rich_renderer_falls_back_to_plain_text(self):
        class FakeBot:
            def __init__(self):
                self.fallback = None

            async def send_rich_message(self, **kwargs):
                raise TelegramBadRequest(SimpleNamespace(), "unsupported")

            async def send_message(self, **kwargs):
                self.fallback = kwargs
                return "sent"

        fake = FakeBot()
        renderer = TelegramUIRenderer(fake)
        result = await renderer.send_rich_or_text(
            10, rich_markdown="# Status", fallback_text="Status"
        )
        self.assertEqual(result, "sent")
        self.assertEqual(fake.fallback["text"], "Status")

    def test_polling_source_does_not_use_skip_updates(self):
        import inspect

        source = inspect.getsource(bot_module.main)
        self.assertNotIn("skip_updates", source)
        self.assertIn("tasks_concurrency_limit", source)

    async def test_dispatcher_injects_workflow_dependencies(self):
        from telegram_runtime import serialized_user_action

        dispatcher = Dispatcher()
        router = Router()
        locks = UserActionLocks()
        observed = []

        @router.message()
        @serialized_user_action
        async def injected_handler(message, user_action_locks: UserActionLocks):
            observed.append((message.from_user.id, user_action_locks.active_keys))

        dispatcher.include_router(router)
        dispatcher.workflow_data["user_action_locks"] = locks
        test_bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
        try:
            await dispatcher.feed_raw_update(
                test_bot,
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": 1,
                        "chat": {"id": 77, "type": "private"},
                        "from": {"id": 77, "is_bot": False, "first_name": "Test"},
                        "text": "hello",
                    },
                },
            )
        finally:
            await test_bot.session.close()

        self.assertEqual(observed, [(77, 1)])
        self.assertEqual(locks.active_keys, 0)


class TelegramDatabaseTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_admin_workflow_survives_service_recreation(self):
        first = AdminWorkflowService(self.db)
        first.set(1, "await_message", mode="all")
        second = AdminWorkflowService(Database(self.path))
        self.assertEqual(second.get(1)["state"], "await_message")
        self.assertEqual(second.get(1)["mode"], "all")
        second.clear(1)
        self.assertIsNone(first.get(1))

    def test_unreachable_clients_are_excluded_and_can_return(self):
        self.db.upsert_client(1, "one")
        self.db.upsert_client(2, "two")
        self.db.mark_telegram_unreachable(1, "TelegramForbiddenError")
        self.assertEqual(self.db.get_client_telegram_ids(), [2])
        self.db.mark_telegram_reachable(1)
        self.assertEqual(self.db.get_client_telegram_ids(), [1, 2])

    def test_star_payment_fields_and_refund_review_do_not_reduce_access(self):
        payment_id = str(uuid.uuid4())
        payload = f"vpn2:{payment_id}:14_days:7"
        self.assertTrue(
            self.db.create_stars_payment_intent(
                payment_id, 7, 100, "14_days", payload
            )
        )
        result = self.db.apply_verified_payment(
            payment_id,
            7,
            "alice",
            100,
            "stars",
            "14_days",
            14,
            telegram_payment_charge_id="charge-1",
            provider_payment_charge_id="provider-1",
            invoice_payload=payload,
        )
        expiry = result["expire_date"]
        self.assertTrue(self.db.mark_stars_refund_observed("charge-1", 100))
        payment = self.db.get_payment_by_id(payment_id)
        self.assertEqual(payment["telegram_payment_charge_id"], "charge-1")
        self.assertEqual(payment["refund_review_status"], "pending_review")
        self.assertEqual(self.db.get_peer_by_telegram_id(7)["expire_date"], expiry)

    def test_star_ledger_distinguishes_payment_and_refund_direction(self):
        self.assertTrue(
            self.db.record_star_transaction("same-id", "incoming", 100, 1)
        )
        self.assertTrue(
            self.db.record_star_transaction("same-id", "outgoing", 100, 2)
        )
        self.assertFalse(
            self.db.record_star_transaction("same-id", "incoming", 100, 1)
        )

    def test_payment_schema_keeps_provider_and_telegram_ids_separate(self):
        payment_id = str(uuid.uuid4())
        payload = f"vpn2:{payment_id}:14_days:9"
        self.db.create_stars_payment_intent(payment_id, 9, 100, "14_days", payload)
        self.db.apply_verified_payment(
            payment_id,
            9,
            None,
            100,
            "stars",
            "14_days",
            14,
            telegram_payment_charge_id="tg-charge",
            provider_payment_charge_id="provider-charge",
            invoice_payload=payload,
        )
        row = self.db.get_payment_by_id(payment_id)
        self.assertEqual(row["telegram_payment_charge_id"], "tg-charge")
        self.assertEqual(row["provider_payment_charge_id"], "provider-charge")

    def test_exact_legacy_star_id_match_is_backfilled_without_reapplying_access(self):
        charge_id = "legacy-charge-id"
        payload = "vpn_access_stars_14_days_19"
        self.db.add_payment(
            charge_id,
            19,
            100,
            "stars",
            "14_days",
            currency="RUB",
        )
        self.db.update_payment_status_by_id(charge_id, "succeeded")
        self.db.record_star_transaction(
            charge_id,
            "incoming",
            100,
            1,
            transaction_type="invoice_payment",
            user_id=19,
            invoice_payload=payload,
            status="discrepancy",
        )

        self.assertEqual(self.db.repair_legacy_star_payment_matches(), 1)
        self.assertEqual(self.db.repair_legacy_star_payment_matches(), 0)
        payment = self.db.get_payment_by_id(charge_id)
        self.assertEqual(payment["telegram_payment_charge_id"], charge_id)
        self.assertEqual(payment["invoice_payload"], payload)
        self.assertEqual(payment["currency"], "XTR")
        self.assertIsNone(self.db.get_peer_by_telegram_id(19))
        self.assertEqual(self.db.count_star_discrepancies(), 0)


class TelegramSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_forbidden_marks_user_unreachable(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            db = Database(path)
            sender = TelegramSender(SimpleNamespace(), db)

            async def forbidden():
                raise TelegramForbiddenError(SimpleNamespace(), "blocked")

            self.assertIsNone(await sender.call(42, forbidden))
            self.assertNotIn(42, db.get_client_telegram_ids())
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass


class PreCheckoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_precheckout_rejects_invoice_without_active_reservation(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            db = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:10"
            db.create_stars_payment_intent(payment_id, 10, 100, "14_days", payload)
            manager = PaymentManager(SimpleNamespace(), db=db)
            answers = []

            async def answer(**kwargs):
                answers.append(kwargs)

            query = SimpleNamespace(
                invoice_payload=payload,
                from_user=SimpleNamespace(id=10),
                total_amount=100,
                currency="XTR",
                answer=answer,
            )
            self.assertFalse(await manager.process_payment(query))
            self.assertFalse(answers[0]["ok"])
            self.assertIn("устарел", answers[0]["error_message"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_successful_update_is_applied_from_star_ledger(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            db = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:50"
            db.create_stars_payment_intent(
                payment_id,
                50,
                100,
                "14_days",
                payload,
                {"username": "alice"},
            )
            transaction = SimpleNamespace(
                id="tg-charge-50",
                amount=100,
                date=1000,
                source=SimpleNamespace(
                    transaction_type="invoice_payment",
                    user=SimpleNamespace(id=50),
                    invoice_payload=payload,
                ),
                receiver=None,
            )

            class FakeBot:
                async def get_star_transactions(self, **kwargs):
                    return SimpleNamespace(transactions=[transaction])

            async def notify(_text):
                return None

            manager = PaymentManager(SimpleNamespace(), db=db)
            reconciler = StarsReconciler(
                FakeBot(),
                db,
                manager,
                SimpleNamespace(),
                notify,
                3600,
            )
            result = await reconciler.run_once()
            self.assertEqual(result.applied, 1)
            payment = db.get_payment_by_id(payment_id)
            self.assertEqual(payment["status"], "succeeded")
            self.assertEqual(payment["telegram_payment_charge_id"], "tg-charge-50")
            tasks = db.get_pending_provisioning_tasks()
            self.assertEqual(tasks[0]["operation"], "create_peer")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass
