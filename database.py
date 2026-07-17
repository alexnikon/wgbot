import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from config import DATABASE_FILE


logger = logging.getLogger(__name__)


class Database:
    """SQLite persistence for clients, subscriptions, Cascade peers, and payments."""

    def __init__(self, db_file: str = DATABASE_FILE):
        self.db_file = db_file
        self.connection_timeout = 30.0
        self.busy_timeout_ms = 30000
        self.init_database()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, timeout=self.connection_timeout)
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_database(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    telegram_user_id INTEGER PRIMARY KEY,
                    telegram_username TEXT NOT NULL DEFAULT '',
                    promo INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    telegram_user_id INTEGER PRIMARY KEY REFERENCES clients(telegram_user_id) ON DELETE CASCADE,
                    expire_date TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    payment_status TEXT NOT NULL DEFAULT 'unpaid',
                    stars_paid INTEGER NOT NULL DEFAULT 0,
                    rub_paid INTEGER NOT NULL DEFAULT 0,
                    last_payment_date TEXT,
                    tariff_key TEXT,
                    payment_method TEXT,
                    notification_sent INTEGER NOT NULL DEFAULT 0,
                    hour_notification_sent INTEGER NOT NULL DEFAULT 0,
                    expired_notification_sent INTEGER NOT NULL DEFAULT 0,
                    legacy_peer_name TEXT,
                    legacy_public_key TEXT
                );

                CREATE TABLE IF NOT EXISTS client_peers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL REFERENCES clients(telegram_user_id) ON DELETE CASCADE,
                    server_key TEXT,
                    interface_id TEXT,
                    cascade_peer_id TEXT,
                    public_key TEXT NOT NULL DEFAULT '',
                    peer_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'manual',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(server_key, interface_id, cascade_peer_id),
                    UNIQUE(telegram_user_id, public_key)
                );

                CREATE TABLE IF NOT EXISTS server_reservations (
                    telegram_user_id INTEGER PRIMARY KEY,
                    server_key TEXT NOT NULL,
                    interface_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS provisioning_tasks (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_name TEXT,
                    operation TEXT,
                    details TEXT,
                    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    currency TEXT DEFAULT 'RUB',
                    status TEXT DEFAULT 'pending',
                    payment_method TEXT,
                    tariff_key TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_subscriptions_expiry
                    ON subscriptions(is_active, payment_status, expire_date);
                CREATE INDEX IF NOT EXISTS idx_client_peers_user_role
                    ON client_peers(telegram_user_id, role);
                CREATE INDEX IF NOT EXISTS idx_client_peers_public_key
                    ON client_peers(public_key);
                CREATE INDEX IF NOT EXISTS idx_reservations_server_expiry
                    ON server_reservations(server_key, expires_at);
                CREATE INDEX IF NOT EXISTS idx_provisioning_pending
                    ON provisioning_tasks(status, next_attempt_at);
                """
            )
            self._migrate_legacy_peers(conn)
            conn.commit()
        logger.info("Cascade database schema initialized")

    def _migrate_legacy_peers(self, conn: sqlite3.Connection) -> None:
        """Copy legacy business data without mutating the old table."""
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='peers'"
        ).fetchone()
        if not table:
            return
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM peers WHERE telegram_user_id IS NOT NULL ORDER BY id"
        ).fetchall()
        for row in rows:
            item = dict(row)
            user_id = int(item["telegram_user_id"])
            conn.execute(
                """
                INSERT INTO clients(telegram_user_id, telegram_username, created_at, updated_at)
                VALUES (?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_username=CASE
                        WHEN clients.telegram_username='' THEN excluded.telegram_username
                        ELSE clients.telegram_username END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, item.get("telegram_username") or "", item.get("created_at")),
            )
            conn.execute(
                """
                INSERT INTO subscriptions(
                    telegram_user_id, expire_date, is_active, payment_status,
                    stars_paid, rub_paid, last_payment_date, tariff_key, payment_method,
                    notification_sent, hour_notification_sent, expired_notification_sent,
                    legacy_peer_name, legacy_public_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO NOTHING
                """,
                (
                    user_id,
                    item.get("expire_date"),
                    int(bool(item.get("is_active", 1))),
                    item.get("payment_status") or "unpaid",
                    int(item.get("stars_paid") or 0),
                    int(item.get("rub_paid") or 0),
                    item.get("last_payment_date"),
                    item.get("tariff_key"),
                    item.get("payment_method"),
                    int(bool(item.get("notification_sent", 0))),
                    int(bool(item.get("hour_notification_sent", 0))),
                    int(bool(item.get("expired_notification_sent", 0))),
                    item.get("peer_name"),
                    item.get("peer_id"),
                ),
            )

    def upsert_client(self, user_id: int, username: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO clients(telegram_user_id, telegram_username)
                VALUES (?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_username=CASE
                        WHEN excluded.telegram_username != '' THEN excluded.telegram_username
                        ELSE clients.telegram_username END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, (username or "").strip().lstrip("@")),
            )
            conn.commit()

    def ensure_subscription(
        self,
        user_id: int,
        username: str | None = None,
        expire_date: str | None = None,
        payment_status: str = "unpaid",
        tariff_key: str | None = None,
        payment_method: str | None = None,
    ) -> None:
        self.upsert_client(user_id, username)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subscriptions(
                    telegram_user_id, expire_date, payment_status, tariff_key, payment_method
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    expire_date=COALESCE(excluded.expire_date, subscriptions.expire_date),
                    payment_status=excluded.payment_status,
                    tariff_key=COALESCE(excluded.tariff_key, subscriptions.tariff_key),
                    payment_method=COALESCE(excluded.payment_method, subscriptions.payment_method)
                """,
                (user_id, expire_date, payment_status, tariff_key, payment_method),
            )
            conn.commit()

    def save_client_peer(
        self,
        user_id: int,
        server_key: str,
        interface_id: str,
        cascade_peer_id: str,
        public_key: str,
        peer_name: str,
        role: str = "primary",
        enabled: bool = True,
    ) -> bool:
        self.upsert_client(user_id)
        try:
            with self._connect() as conn:
                other_assignment = conn.execute(
                    """
                    SELECT server_key, interface_id FROM client_peers
                    WHERE telegram_user_id=? AND server_key IS NOT NULL
                      AND (server_key != ? OR interface_id != ?) LIMIT 1
                    """,
                    (user_id, server_key, interface_id),
                ).fetchone()
                if other_assignment:
                    logger.error(
                        "User %s is already assigned to Cascade server %s interface %s",
                        user_id,
                        other_assignment[0],
                        other_assignment[1],
                    )
                    return False
                if role == "primary":
                    conn.execute(
                        "DELETE FROM client_peers WHERE telegram_user_id=? AND role='primary'",
                        (user_id,),
                    )
                conn.execute(
                    """
                    INSERT INTO client_peers(
                        telegram_user_id, server_key, interface_id, cascade_peer_id,
                        public_key, peer_name, role, enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id, public_key) DO UPDATE SET
                        server_key=excluded.server_key,
                        interface_id=excluded.interface_id,
                        cascade_peer_id=excluded.cascade_peer_id,
                        peer_name=excluded.peer_name,
                        role=excluded.role,
                        enabled=excluded.enabled,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        user_id,
                        server_key,
                        interface_id,
                        cascade_peer_id,
                        public_key,
                        peer_name,
                        role,
                        int(enabled),
                    ),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError as exc:
            logger.error("Failed to save Cascade peer for user %s: %s", user_id, exc)
            return False

    def get_client_peers(self, user_id: int, bound_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM client_peers WHERE telegram_user_id=?"
        if bound_only:
            sql += " AND server_key IS NOT NULL AND interface_id IS NOT NULL AND cascade_peer_id IS NOT NULL"
        sql += " ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END, id"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, (user_id,)).fetchall()]

    def get_primary_client_peer(self, user_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM client_peers
                WHERE telegram_user_id=? AND role='primary'
                  AND server_key IS NOT NULL AND interface_id IS NOT NULL
                  AND cascade_peer_id IS NOT NULL
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_client_peer_enabled(self, cascade_peer_id: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE client_peers SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE cascade_peer_id=?",
                (int(enabled), cascade_peer_id),
            )
            conn.commit()

    def get_peer_by_telegram_id(self, telegram_user_id: int) -> Optional[dict[str, Any]]:
        """Return a compatibility view consumed by existing bot UI handlers."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT c.telegram_user_id, c.telegram_username, c.promo,
                       s.*, cp.peer_name, cp.public_key, cp.cascade_peer_id,
                       cp.server_key, cp.interface_id, cp.role, cp.enabled
                FROM clients c
                LEFT JOIN subscriptions s USING(telegram_user_id)
                LEFT JOIN client_peers cp
                  ON cp.telegram_user_id=c.telegram_user_id AND cp.role='primary'
                WHERE c.telegram_user_id=?
                LIMIT 1
                """,
                (telegram_user_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["peer_id"] = result.get("cascade_peer_id")
        result["is_active"] = int(result.get("is_active") or 0)
        return result

    def get_peer_count(self, user_id: int) -> int:
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM client_peers WHERE telegram_user_id=?",
                    (user_id,),
                ).fetchone()[0]
            )

    def get_client_telegram_ids(self) -> list[int]:
        with self._connect() as conn:
            return [int(row[0]) for row in conn.execute("SELECT telegram_user_id FROM clients ORDER BY telegram_user_id")]

    def get_admin_client_options(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT telegram_user_id, telegram_username FROM clients ORDER BY lower(telegram_username), telegram_user_id"
            ).fetchall()
        return [{"telegramId": int(row[0]), "username": row[1] or ""} for row in rows]

    def set_client_promo(self, user_id: int, promo: int) -> bool:
        self.upsert_client(user_id)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE clients SET promo=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_user_id=?",
                (promo, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_user_promo_factor(self, user_id: int) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT promo FROM clients WHERE telegram_user_id=?", (user_id,)).fetchone()
        value = int(row[0] or 0) if row else 0
        if value <= 0:
            return 1.0
        return 1.0 - value / 100.0 if value <= 100 else value / 100.0

    def create_reservation(
        self, user_id: int, server_key: str, interface_id: str, minutes: int
    ) -> None:
        expires_at = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO server_reservations(telegram_user_id, server_key, interface_id, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    server_key=excluded.server_key,
                    interface_id=excluded.interface_id,
                    expires_at=excluded.expires_at,
                    created_at=CURRENT_TIMESTAMP
                """,
                (user_id, server_key, interface_id, expires_at),
            )
            conn.commit()

    def get_active_reservation(self, user_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM server_reservations WHERE telegram_user_id=? AND expires_at > datetime('now')",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def count_active_reservations(self, server_key: str) -> int:
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM server_reservations WHERE server_key=? AND expires_at > datetime('now')",
                    (server_key,),
                ).fetchone()[0]
            )

    def release_reservation(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM server_reservations WHERE telegram_user_id=?", (user_id,))
            conn.commit()

    def cleanup_expired_reservations(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM server_reservations WHERE expires_at <= datetime('now')")
            conn.commit()
            return cursor.rowcount

    def add_provisioning_task(
        self, user_id: int, operation: str, payload: dict[str, Any], error: str
    ) -> str:
        encoded_payload = json.dumps(payload, sort_keys=True)
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM provisioning_tasks
                WHERE telegram_user_id=? AND operation=? AND status='pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, operation),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE provisioning_tasks SET payload=?, last_error=?,
                        next_attempt_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (encoded_payload, error[:1000], existing[0]),
                )
                conn.commit()
                return str(existing[0])
            task_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO provisioning_tasks(id, telegram_user_id, operation, payload, last_error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, user_id, operation, encoded_payload, error[:1000]),
            )
            conn.commit()
        return task_id

    def get_pending_provisioning_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM provisioning_tasks
                WHERE status='pending' AND next_attempt_at <= datetime('now')
                ORDER BY created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def complete_provisioning_task(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE provisioning_tasks SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (task_id,),
            )
            conn.commit()

    def fail_provisioning_task(self, task_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE provisioning_tasks
                SET attempts=attempts+1, last_error=?,
                    next_attempt_at=datetime('now', '+' || MIN(3600, 60 * (attempts + 1)) || ' seconds'),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (error[:1000], task_id),
            )
            conn.commit()

    def update_payment_status(
        self,
        telegram_user_id: int,
        payment_status: str,
        amount_paid: int = 0,
        payment_method: str | None = None,
        tariff_key: str | None = None,
    ) -> bool:
        self.ensure_subscription(
            telegram_user_id,
            payment_status=payment_status,
            tariff_key=tariff_key,
            payment_method=payment_method,
        )
        field = "rub_paid" if payment_method == "yookassa" else "stars_paid"
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE subscriptions SET payment_status=?, {field}=?,
                    last_payment_date=CURRENT_TIMESTAMP,
                    payment_method=COALESCE(?, payment_method),
                    tariff_key=COALESCE(?, tariff_key)
                WHERE telegram_user_id=?
                """,
                (payment_status, amount_paid, payment_method, tariff_key, telegram_user_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def extend_access(self, telegram_user_id: int, days: int = 30) -> tuple[bool, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT expire_date FROM subscriptions WHERE telegram_user_id=?",
                (telegram_user_id,),
            ).fetchone()
            if not row:
                return False, ""
            try:
                current = datetime.fromisoformat(row[0]) if row[0] else datetime.now()
            except ValueError:
                current = datetime.now()
            new_expiry = max(current, datetime.now()) + timedelta(days=days)
            value = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.execute(
                """
                UPDATE subscriptions SET expire_date=?, is_active=1, payment_status='paid',
                    notification_sent=0, hour_notification_sent=0,
                    expired_notification_sent=0 WHERE telegram_user_id=?
                """,
                (value, telegram_user_id),
            )
            conn.commit()
            return cursor.rowcount > 0, value

    def activate_new_access(
        self,
        user_id: int,
        username: str | None,
        days: int,
        tariff_key: str,
        payment_method: str,
    ) -> str:
        expiry = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        self.ensure_subscription(
            user_id,
            username,
            expiry,
            "paid",
            tariff_key,
            payment_method,
        )
        return expiry

    def apply_refund(self, payment_id: str, days: int) -> tuple[int, str] | None:
        """Atomically mark a payment refunded and reduce its subscription."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            payment = conn.execute(
                "SELECT user_id FROM payments WHERE payment_id=? AND status='succeeded'",
                (payment_id,),
            ).fetchone()
            if not payment:
                conn.rollback()
                return None
            user_id = int(payment[0])
            subscription = conn.execute(
                "SELECT expire_date FROM subscriptions WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
            if not subscription or not subscription[0]:
                conn.rollback()
                return None
            new_expiry = datetime.fromisoformat(subscription[0]) - timedelta(days=days)
            value = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                UPDATE subscriptions SET expire_date=?, notification_sent=0,
                    hour_notification_sent=0, expired_notification_sent=0
                WHERE telegram_user_id=?
                """,
                (value, user_id),
            )
            conn.execute(
                "UPDATE payments SET status='refunded', updated_at=CURRENT_TIMESTAMP WHERE payment_id=?",
                (payment_id,),
            )
            conn.commit()
            return user_id, value

    def get_expired_peers(self) -> list[dict[str, Any]]:
        return self._subscription_query(
            "s.is_active=1 AND s.expire_date < datetime('now') AND s.expired_notification_sent=0"
        )

    def sync_expired_access_statuses(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE subscriptions SET payment_status='expired'
                WHERE is_active=1 AND payment_status='paid'
                  AND expire_date IS NOT NULL AND expire_date < datetime('now')
                """
            )
            conn.commit()
            return cursor.rowcount

    def get_users_for_notification(self, days_before: int = 3) -> list[dict[str, Any]]:
        return self._subscription_query(
            f"s.is_active=1 AND s.payment_status='paid' AND s.notification_sent=0 "
            f"AND s.expire_date <= datetime('now', '+{int(days_before)} days') "
            "AND s.expire_date > datetime('now', '+1 hour')"
        )

    def get_users_for_hour_notification(self) -> list[dict[str, Any]]:
        return self._subscription_query(
            "s.is_active=1 AND s.payment_status='paid' AND s.hour_notification_sent=0 "
            "AND s.expire_date <= datetime('now', '+1 hour') AND s.expire_date > datetime('now')"
        )

    def _subscription_query(self, where: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT c.telegram_user_id, c.telegram_username, s.*
                FROM subscriptions s JOIN clients c USING(telegram_user_id)
                WHERE {where} ORDER BY s.expire_date
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def _mark_notification(self, user_id: int, column: str) -> bool:
        if column not in {"notification_sent", "hour_notification_sent", "expired_notification_sent"}:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE subscriptions SET {column}=1 WHERE telegram_user_id=?",
                (user_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def mark_notification_sent(self, user_id: int) -> bool:
        return self._mark_notification(user_id, "notification_sent")

    def mark_hour_notification_sent(self, user_id: int) -> bool:
        return self._mark_notification(user_id, "hour_notification_sent")

    def mark_expired_notification_sent(self, user_id: int) -> bool:
        return self._mark_notification(user_id, "expired_notification_sent")

    def add_payment(
        self,
        payment_id: str,
        user_id: int,
        amount: int,
        payment_method: str,
        tariff_key: str,
        metadata: dict | None = None,
    ) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO payments(payment_id, user_id, amount, payment_method, tariff_key, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (payment_id, user_id, amount, payment_method, tariff_key, json.dumps(metadata or {})),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_payment_status_by_id(self, payment_id: str, status: str) -> bool:
        if status not in {"pending", "succeeded", "canceled", "refunded"}:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE payments SET status=?, updated_at=CURRENT_TIMESTAMP WHERE payment_id=?",
                (status, payment_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def claim_payment_success(self, payment_id: str) -> bool:
        """Atomically claim a successful payment event exactly once."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE payments SET status='succeeded', updated_at=CURRENT_TIMESTAMP
                WHERE payment_id=? AND status='pending'
                """,
                (payment_id,),
            )
            conn.commit()
            return cursor.rowcount == 1

    def get_payment_by_id(self, payment_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
            return dict(row) if row else None

    def get_legacy_migration_candidates(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT c.telegram_user_id, c.telegram_username,
                       s.legacy_peer_name, s.legacy_public_key
                FROM clients c JOIN subscriptions s USING(telegram_user_id)
                WHERE s.legacy_public_key IS NOT NULL AND s.legacy_public_key != ''
                ORDER BY c.telegram_user_id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def import_unbound_peer(
        self, user_id: int, public_key: str, peer_name: str, role: str = "manual"
    ) -> bool:
        self.upsert_client(user_id)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO client_peers(telegram_user_id, public_key, peer_name, role)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id, public_key) DO UPDATE SET
                        peer_name=excluded.peer_name, role=excluded.role,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (user_id, public_key, peer_name, role),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def log_operation(self, peer_name: str, operation: str, details: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (peer_name, operation, details),
            )
            conn.commit()
