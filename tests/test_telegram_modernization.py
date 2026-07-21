import asyncio
import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

import bot as bot_module
from callbacks import PaymentMethod, PaymentMethodCallback, RefundConfirmationCallback
from database import Database
from handlers.admin import AdminWorkflowService
from handlers.payments import (
    _parse_legacy_method,
    confirm_stars_refund,
    handle_pay_stars_callback,
    process_refunded_payment,
)
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

    async def test_config_is_sent_once_with_caption_and_home_keyboard(self):
        fake_bot = SimpleNamespace(send_document=AsyncMock())
        keyboard = SimpleNamespace()
        with (
            patch.object(bot_module, "bot", fake_bot, create=True),
            patch.object(bot_module, "create_home_keyboard", return_value=keyboard),
        ):
            self.assertTrue(
                await bot_module.send_config_with_confirmation(10, b"config")
            )
        fake_bot.send_document.assert_awaited_once()
        arguments = fake_bot.send_document.await_args.kwargs
        self.assertIn("AmneziaWG", arguments["caption"])
        self.assertIs(arguments["reply_markup"], keyboard)


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

    def test_expired_admin_workflow_is_removed(self):
        self.db.set_admin_workflow(1, "input", "waiting", {}, ttl_hours=0)
        self.assertIsNone(self.db.get_admin_workflow(1, "input"))

    def test_daily_legacy_callback_counter_and_zero_streak(self):
        self.db.ensure_telegram_daily_metrics_day()
        self.assertEqual(self.db.get_legacy_callback_zero_streak(), 1)
        self.db.record_telegram_daily_metric("legacy_callbacks")
        self.assertEqual(self.db.get_legacy_callback_zero_streak(), 0)
        self.assertEqual(
            self.db.get_runtime_stats()["legacy_callbacks_today"], 1
        )

    def test_star_discrepancy_approval_is_atomic_and_does_not_grant_access(self):
        self.db.record_star_transaction(
            "manual-review",
            "incoming",
            50,
            1,
            transaction_type="invoice_payment",
            user_id=31,
            status="discrepancy",
        )
        review_id = self.db.list_star_discrepancies()[0]["review_id"]
        self.assertTrue(self.db.approve_star_discrepancy(review_id, 999))
        self.assertFalse(self.db.approve_star_discrepancy(review_id, 999))
        self.assertEqual(self.db.count_star_discrepancies(), 0)
        self.assertIsNone(self.db.get_peer_by_telegram_id(31))

    def test_daily_star_summary_separates_receipts_refunds_and_discrepancies(self):
        self.db.record_star_transaction(
            "received", "incoming", 100, 1, status="applied"
        )
        self.db.record_star_transaction(
            "refunded", "outgoing", -40, 2, status="refund_pending_review"
        )
        self.db.record_star_transaction(
            "unknown", "incoming", 20, 3, status="discrepancy"
        )
        summary = self.db.get_star_daily_summary()
        self.assertEqual(summary["received_stars"], 120)
        self.assertEqual(summary["refunded_stars"], 40)
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(summary["discrepancies"], 1)

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

    async def test_retry_after_retries_only_the_operation(self):
        sender = TelegramSender(SimpleNamespace(), SimpleNamespace())
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TelegramRetryAfter(SimpleNamespace(), "slow down", 1)
            return "sent"

        with patch("telegram_runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await sender.call(42, operation), "sent")
        self.assertEqual(attempts, 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_network_error_uses_bounded_backoff(self):
        sender = TelegramSender(SimpleNamespace(), SimpleNamespace())
        operation = AsyncMock(
            side_effect=TelegramNetworkError(SimpleNamespace(), "offline")
        )
        with patch("telegram_runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertIsNone(await sender.call(42, operation))
        self.assertEqual(operation.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_bad_request_is_not_retried(self):
        sender = TelegramSender(SimpleNamespace(), SimpleNamespace())
        operation = AsyncMock(
            side_effect=TelegramBadRequest(SimpleNamespace(), "invalid")
        )
        self.assertIsNone(await sender.call(42, operation))
        self.assertEqual(operation.await_count, 1)


class TelegramHandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_malformed_legacy_payment_callbacks_are_rejected(self):
        manager = SimpleNamespace(is_tariff_enabled=lambda tariff: tariff == "14_days")
        self.assertIsNone(_parse_legacy_method("pay_stars_bad", "stars", manager))
        self.assertIsNone(
            _parse_legacy_method("pay_stars_14_days_not-a-user", "stars", manager)
        )
        self.assertIsNone(
            _parse_legacy_method("pay_stars_unknown_10", "stars", manager)
        )

    async def test_payment_callback_is_acknowledged_before_cascade(self):
        events = []

        async def acknowledge(*_args, **_kwargs):
            events.append("ack")

        class FakeCascade:
            async def ensure_reservation(self, _user_id):
                events.append("cascade")

        class FakePaymentManager:
            def is_tariff_enabled(self, _tariff):
                return True

            async def send_stars_payment_request(self, *_args):
                events.append("invoice")
                return True

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=55, username="alice"),
            message=SimpleNamespace(chat=SimpleNamespace(id=55)),
        )
        await handle_pay_stars_callback(
            callback,
            FakePaymentManager(),
            FakeCascade(),
            acknowledge,
            AsyncMock(),
            lambda: None,
            UserActionLocks(),
            SimpleNamespace(telegram_event=lambda _name: None),
            PaymentMethodCallback(
                method=PaymentMethod.STARS, tariff="14_days", user_id=55
            ),
        )
        self.assertEqual(events, ["ack", "cascade", "invoice"])

    async def test_command_scopes_are_registered(self):
        fake_bot = SimpleNamespace(set_my_commands=AsyncMock())
        with (
            patch.object(bot_module, "bot", fake_bot, create=True),
            patch.object(bot_module, "get_admin_telegram_ids", return_value=[99]),
        ):
            await bot_module.register_bot_commands()
        self.assertEqual(fake_bot.set_my_commands.await_count, 2)
        default_commands = fake_bot.set_my_commands.await_args_list[0].args[0]
        admin_commands = fake_bot.set_my_commands.await_args_list[1].args[0]
        self.assertEqual(
            [command.command for command in default_commands],
            ["start", "status", "connect", "buy", "help"],
        )
        self.assertIn("stars_reconcile", [command.command for command in admin_commands])

    async def test_my_chat_member_transitions_reachability(self):
        database = SimpleNamespace(
            mark_telegram_unreachable=unittest.mock.Mock(),
            mark_telegram_reachable=unittest.mock.Mock(),
        )
        event = SimpleNamespace(
            chat=SimpleNamespace(id=45, type=ChatType.PRIVATE),
            new_chat_member=SimpleNamespace(status=ChatMemberStatus.KICKED),
        )
        await bot_module.handle_bot_chat_member_update(event, database)
        database.mark_telegram_unreachable.assert_called_once()
        event.new_chat_member.status = ChatMemberStatus.MEMBER
        await bot_module.handle_bot_chat_member_update(event, database)
        database.mark_telegram_reachable.assert_called_once_with(45)

    async def test_duplicate_refund_confirmation_calls_telegram_once(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:70"
            database.create_stars_payment_intent(
                payment_id, 70, 100, "14_days", payload
            )
            database.apply_verified_payment(
                payment_id,
                70,
                None,
                100,
                "stars",
                "14_days",
                14,
                telegram_payment_charge_id="charge-70",
                invoice_payload=payload,
            )
            telegram_bot = SimpleNamespace(refund_star_payment=AsyncMock())
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=1),
                message=SimpleNamespace(edit_text=AsyncMock()),
            )
            callback_data = RefundConfirmationCallback(payment_id=payment_id)
            safe_answer = AsyncMock()
            for _ in range(2):
                await confirm_stars_refund(
                    callback,
                    callback_data,
                    telegram_bot,
                    database,
                    safe_answer,
                    lambda _user_id: True,
                )
            self.assertEqual(telegram_bot.refund_star_payment.await_count, 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_refunded_payment_handler_never_shortens_access(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            payment_id = str(uuid.uuid4())
            payload = f"vpn2:{payment_id}:14_days:80"
            database.create_stars_payment_intent(
                payment_id, 80, 100, "14_days", payload
            )
            applied = database.apply_verified_payment(
                payment_id,
                80,
                None,
                100,
                "stars",
                "14_days",
                14,
                telegram_payment_charge_id="charge-80",
                invoice_payload=payload,
            )
            message = SimpleNamespace(
                refunded_payment=SimpleNamespace(
                    telegram_payment_charge_id="charge-80",
                    total_amount=100,
                    invoice_payload=payload,
                ),
                date=SimpleNamespace(timestamp=lambda: 1),
                from_user=SimpleNamespace(id=80),
            )
            await process_refunded_payment(message, database, AsyncMock())
            self.assertEqual(
                database.get_peer_by_telegram_id(80)["expire_date"],
                applied["expire_date"],
            )
            self.assertEqual(
                database.get_payment_by_id(payment_id)["status"], "refunded"
            )
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
    async def test_reconciliation_reads_multiple_pages(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            database = Database(path)
            transactions = [
                SimpleNamespace(
                    id=f"generic-{index}",
                    amount=1,
                    date=index + 1,
                    source=SimpleNamespace(
                        transaction_type="fragment",
                        user=SimpleNamespace(id=500 + index),
                        invoice_payload=None,
                    ),
                    receiver=None,
                )
                for index in range(101)
            ]
            offsets = []

            class FakeBot:
                async def get_star_transactions(self, *, offset, limit):
                    offsets.append(offset)
                    return SimpleNamespace(
                        transactions=transactions[offset : offset + limit]
                    )

            reconciler = StarsReconciler(
                FakeBot(),
                database,
                SimpleNamespace(),
                SimpleNamespace(),
                AsyncMock(),
                3600,
            )
            result = await reconciler.run_once()
            self.assertEqual(result.observed, 101)
            self.assertEqual(offsets, [0, 100])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

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
