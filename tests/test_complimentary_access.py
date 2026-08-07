import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot as bot_module
from callbacks import PaymentMethod, PaymentMethodCallback
from cascade_api import CascadeRouter, CascadeServer
from database import COMPLIMENTARY_CASCADE_EXPIRY, Database
from handlers.navigation import cmd_start
from handlers.payments import handle_pay_stars_callback
from provisioning import ProvisioningWorker
from telegram_runtime import TelegramSender, UserActionLocks


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
        self.assertEqual(existing["identity_verified"], 0)
        self.assertFalse(self.db.get_client_access_state(123456).active)
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

    def test_invitation_conflict_is_one_time_and_requires_approval(self):
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
        self.assertEqual(claimed.status, "conflict")
        self.assertEqual(claimed.conflict_reason, "username_mismatch")
        self.assertEqual(
            self.db.claim_client_invitation(
                invitation["token"], 778, "forwarded"
            ).status,
            "invalid",
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

    def test_exact_username_invitation_is_automatically_approved(self):
        invitation = self.db.create_client_invitation("Alice_123", 99)
        self.db.set_invitation_promo(invitation["id"], 99, 30)
        self.db.set_invitation_complimentary(invitation["id"], 99, True)

        claimed = self.db.claim_client_invitation(
            invitation["token"], 777, "aLiCe_123"
        )

        self.assertEqual(claimed.status, "auto_approved")
        self.assertEqual(claimed.client["telegram_user_id"], 777)
        self.assertEqual(claimed.client["identity_source"], "username_invite")
        self.assertEqual(claimed.client["promo"], 30)
        self.assertEqual(claimed.client["is_complimentary"], 1)
        self.assertEqual(
            self.db.claim_client_invitation(invitation["token"], 777, "Alice_123").status,
            "invalid",
        )

    def test_invitation_merge_only_improves_existing_benefits(self):
        paid_expiry = "2030-01-01 00:00:00"
        self.db.ensure_subscription(
            777, "alice", paid_expiry, "paid", "30_days", "stars"
        )
        self.db.set_client_promo(777, 40)
        self.db.set_client_complimentary(777, 99, True)
        invitation = self.db.create_client_invitation("alice", 99)
        self.db.set_invitation_promo(invitation["id"], 99, 10)

        claimed = self.db.claim_client_invitation(invitation["token"], 777, "ALICE")

        self.assertEqual(claimed.status, "auto_approved")
        client = self.db.get_admin_client_details(777)
        self.assertEqual(client["promo"], 40)
        self.assertEqual(client["is_complimentary"], 1)
        self.assertEqual(client["expire_date"], paid_expiry)
        self.assertEqual(client["payment_status"], "paid")

    def test_ambiguous_invitation_claims_require_manual_review(self):
        missing = self.db.create_client_invitation("missing_name", 99)
        missing_claim = self.db.claim_client_invitation(missing["token"], 10, None)
        self.assertEqual(missing_claim.conflict_reason, "username_missing")

        self.db.ensure_subscription(20, "owned_name", None, "unpaid")
        owned = self.db.create_client_invitation("owned_name", 99)
        owned_claim = self.db.claim_client_invitation(
            owned["token"], 21, "OWNED_NAME"
        )
        self.assertEqual(
            owned_claim.conflict_reason, "username_owned_by_other_client"
        )

        self.db.ensure_subscription(30, "banned_name", None, "unpaid")
        self.db.set_client_ban(30, 99, True, "review")
        banned = self.db.create_client_invitation("banned_name", 99)
        banned_claim = self.db.claim_client_invitation(
            banned["token"], 30, "banned_name"
        )
        self.assertEqual(banned_claim.conflict_reason, "claimant_banned")

    def test_expired_or_rejected_invitation_can_be_reissued(self):
        invitation = self.db.create_client_invitation("expired_user", 99, ttl_days=-1)
        self.assertEqual(
            self.db.get_client_invitation(invitation["id"])["display_status"],
            "expired",
        )
        self.assertEqual(
            self.db.claim_client_invitation(
                invitation["token"], 10, "expired_user"
            ).status,
            "invalid",
        )

        reissued = self.db.reissue_client_invitation(invitation["id"], 99)
        self.assertNotEqual(reissued["token"], invitation["token"])
        self.assertEqual(
            self.db.claim_client_invitation(
                reissued["token"], 10, "different_user"
            ).status,
            "conflict",
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

    def test_unverified_clients_are_excluded_from_outgoing_recipients(self):
        self.db.admin_add_client(10, 99)
        self.assertNotIn(10, self.db.get_client_telegram_ids())

        self.db.verify_preadded_client(10, "alice")

        self.assertIn(10, self.db.get_client_telegram_ids())

    def test_numeric_id_verification_does_not_block_duplicate_username(self):
        self.db.ensure_subscription(10, "shared_name")
        self.db.admin_add_client(20, 99)
        self.db.set_client_complimentary(20, 99, True)

        verification = self.db.verify_preadded_client(20, "SHARED_NAME")

        self.assertEqual(verification["duplicate_username_ids"], [10])
        self.assertTrue(self.db.get_client_access_state(20).active)

    def test_identity_migration_keeps_existing_users_verified(self):
        legacy_handle, legacy_path = tempfile.mkstemp(suffix=".db")
        os.close(legacy_handle)
        self.addCleanup(
            lambda: [
                os.remove(legacy_path + suffix)
                for suffix in ("", "-wal", "-shm")
                if os.path.exists(legacy_path + suffix)
            ]
        )
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE clients (
                    telegram_user_id INTEGER PRIMARY KEY,
                    telegram_username TEXT NOT NULL DEFAULT '',
                    promo INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_name TEXT,
                    operation TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO clients(telegram_user_id, telegram_username)
                VALUES (1, 'existing'), (2, 'preadded');
                INSERT INTO operation_logs(peer_name, operation, details)
                VALUES ('telegram:2', 'admin_add_client', '{}');
                """
            )

        migrated = Database(legacy_path)

        self.assertTrue(migrated.get_client_access_state(1).identity_verified)
        self.assertFalse(migrated.get_client_access_state(2).identity_verified)

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
        self.db.verify_preadded_client(10, "alice")
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

    async def test_preadded_free_client_is_provisioned_only_after_first_contact(self):
        self.db.admin_add_client(20, 99)
        self.db.set_client_complimentary(20, 99, True)
        self.assertFalse(self.db.get_client_access_state(20).active)
        self.assertEqual(self.api.created_expiries, [])

        first, second = await asyncio.gather(
            self.router.activate_preadded_client(20, "pending_user"),
            self.router.activate_preadded_client(20, "pending_user"),
        )

        activations = [result for result in (first, second) if result is not None]
        self.assertEqual(len(activations), 1)
        self.assertEqual(activations[0]["sync"]["created"], 1)
        self.assertEqual(
            self.api.created_expiries,
            [(COMPLIMENTARY_CASCADE_EXPIRY, "if-production")],
        )
        access = self.db.get_client_access_state(20)
        self.assertTrue(access.identity_verified)
        self.assertEqual(access.source, "complimentary")

    async def test_preadded_paid_client_activates_with_paid_expiry(self):
        self.db.admin_add_client(20, 99)
        paid_expiry = "2030-01-01 00:00:00"
        self.db.ensure_subscription(
            20, "", paid_expiry, "paid", "30_days", "stars"
        )
        self.assertFalse(self.db.get_client_access_state(20).active)

        activation = await self.router.activate_preadded_client(20, "paid_user")

        self.assertEqual(activation["sync"]["failed"], 0)
        self.assertEqual(
            self.api.created_expiries,
            [(paid_expiry, "if-production")],
        )

    async def test_outbound_sender_skips_preadded_client_until_first_contact(self):
        self.db.admin_add_client(20, 99)
        sender = TelegramSender(SimpleNamespace(), self.db)
        operation = AsyncMock(return_value="sent")

        self.assertIsNone(await sender.call(20, operation))
        operation.assert_not_awaited()

        self.db.verify_preadded_client(20, "verified_user")
        self.assertEqual(await sender.call(20, operation), "sent")

    async def test_identity_middleware_queues_failed_activation(self):
        self.db.admin_add_client(20, 99)
        self.db.set_client_complimentary(20, 99, True)
        activation_router = SimpleNamespace(
            activate_preadded_client=AsyncMock(
                return_value={
                    "verification": {"duplicate_username_ids": []},
                    "sync": {
                        "total": 1,
                        "updated": 0,
                        "missing": 0,
                        "failed": 1,
                        "created": 0,
                    },
                    "error": "offline",
                }
            )
        )
        self.db.verify_preadded_client(20, "pending_user")
        handler = AsyncMock(return_value="handled")
        event = SimpleNamespace(from_user=SimpleNamespace(id=20, username="pending_user"))
        notify_admins = AsyncMock()
        with (
            patch.object(bot_module, "db", self.db, create=True),
            patch.object(
                bot_module, "cascade_router", activation_router, create=True
            ),
            patch.object(bot_module, "notify_admins", notify_admins, create=True),
        ):
            result = await bot_module.ClientIdentityMiddleware()(handler, event, {})

        self.assertEqual(result, "handled")
        with sqlite3.connect(self.path) as connection:
            task = connection.execute(
                "SELECT operation, status FROM provisioning_tasks WHERE telegram_user_id=20"
            ).fetchone()
        self.assertEqual(task, ("create_peer", "pending"))
        notify_admins.assert_awaited_once()

    async def test_start_link_auto_approves_exact_username_and_provisions(self):
        invitation = self.db.create_client_invitation("invite_user", 99)
        self.db.set_invitation_complimentary(invitation["id"], 99, True)
        panel = SimpleNamespace(
            delete_user_message=AsyncMock(), restore_or_create=AsyncMock()
        )
        notify_admins = AsyncMock()
        message = SimpleNamespace(
            text=f"/start claim_{invitation['token']}",
            from_user=SimpleNamespace(id=30, username="INVITE_USER"),
            chat=SimpleNamespace(id=30),
        )

        await cmd_start(
            message,
            self.db,
            lambda _user_id: SimpleNamespace(),
            panel,
            unittest.mock.Mock(),
            user_action_locks=UserActionLocks(),
            notify_admins=notify_admins,
            cascade_router=self.router,
        )

        client = self.db.get_admin_client_details(30)
        self.assertEqual(client["identity_source"], "username_invite")
        self.assertEqual(client["is_complimentary"], 1)
        self.assertIsNotNone(self.db.get_primary_client_peer(30))
        notify_admins.assert_awaited_once()
        self.assertIn(
            "автоматически привязан",
            notify_admins.await_args.args[0],
        )
