import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from callbacks import PaymentMethod, PaymentMethodCallback
from cascade_api import CascadeRouter, CascadeServer
from database import COMPLIMENTARY_CASCADE_EXPIRY, Database
from handlers.payments import handle_pay_stars_callback
from provisioning import ProvisioningWorker
from telegram_runtime import UserActionLocks


class ComplimentaryDatabaseTests(unittest.TestCase):
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

    def test_admin_add_client_is_idempotent_and_unpaid(self):
        created = self.db.admin_add_client(123456, 99)
        existing = self.db.admin_add_client(123456, 100)

        self.assertEqual(created["telegram_user_id"], 123456)
        self.assertEqual(existing["telegram_user_id"], 123456)
        self.assertEqual(existing["payment_status"], "unpaid")
        with sqlite3.connect(self.path) as connection:
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM operation_logs WHERE operation='admin_add_client'"
            ).fetchone()[0]
        self.assertEqual(audit_count, 1)

    def test_access_precedence_preserves_paid_expiry(self):
        paid_expiry = "2030-01-01 00:00:00"
        self.db.ensure_subscription(
            10, "alice", paid_expiry, "paid", "30_days", "stars"
        )
        self.assertEqual(self.db.get_client_access_state(10).source, "paid")

        self.assertTrue(self.db.set_client_complimentary(10, 99, True))
        complimentary = self.db.get_client_access_state(10)
        self.assertTrue(complimentary.active)
        self.assertEqual(complimentary.source, "complimentary")
        self.assertEqual(complimentary.cascade_expiry, COMPLIMENTARY_CASCADE_EXPIRY)
        self.assertEqual(complimentary.paid_expiry, paid_expiry)

        self.assertTrue(self.db.set_client_ban(10, 99, True, "review"))
        banned = self.db.get_client_access_state(10)
        self.assertFalse(banned.active)
        self.assertEqual(banned.source, "none")
        self.assertTrue(banned.is_complimentary)

        self.assertTrue(self.db.set_client_ban(10, 99, False))
        self.assertEqual(
            self.db.get_client_access_state(10).source, "complimentary"
        )
        self.assertTrue(self.db.set_client_complimentary(10, 99, False))
        restored = self.db.get_client_access_state(10)
        self.assertEqual(restored.source, "paid")
        self.assertEqual(restored.cascade_expiry, paid_expiry)

    def test_invitation_is_one_time_and_requires_approval(self):
        invitation = self.db.create_client_invitation("@Alice_123", 99)
        duplicate = self.db.create_client_invitation("alice_123", 100)
        self.assertEqual(invitation["id"], duplicate["id"])
        expires_at = datetime.fromisoformat(str(invitation["expires_at"])).replace(
            tzinfo=UTC
        )
        self.assertGreater((expires_at - datetime.now(UTC)).total_seconds(), 6 * 86400)
        self.assertLess((expires_at - datetime.now(UTC)).total_seconds(), 8 * 86400)

        self.assertTrue(self.db.set_invitation_promo(invitation["id"], 99, 25))
        self.assertTrue(
            self.db.set_invitation_complimentary(invitation["id"], 99, True)
        )
        claimed = self.db.claim_client_invitation(
            invitation["token"], 777, "Actual_User"
        )
        self.assertIsNotNone(claimed)
        self.assertIsNone(
            self.db.claim_client_invitation(invitation["token"], 778, "forwarded")
        )
        self.assertIsNone(self.db.get_admin_client_details(777))

        client = self.db.approve_client_invitation(invitation["id"], 99)
        self.assertEqual(client["telegram_user_id"], 777)
        self.assertEqual(client["telegram_username"], "Actual_User")
        self.assertEqual(client["promo"], 25)
        self.assertEqual(client["is_complimentary"], 1)
        self.assertEqual(client["payment_status"], "unpaid")
        self.assertEqual(self.db.get_client_access_state(777).source, "complimentary")
        self.assertNotIn(
            invitation["id"],
            {item["id"] for item in self.db.list_client_invitations()},
        )

    def test_expired_or_rejected_invitation_can_be_reissued(self):
        invitation = self.db.create_client_invitation("expired_user", 99, ttl_days=-1)
        self.assertEqual(
            self.db.get_client_invitation(invitation["id"])["display_status"],
            "expired",
        )
        self.assertIsNone(
            self.db.claim_client_invitation(invitation["token"], 10, "expired_user")
        )

        reissued = self.db.reissue_client_invitation(invitation["id"], 99)
        self.assertNotEqual(reissued["token"], invitation["token"])
        self.assertIsNotNone(
            self.db.claim_client_invitation(reissued["token"], 10, "expired_user")
        )
        self.assertTrue(self.db.reject_client_invitation(invitation["id"], 99))
        rejected = self.db.get_client_invitation(invitation["id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertIsNotNone(self.db.reissue_client_invitation(invitation["id"], 99))

    def test_complimentary_clients_are_excluded_from_expiry_notifications(self):
        expiry = (datetime.now(UTC) + timedelta(days=3)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.db.ensure_subscription(
            10, "alice", expiry, "paid", "30_days", "stars"
        )
        self.db.set_client_complimentary(10, 99, True)

        recipients = self.db.get_users_for_notification(days_before=3)
        self.assertNotIn(10, {item["telegram_user_id"] for item in recipients})

    def test_paid_expiry_sweep_does_not_disable_free_configs(self):
        self.db.ensure_subscription(
            10, "alice", "2000-01-01 00:00:00", "paid", "30_days", "stars"
        )
        self.db.save_client_peer(
            10,
            "server-a",
            "if-production",
            "peer-10",
            "public-key-10",
            "alice",
            "primary",
            enabled=True,
        )
        self.db.set_client_complimentary(10, 99, True)

        self.db.sync_expired_access_statuses()

        self.assertEqual(self.db.get_client_access_state(10).source, "complimentary")
        self.assertEqual(self.db.get_primary_client_peer(10)["enabled"], 1)

    def test_complimentary_menu_hides_purchase_and_extension(self):
        import bot as bot_module

        self.db.admin_add_client(10, 99)
        self.db.set_client_complimentary(10, 99, True)
        with patch.object(bot_module, "db", self.db, create=True):
            keyboard = bot_module.create_main_menu_keyboard(10)

        labels = {
            button.text for row in keyboard.inline_keyboard for button in row
        }
        self.assertIn("🎁 Бесплатный доступ", labels)
        self.assertIn("📥 Получить конфигурацию", labels)
        self.assertNotIn("💳 Купить доступ", labels)
        self.assertNotIn("🔄 Продлить подписку", labels)


class RecordingCascadeAPI:
    def __init__(self):
        self.created_expiries = []
        self.updated_expiries = []
        self.enabled = []
        self.disabled = []

    async def list_peers(self):
        return []

    async def create_peer(self, name, expired_at, interface_id=None):
        self.created_expiries.append((expired_at, interface_id))
        return {
            "id": "peer-10",
            "name": name,
            "publicKey": "public-key-10",
            "enabled": True,
        }

    async def download_config(self, peer_id, interface_id=None):
        return b"config"

    async def update_expiry(self, peer_id, expired_at, interface_id=None):
        self.updated_expiries.append((expired_at, interface_id))

    async def enable_peer(self, peer_id, interface_id=None):
        self.enabled.append((peer_id, interface_id))

    async def disable_peer(self, peer_id, interface_id=None):
        self.disabled.append((peer_id, interface_id))


class ComplimentaryCascadeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)
        self.db.ensure_subscription(
            10, "alice", "2030-01-01 00:00:00", "paid", "30_days", "stars"
        )
        self.router = CascadeRouter(self.db, servers=[])
        self.router.servers = [
            CascadeServer(
                "server-a", "https://a.test/admin", "token", "if-production", 1, 10
            )
        ]
        self.api = RecordingCascadeAPI()
        self.router.apis = {"server-a": self.api}

    async def asyncTearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    async def test_free_provisioning_and_revoke_use_effective_expiries(self):
        enabled = await self.router.set_client_complimentary(10, 99, True)
        self.assertEqual(enabled["created"], 1)
        self.assertEqual(
            self.api.created_expiries,
            [(COMPLIMENTARY_CASCADE_EXPIRY, "if-production")],
        )

        disabled = await self.router.set_client_complimentary(10, 99, False)
        self.assertEqual(disabled["failed"], 0)
        self.assertEqual(
            self.api.updated_expiries[-1], ("2030-01-01 00:00:00", "if-production")
        )
        self.assertEqual(self.api.enabled[-1], ("peer-10", "if-production"))

        with sqlite3.connect(self.path) as connection:
            operations = {
                row[0]
                for row in connection.execute(
                    "SELECT operation FROM operation_logs WHERE operation LIKE '%complimentary_sync'"
                )
            }
        self.assertEqual(
            operations,
            {
                "admin_enable_complimentary_sync",
                "admin_disable_complimentary_sync",
            },
        )

    async def test_old_payment_callback_is_rejected_for_free_client(self):
        self.db.set_client_complimentary(10, 99, True)
        answer = AsyncMock()
        payment_manager = SimpleNamespace(
            is_tariff_enabled=lambda _tariff: True,
            send_stars_payment_request=AsyncMock(return_value=True),
        )
        cascade_router = SimpleNamespace(ensure_reservation=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=10, username="alice"),
            message=SimpleNamespace(chat=SimpleNamespace(id=10)),
        )

        await handle_pay_stars_callback(
            callback,
            payment_manager,
            cascade_router,
            answer,
            AsyncMock(),
            lambda: None,
            UserActionLocks(),
            SimpleNamespace(telegram_event=lambda _name: None),
            PaymentMethodCallback(
                method=PaymentMethod.STARS,
                tariff="14_days",
                user_id=10,
            ),
            db=self.db,
        )

        answer.assert_awaited_once_with(callback, "🎁 Бесплатный доступ уже активен")
        cascade_router.ensure_reservation.assert_not_awaited()
        payment_manager.send_stars_payment_request.assert_not_awaited()

    async def test_stale_free_provisioning_task_does_not_restore_access(self):
        self.db.admin_add_client(20, 99)
        self.db.set_client_complimentary(20, 99, True)
        self.db.add_provisioning_task(
            20,
            "create_peer",
            {
                "username": "pending",
                "peer_name": "pending",
                "expire_date": COMPLIMENTARY_CASCADE_EXPIRY,
            },
            "offline",
        )
        worker_router = AsyncMock()
        worker = ProvisioningWorker(
            self.db,
            worker_router,
            AsyncMock(),
            AsyncMock(),
            interval_seconds=60,
            lease_seconds=30,
        )
        tasks = self.db.claim_provisioning_tasks(worker.worker_id, 30)
        self.db.set_client_complimentary(20, 99, False)

        await worker._process(tasks[0])

        worker_router.create_user_peer.assert_not_awaited()
        self.assertFalse(self.db.get_client_access_state(20).active)
        with sqlite3.connect(self.path) as connection:
            status = connection.execute(
                "SELECT status FROM provisioning_tasks WHERE telegram_user_id=20"
            ).fetchone()[0]
        self.assertEqual(status, "completed")
