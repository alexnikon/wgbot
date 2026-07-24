import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from config import DATABASE_FILE

logger = logging.getLogger(__name__)
DEFAULT_PRIMARY_CONFIG_NAME = "Основной конфиг"
MAX_CONFIG_NAME_LENGTH = 48


def normalize_config_name(value: str) -> str:
    """Normalize and validate a user-facing configuration name."""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Configuration name contains control characters")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > MAX_CONFIG_NAME_LENGTH:
        raise ValueError(
            f"Configuration name must contain 1-{MAX_CONFIG_NAME_LENGTH} characters"
        )
    return normalized


class Database:
    """SQLite persistence for clients, subscriptions, Cascade peers, and payments."""

    def __init__(self, db_file: str = DATABASE_FILE):
        self.db_file = db_file
        self.connection_timeout = 30.0
        self.busy_timeout_ms = 30000
        self.init_database()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_file, timeout=self.connection_timeout)
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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
                    expired_notification_sent INTEGER NOT NULL DEFAULT 0
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
                    config_name TEXT,
                    admin_enabled INTEGER NOT NULL DEFAULT 1,
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
                    lease_owner TEXT,
                    lease_until TEXT,
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

                CREATE TABLE IF NOT EXISTS admin_workflows (
                    admin_id INTEGER NOT NULL,
                    workflow_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY(admin_id, workflow_type)
                );

                CREATE TABLE IF NOT EXISTS telegram_ui_panels (
                    telegram_user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS star_transactions (
                    transaction_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    transaction_type TEXT,
                    user_id INTEGER,
                    invoice_payload TEXT,
                    matched_payment_id TEXT,
                    status TEXT NOT NULL DEFAULT 'observed',
                    review_token TEXT,
                    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(transaction_id, direction)
                );

                CREATE TABLE IF NOT EXISTS star_reconciliation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    observed_count INTEGER NOT NULL DEFAULT 0,
                    applied_count INTEGER NOT NULL DEFAULT 0,
                    discrepancy_count INTEGER NOT NULL DEFAULT 0,
                    error_type TEXT
                );

                CREATE TABLE IF NOT EXISTS telegram_daily_metrics (
                    day TEXT PRIMARY KEY,
                    legacy_callbacks INTEGER NOT NULL DEFAULT 0,
                    unhandled_errors INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            self._ensure_column(conn, "provisioning_tasks", "lease_owner", "TEXT")
            self._ensure_column(conn, "provisioning_tasks", "lease_until", "TEXT")
            self._ensure_column(conn, "clients", "telegram_reachable", "INTEGER")
            self._ensure_column(conn, "clients", "telegram_blocked_at", "TEXT")
            self._ensure_column(conn, "clients", "last_telegram_error", "TEXT")
            self._ensure_column(
                conn, "clients", "telegram_reachability_updated_at", "TEXT"
            )
            self._ensure_column(conn, "client_peers", "config_name", "TEXT")
            self._ensure_column(
                conn, "client_peers", "admin_enabled", "INTEGER NOT NULL DEFAULT 1"
            )
            conn.execute(
                """
                UPDATE client_peers SET config_name=?
                WHERE role='primary' AND (config_name IS NULL OR trim(config_name)='')
                """,
                (DEFAULT_PRIMARY_CONFIG_NAME,),
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_client_peers_config_name
                ON client_peers(telegram_user_id, config_name COLLATE NOCASE)
                WHERE config_name IS NOT NULL AND role IN ('primary', 'additional')
                """
            )
            self._ensure_column(conn, "star_transactions", "review_token", "TEXT")
            conn.execute(
                """
                UPDATE star_transactions SET review_token=lower(hex(randomblob(8)))
                WHERE review_token IS NULL
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_star_transactions_review_token
                ON star_transactions(review_token) WHERE review_token IS NOT NULL
                """
            )
            for column, definition in {
                "telegram_payment_charge_id": "TEXT",
                "provider_payment_charge_id": "TEXT",
                "invoice_payload": "TEXT",
                "is_recurring": "INTEGER NOT NULL DEFAULT 0",
                "is_first_recurring": "INTEGER NOT NULL DEFAULT 0",
                "subscription_expiration_date": "INTEGER",
                "access_days": "INTEGER",
                "applied_from": "TEXT",
                "applied_until": "TEXT",
                "refunded_amount": "INTEGER NOT NULL DEFAULT 0",
                "refunded_at": "TEXT",
                "refund_review_status": "TEXT",
                "invoice_message_id": "INTEGER",
            }.items():
                self._ensure_column(conn, "payments", column, definition)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provisioning_claim
                ON provisioning_tasks(status, next_attempt_at, lease_until)
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_telegram_charge
                ON payments(telegram_payment_charge_id)
                WHERE telegram_payment_charge_id IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider_charge
                ON payments(provider_payment_charge_id)
                WHERE provider_payment_charge_id IS NOT NULL
                """
            )
            conn.commit()
        logger.info("Cascade database schema initialized")

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def mark_telegram_reachable(self, user_id: int) -> None:
        self.upsert_client(user_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE clients SET telegram_reachable=1, telegram_blocked_at=NULL,
                    last_telegram_error=NULL,
                    telegram_reachability_updated_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE telegram_user_id=?
                """,
                (user_id,),
            )
            conn.commit()

    def mark_telegram_unreachable(self, user_id: int, error_type: str) -> None:
        self.upsert_client(user_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE clients SET telegram_reachable=0,
                    telegram_blocked_at=COALESCE(telegram_blocked_at, CURRENT_TIMESTAMP),
                    last_telegram_error=?,
                    telegram_reachability_updated_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE telegram_user_id=?
                """,
                (error_type[:100], user_id),
            )
            conn.commit()

    def get_telegram_ui_panel(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM telegram_ui_panels WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_telegram_ui_panel(self, user_id: int, chat_id: int, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_ui_panels(telegram_user_id, chat_id, message_id)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    message_id=excluded.message_id,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, chat_id, message_id),
            )
            conn.commit()

    def delete_telegram_ui_panel(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM telegram_ui_panels WHERE telegram_user_id=?", (user_id,)
            )
            conn.commit()

    def set_admin_workflow(
        self,
        admin_id: int,
        workflow_type: str,
        state: str,
        data: dict[str, Any],
        ttl_hours: int = 24,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admin_workflows(
                    admin_id, workflow_type, state, data, expires_at
                ) VALUES (?, ?, ?, ?, datetime('now', ?))
                ON CONFLICT(admin_id, workflow_type) DO UPDATE SET
                    state=excluded.state, data=excluded.data,
                    updated_at=CURRENT_TIMESTAMP, expires_at=excluded.expires_at
                """,
                (
                    admin_id,
                    workflow_type,
                    state,
                    json.dumps(data, sort_keys=True),
                    f"+{int(ttl_hours)} hours",
                ),
            )
            conn.commit()

    def get_admin_workflow(
        self, admin_id: int, workflow_type: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "DELETE FROM admin_workflows WHERE expires_at <= datetime('now')"
            )
            row = conn.execute(
                """
                SELECT * FROM admin_workflows
                WHERE admin_id=? AND workflow_type=?
                """,
                (admin_id, workflow_type),
            ).fetchone()
            conn.commit()
        if not row:
            return None
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    def delete_admin_workflow(self, admin_id: int, workflow_type: str | None = None) -> int:
        with self._connect() as conn:
            if workflow_type is None:
                cursor = conn.execute(
                    "DELETE FROM admin_workflows WHERE admin_id=?", (admin_id,)
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM admin_workflows WHERE admin_id=? AND workflow_type=?",
                    (admin_id, workflow_type),
                )
            conn.commit()
            return cursor.rowcount

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
        config_name: str | None = None,
        admin_enabled: bool = True,
    ) -> bool:
        self.upsert_client(user_id)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if role != "additional":
                    other_assignment = conn.execute(
                        """
                        SELECT server_key, interface_id FROM client_peers
                        WHERE telegram_user_id=? AND server_key IS NOT NULL
                          AND role != 'additional'
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
                    current = conn.execute(
                        """
                        SELECT config_name FROM client_peers
                        WHERE telegram_user_id=? AND role='primary' LIMIT 1
                        """,
                        (user_id,),
                    ).fetchone()
                    config_name = (
                        config_name
                        or (current[0] if current and current[0] else None)
                        or DEFAULT_PRIMARY_CONFIG_NAME
                    )
                if config_name is not None:
                    config_name = normalize_config_name(config_name)
                    existing_names = conn.execute(
                        """
                        SELECT config_name, role FROM client_peers
                        WHERE telegram_user_id=? AND role IN ('primary', 'additional')
                          AND config_name IS NOT NULL
                          AND public_key != ?
                        """,
                        (user_id, public_key),
                    ).fetchall()
                    if any(
                        str(row[0]).casefold() == config_name.casefold()
                        and not (role == "primary" and row[1] == "primary")
                        for row in existing_names
                    ):
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
                        public_key, peer_name, role, enabled, config_name, admin_enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id, public_key) DO UPDATE SET
                        server_key=excluded.server_key,
                        interface_id=excluded.interface_id,
                        cascade_peer_id=excluded.cascade_peer_id,
                        peer_name=excluded.peer_name,
                        role=excluded.role,
                        enabled=excluded.enabled,
                        config_name=COALESCE(excluded.config_name, client_peers.config_name),
                        admin_enabled=excluded.admin_enabled,
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
                        config_name,
                        int(admin_enabled),
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

    def get_primary_client_peer(self, user_id: int) -> dict[str, Any] | None:
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

    def get_client_peer(self, peer_id: int, user_id: int | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM client_peers WHERE id=?"
        params: tuple[Any, ...] = (peer_id,)
        if user_id is not None:
            sql += " AND telegram_user_id=?"
            params = (peer_id, user_id)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def get_client_peer_by_cascade_id(
        self, server_key: str, interface_id: str, cascade_peer_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM client_peers
                WHERE server_key=? AND interface_id=? AND cascade_peer_id=?
                LIMIT 1
                """,
                (server_key, interface_id, cascade_peer_id),
            ).fetchone()
            return dict(row) if row else None

    def get_managed_client_configs(
        self, user_id: int, *, available_only: bool = False
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT * FROM client_peers
            WHERE telegram_user_id=? AND role IN ('primary', 'additional')
              AND server_key IS NOT NULL AND interface_id IS NOT NULL
              AND cascade_peer_id IS NOT NULL
        """
        if available_only:
            sql += (
                " AND admin_enabled=1"
                " AND (role='primary' OR (role='additional' AND enabled=1))"
            )
        sql += " ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END, id"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, (user_id,)).fetchall()]

    def rename_managed_config(self, peer_id: int, user_id: int, name: str) -> bool:
        normalized = normalize_config_name(name)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing_names = conn.execute(
                    """
                    SELECT config_name FROM client_peers
                    WHERE telegram_user_id=? AND id != ?
                      AND role IN ('primary', 'additional')
                      AND config_name IS NOT NULL
                    """,
                    (user_id, peer_id),
                ).fetchall()
                if any(
                    str(row[0]).casefold() == normalized.casefold()
                    for row in existing_names
                ):
                    return False
                cursor = conn.execute(
                    """
                    UPDATE client_peers SET config_name=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND telegram_user_id=?
                      AND role IN ('primary', 'additional')
                    """,
                    (normalized, peer_id, user_id),
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.IntegrityError:
            return False

    def set_config_admin_enabled(
        self, peer_id: int, user_id: int, admin_enabled: bool
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE client_peers SET admin_enabled=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND telegram_user_id=? AND role='additional'
                """,
                (int(admin_enabled), peer_id, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_subscription_expiry(self, user_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT expire_date FROM subscriptions WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
            return str(row[0]) if row and row[0] else None

    def set_client_peer_enabled(self, cascade_peer_id: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE client_peers SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE cascade_peer_id=?",
                (int(enabled), cascade_peer_id),
            )
            conn.commit()

    def log_admin_config_change(
        self,
        admin_id: int,
        user_id: int,
        peer_id: int,
        operation: str,
        *,
        server_key: str | None = None,
    ) -> None:
        details = json.dumps(
            {
                "admin_id": admin_id,
                "client_id": user_id,
                "peer_id": peer_id,
                "server_key": server_key,
            },
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (f"telegram:{user_id}", operation, details),
            )
            conn.commit()

    def get_peer_by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
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
            return [
                int(row[0])
                for row in conn.execute(
                    """
                    SELECT telegram_user_id FROM clients
                    WHERE telegram_reachable IS NULL OR telegram_reachable=1
                    ORDER BY telegram_user_id
                    """
                )
            ]

    def get_admin_client_options(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT telegram_user_id, telegram_username FROM clients ORDER BY lower(telegram_username), telegram_user_id"
            ).fetchall()
        return [{"telegramId": int(row[0]), "username": row[1] or ""} for row in rows]

    def get_runtime_stats(self) -> dict[str, int | None]:
        """Return non-sensitive gauges for protected operational diagnostics."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM clients),
                    (SELECT COUNT(*) FROM subscriptions
                     WHERE is_active=1 AND payment_status='paid'),
                    (SELECT COUNT(*) FROM provisioning_tasks
                     WHERE status='pending'),
                    (SELECT COUNT(*) FROM provisioning_tasks
                     WHERE status='running'),
                    (SELECT COUNT(*) FROM provisioning_tasks
                     WHERE status='failed'),
                    (SELECT COUNT(*) FROM server_reservations
                     WHERE datetime(expires_at) > datetime('now')),
                    (SELECT COUNT(*) FROM clients WHERE telegram_reachable=1),
                    (SELECT COUNT(*) FROM clients WHERE telegram_reachable=0),
                    (SELECT COUNT(*) FROM clients WHERE telegram_reachable IS NULL),
                    (SELECT COUNT(*) FROM star_transactions WHERE status='discrepancy'),
                    (SELECT CAST(strftime('%s', 'now') - strftime('%s', completed_at) AS INTEGER)
                     FROM star_reconciliation_runs WHERE status='completed'
                     ORDER BY id DESC LIMIT 1),
                    (SELECT legacy_callbacks FROM telegram_daily_metrics
                     WHERE day=date('now'))
                """
            ).fetchone()
        return {
            "clients": int(row[0]),
            "active_subscriptions": int(row[1]),
            "provisioning_pending": int(row[2]),
            "provisioning_running": int(row[3]),
            "provisioning_failed": int(row[4]),
            "active_reservations": int(row[5]),
            "telegram_reachable": int(row[6]),
            "telegram_blocked": int(row[7]),
            "telegram_reachability_unknown": int(row[8]),
            "stars_discrepancies": int(row[9]),
            "stars_last_success_age_seconds": int(row[10])
            if row[10] is not None
            else None,
            "legacy_callbacks_today": int(row[11] or 0),
        }

    def record_telegram_daily_metric(self, name: str) -> None:
        """Persist a low-volume Telegram counter for rollout decisions."""
        if name not in {"legacy_callbacks", "unhandled_errors"}:
            raise ValueError("Unsupported Telegram metric")
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO telegram_daily_metrics(day, {name})
                VALUES (date('now'), 1)
                ON CONFLICT(day) DO UPDATE SET
                    {name}={name}+1, updated_at=CURRENT_TIMESTAMP
                """
            )
            conn.commit()

    def ensure_telegram_daily_metrics_day(self) -> None:
        """Create today's zero-valued row for a continuous rollout history."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO telegram_daily_metrics(day) VALUES (date('now'))"
            )
            conn.commit()

    def get_legacy_callback_zero_streak(self, maximum_days: int = 30) -> int:
        history = {
            item["day"]: int(item["legacy_callbacks"])
            for item in self.get_telegram_daily_metrics(maximum_days)
        }
        streak = 0
        current = datetime.now(UTC).date()
        for offset in range(maximum_days):
            day = (current - timedelta(days=offset)).isoformat()
            if history.get(day) != 0:
                break
            streak += 1
        return streak

    def get_telegram_daily_metrics(self, days: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT day, legacy_callbacks, unhandled_errors
                FROM telegram_daily_metrics
                WHERE day >= date('now', ?)
                ORDER BY day DESC
                """,
                (f"-{max(1, min(int(days), 365)) - 1} days",),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_admin_clients_page(
        self, page: int, page_size: int, query: str = ""
    ) -> tuple[list[dict[str, Any]], int]:
        """Return a filtered admin client page with subscription and server data."""
        page = max(0, int(page))
        page_size = max(1, min(int(page_size), 50))
        normalized = query.strip().lstrip("@").lower()
        where = ""
        params: list[Any] = []
        if normalized:
            where = (
                "WHERE CAST(c.telegram_user_id AS TEXT)=? "
                "OR lower(c.telegram_username) LIKE ?"
            )
            params.extend((normalized, f"%{normalized}%"))

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM clients c {where}", params
                ).fetchone()[0]
            )
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages - 1)
            rows = conn.execute(
                f"""
                SELECT c.telegram_user_id, c.telegram_username, c.promo,
                       s.expire_date, s.is_active, s.payment_status,
                       cp.server_key, cp.interface_id, cp.peer_name,
                       cp.cascade_peer_id,
                       (
                           SELECT group_concat(server_key, ', ')
                           FROM (
                               SELECT DISTINCT peers.server_key
                               FROM client_peers peers
                               WHERE peers.telegram_user_id=c.telegram_user_id
                                 AND peers.server_key IS NOT NULL
                               ORDER BY peers.server_key
                           )
                       ) AS server_keys,
                       (SELECT COUNT(*) FROM client_peers devices
                        WHERE devices.telegram_user_id=c.telegram_user_id) AS device_count
                FROM clients c
                LEFT JOIN subscriptions s USING(telegram_user_id)
                LEFT JOIN client_peers cp
                  ON cp.telegram_user_id=c.telegram_user_id AND cp.role='primary'
                {where}
                ORDER BY CASE WHEN c.telegram_username='' THEN 1 ELSE 0 END,
                         lower(c.telegram_username), c.telegram_user_id
                LIMIT ? OFFSET ?
                """,
                (*params, page_size, page * page_size),
            ).fetchall()
        return [dict(row) for row in rows], total

    def get_admin_client_details(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT c.telegram_user_id, c.telegram_username, c.promo,
                       s.expire_date, s.is_active, s.payment_status,
                       cp.server_key, cp.interface_id, cp.peer_name,
                       cp.cascade_peer_id,
                       (
                           SELECT group_concat(server_key, ', ')
                           FROM (
                               SELECT DISTINCT peers.server_key
                               FROM client_peers peers
                               WHERE peers.telegram_user_id=c.telegram_user_id
                                 AND peers.server_key IS NOT NULL
                               ORDER BY peers.server_key
                           )
                       ) AS server_keys,
                       (SELECT COUNT(*) FROM client_peers devices
                        WHERE devices.telegram_user_id=c.telegram_user_id) AS device_count
                FROM clients c
                LEFT JOIN subscriptions s USING(telegram_user_id)
                LEFT JOIN client_peers cp
                  ON cp.telegram_user_id=c.telegram_user_id AND cp.role='primary'
                WHERE c.telegram_user_id=?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_client_promo(self, user_id: int, promo: int) -> bool:
        if isinstance(promo, bool) or not isinstance(promo, int) or not 0 <= promo <= 90:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE clients SET promo=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_user_id=?",
                (promo, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def log_admin_promo_change(
        self,
        admin_id: int,
        user_id: int,
        server_key: str | None,
        old_promo: int,
        new_promo: int,
    ) -> None:
        details = json.dumps(
            {
                "admin_id": admin_id,
                "client_id": user_id,
                "server_key": server_key,
                "old_promo": old_promo,
                "new_promo": new_promo,
            },
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (f"telegram:{user_id}", "admin_set_discount", details),
            )
            conn.commit()

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

    def get_active_reservation(self, user_id: int) -> dict[str, Any] | None:
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
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id, status FROM provisioning_tasks
                WHERE telegram_user_id=? AND operation=? AND status IN ('pending', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, operation),
            ).fetchone()
            if existing:
                if existing["status"] == "running":
                    conn.execute(
                        """
                        UPDATE provisioning_tasks SET payload=?, last_error=?,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (encoded_payload, error[:1000], existing["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE provisioning_tasks SET payload=?, last_error=?,
                            next_attempt_at=CURRENT_TIMESTAMP,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?
                        """,
                        (encoded_payload, error[:1000], existing["id"]),
                    )
                conn.commit()
                return str(existing["id"])
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

    def claim_provisioning_tasks(
        self, worker_id: str, lease_seconds: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Atomically lease due provisioning tasks to one worker."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM provisioning_tasks
                WHERE next_attempt_at <= datetime('now')
                  AND (
                    status='pending'
                    OR (status='running' AND lease_until <= datetime('now'))
                  )
                ORDER BY created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
            task_ids = [row["id"] for row in rows]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                conn.execute(
                    f"""
                    UPDATE provisioning_tasks
                    SET status='running', lease_owner=?,
                        lease_until=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    (worker_id, f"+{int(lease_seconds)} seconds", *task_ids),
                )
            conn.commit()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            item["lease_owner"] = worker_id
            result.append(item)
        return result

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

    def renew_provisioning_lease(
        self, task_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        """Extend an active task lease owned by the current worker."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE provisioning_tasks
                SET lease_until=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (f"+{int(lease_seconds)} seconds", task_id, worker_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    def complete_provisioning_task(
        self, task_id: str, worker_id: str | None = None
    ) -> bool:
        with self._connect() as conn:
            if worker_id:
                cursor = conn.execute(
                    """
                    UPDATE provisioning_tasks SET status='completed', lease_owner=NULL,
                        lease_until=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='running' AND lease_owner=?
                    """,
                    (task_id, worker_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE provisioning_tasks SET status='completed', lease_owner=NULL,
                        lease_until=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (task_id,),
                )
            conn.commit()
            return cursor.rowcount == 1

    def fail_provisioning_task(
        self, task_id: str, error: str, worker_id: str | None = None
    ) -> None:
        with self._connect() as conn:
            owner_clause = " AND lease_owner=?" if worker_id else ""
            parameters: tuple[Any, ...] = (
                (error[:1000], task_id, worker_id)
                if worker_id
                else (error[:1000], task_id)
            )
            conn.execute(
                f"""
                UPDATE provisioning_tasks
                SET status='pending', attempts=attempts+1, last_error=?,
                    lease_owner=NULL, lease_until=NULL,
                    next_attempt_at=datetime('now', '+' || MIN(3600, 60 * (attempts + 1)) || ' seconds'),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?{owner_clause}
                """,
                parameters,
            )
            conn.commit()

    def apply_verified_payment(
        self,
        payment_id: str,
        user_id: int,
        username: str | None,
        amount: int,
        payment_method: str,
        tariff_key: str,
        days: int,
        *,
        telegram_payment_charge_id: str | None = None,
        provider_payment_charge_id: str | None = None,
        invoice_payload: str | None = None,
        is_recurring: bool = False,
        is_first_recurring: bool = False,
        subscription_expiration_date: int | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim a verified payment and update its subscription."""
        if payment_method not in {"stars", "yookassa"} or days <= 0:
            raise ValueError("Invalid payment method or subscription duration")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            payment = conn.execute(
                "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
            ).fetchone()
            if not payment:
                conn.rollback()
                raise ValueError("Payment does not exist in the local database")
            if payment["status"] != "pending":
                conn.rollback()
                return None
            expected = (
                int(payment["user_id"]) == int(user_id)
                and int(payment["amount"]) == int(amount)
                and payment["payment_method"] == payment_method
                and payment["tariff_key"] == tariff_key
            )
            if not expected:
                conn.rollback()
                raise ValueError("Verified payment does not match the local payment record")

            normalized_username = (username or "").strip().lstrip("@")
            conn.execute(
                """
                INSERT INTO clients(telegram_user_id, telegram_username)
                VALUES (?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_username=CASE WHEN excluded.telegram_username != ''
                        THEN excluded.telegram_username ELSE clients.telegram_username END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, normalized_username),
            )
            subscription = conn.execute(
                "SELECT expire_date, payment_status FROM subscriptions WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
            now = datetime.now()
            current_expiry = now
            if subscription and subscription["expire_date"]:
                try:
                    current_expiry = datetime.fromisoformat(subscription["expire_date"])
                except ValueError:
                    current_expiry = now
            is_extension = bool(
                subscription
                and subscription["payment_status"] == "paid"
                and current_expiry > now
            )
            new_expiry = max(current_expiry, now) + timedelta(days=days)
            expire_date = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
            stars_paid = amount if payment_method == "stars" else 0
            rub_paid = amount // 100 if payment_method == "yookassa" else 0
            conn.execute(
                """
                INSERT INTO subscriptions(
                    telegram_user_id, expire_date, is_active, payment_status,
                    stars_paid, rub_paid, last_payment_date, tariff_key,
                    payment_method, notification_sent, hour_notification_sent,
                    expired_notification_sent
                ) VALUES (?, ?, 1, 'paid', ?, ?, CURRENT_TIMESTAMP, ?, ?, 0, 0, 0)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    expire_date=excluded.expire_date, is_active=1,
                    payment_status='paid', stars_paid=excluded.stars_paid,
                    rub_paid=excluded.rub_paid, last_payment_date=CURRENT_TIMESTAMP,
                    tariff_key=excluded.tariff_key, payment_method=excluded.payment_method,
                    notification_sent=0, hour_notification_sent=0,
                    expired_notification_sent=0
                """,
                (
                    user_id,
                    expire_date,
                    stars_paid,
                    rub_paid,
                    tariff_key,
                    payment_method,
                ),
            )
            updated = conn.execute(
                """
                UPDATE payments SET status='succeeded', updated_at=CURRENT_TIMESTAMP,
                    telegram_payment_charge_id=COALESCE(?, telegram_payment_charge_id),
                    provider_payment_charge_id=COALESCE(?, provider_payment_charge_id),
                    invoice_payload=COALESCE(?, invoice_payload),
                    is_recurring=?, is_first_recurring=?,
                    subscription_expiration_date=?, access_days=?,
                    applied_from=?, applied_until=?
                WHERE payment_id=? AND status='pending'
                """,
                (
                    telegram_payment_charge_id,
                    provider_payment_charge_id,
                    invoice_payload,
                    int(is_recurring),
                    int(is_first_recurring),
                    subscription_expiration_date,
                    days,
                    max(current_expiry, now).strftime("%Y-%m-%d %H:%M:%S"),
                    expire_date,
                    payment_id,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return {"expire_date": expire_date, "is_extension": is_extension}

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
            "s.is_active=1 AND s.expire_date <= datetime('now') AND s.expired_notification_sent=0"
        )

    def sync_expired_access_statuses(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE subscriptions SET payment_status='expired'
                WHERE is_active=1 AND payment_status='paid'
                  AND expire_date IS NOT NULL AND expire_date <= datetime('now')
                """
            )
            conn.execute(
                """
                UPDATE client_peers SET enabled=0, updated_at=CURRENT_TIMESTAMP
                WHERE enabled=1 AND telegram_user_id IN (
                    SELECT telegram_user_id FROM subscriptions
                    WHERE is_active=1 AND payment_status='expired'
                      AND expire_date IS NOT NULL AND expire_date <= datetime('now')
                )
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
                WHERE ({where})
                  AND (c.telegram_reachable IS NULL OR c.telegram_reachable=1)
                ORDER BY s.expire_date
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
        *,
        currency: str | None = None,
        invoice_payload: str | None = None,
        provider_payment_charge_id: str | None = None,
    ) -> bool:
        try:
            effective_currency = currency or (
                "XTR" if payment_method == "stars" else "RUB"
            )
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO payments(
                        payment_id, user_id, amount, payment_method, tariff_key,
                        metadata, currency, invoice_payload, provider_payment_charge_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payment_id,
                        user_id,
                        amount,
                        payment_method,
                        tariff_key,
                        json.dumps(metadata or {}),
                        effective_currency,
                        invoice_payload,
                        provider_payment_charge_id,
                    ),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def create_stars_payment_intent(
        self,
        payment_id: str,
        user_id: int,
        amount: int,
        tariff_key: str,
        invoice_payload: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        return self.add_payment(
            payment_id,
            user_id,
            amount,
            "stars",
            tariff_key,
            metadata,
            currency="XTR",
            invoice_payload=invoice_payload,
        )

    def get_payment_by_invoice_payload(self, invoice_payload: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM payments WHERE invoice_payload=? ORDER BY id DESC LIMIT 1",
                (invoice_payload,),
            ).fetchone()
            return dict(row) if row else None

    def set_stars_invoice_message(
        self, invoice_payload: str, message_id: int | None
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE payments SET invoice_message_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE invoice_payload=? AND payment_method='stars'
                """,
                (message_id, invoice_payload),
            )
            conn.commit()
            return cursor.rowcount == 1

    def get_payment_by_telegram_charge(self, charge_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM payments WHERE telegram_payment_charge_id=? LIMIT 1",
                (charge_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_recent_payments(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM payments ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_stars_refund_observed(
        self, charge_id: str, amount: int, review_status: str = "pending_review"
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE payments SET refunded_amount=MAX(refunded_amount, ?),
                    refunded_at=COALESCE(refunded_at, CURRENT_TIMESTAMP),
                    refund_review_status=?, status='refunded',
                    updated_at=CURRENT_TIMESTAMP
                WHERE telegram_payment_charge_id=?
                """,
                (amount, review_status, charge_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    def claim_stars_refund_request(self, payment_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE payments SET refund_review_status='requested',
                    updated_at=CURRENT_TIMESTAMP
                WHERE payment_id=? AND payment_method='stars' AND status='succeeded'
                  AND COALESCE(refund_review_status, '') NOT IN ('requested', 'completed')
                """,
                (payment_id,),
            )
            conn.commit()
            return cursor.rowcount == 1

    def update_refund_request_status(self, payment_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET refund_review_status=?, updated_at=CURRENT_TIMESTAMP
                WHERE payment_id=?
                """,
                (status, payment_id),
            )
            conn.commit()

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

    def cancel_pending_payment(self, payment_id: str) -> bool:
        """Cancel a payment only while it is still pending locally."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE payments SET status='canceled', updated_at=CURRENT_TIMESTAMP
                WHERE payment_id=? AND status='pending'
                """,
                (payment_id,),
            )
            conn.commit()
            return cursor.rowcount == 1

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

    def get_payment_by_id(self, payment_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
            return dict(row) if row else None

    def record_star_transaction(
        self,
        transaction_id: str,
        direction: str,
        amount: int,
        occurred_at: int,
        *,
        transaction_type: str | None = None,
        user_id: int | None = None,
        invoice_payload: str | None = None,
        matched_payment_id: str | None = None,
        status: str = "observed",
    ) -> bool:
        if direction not in {"incoming", "outgoing"}:
            raise ValueError("Invalid Star transaction direction")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO star_transactions(
                    transaction_id, direction, amount, occurred_at,
                    transaction_type, user_id, invoice_payload,
                    matched_payment_id, status, review_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    direction,
                    amount,
                    occurred_at,
                    transaction_type,
                    user_id,
                    invoice_payload,
                    matched_payment_id,
                    status,
                    uuid.uuid4().hex[:16],
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    def update_star_transaction_match(
        self,
        transaction_id: str,
        direction: str,
        payment_id: str | None,
        status: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE star_transactions SET matched_payment_id=?, status=?
                WHERE transaction_id=? AND direction=?
                """,
                (payment_id, status, transaction_id, direction),
            )
            conn.commit()

    def repair_legacy_star_payment_matches(self) -> int:
        """Backfill charge IDs for exact pre-journal Stars payment matches.

        Older releases stored Telegram's charge ID in ``payment_id``. Exact ID,
        user, and amount matches are journal repairs only; access is not applied
        again.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            candidates = conn.execute(
                """
                SELECT p.id, p.payment_id, p.user_id, t.invoice_payload
                FROM payments p
                JOIN star_transactions t
                  ON t.transaction_id=p.payment_id AND t.direction='incoming'
                WHERE p.payment_method='stars' AND p.status='succeeded'
                  AND p.telegram_payment_charge_id IS NULL
                  AND t.status='discrepancy'
                  AND t.user_id=p.user_id AND t.amount=p.amount
                """
            ).fetchall()
            repaired = 0
            for candidate in candidates:
                payment_update = conn.execute(
                    """
                    UPDATE payments
                    SET telegram_payment_charge_id=payment_id,
                        invoice_payload=COALESCE(invoice_payload, ?),
                        currency='XTR', updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND telegram_payment_charge_id IS NULL
                    """,
                    (candidate["invoice_payload"], candidate["id"]),
                )
                if payment_update.rowcount != 1:
                    continue
                conn.execute(
                    """
                    UPDATE star_transactions
                    SET matched_payment_id=?, status='matched_historical'
                    WHERE transaction_id=? AND direction='incoming'
                    """,
                    (candidate["payment_id"], candidate["payment_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO operation_logs(peer_name, operation, details)
                    VALUES (?, 'stars_legacy_charge_backfilled', ?)
                    """,
                    (
                        f"telegram:{candidate['user_id']}",
                        f"payment_id={candidate['payment_id']}",
                    ),
                )
                repaired += 1
            conn.commit()
            return repaired

    def start_star_reconciliation_run(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute("INSERT INTO star_reconciliation_runs DEFAULT VALUES")
            conn.commit()
            return int(cursor.lastrowid)

    def finish_star_reconciliation_run(
        self,
        run_id: int,
        *,
        status: str,
        observed_count: int,
        applied_count: int,
        discrepancy_count: int,
        error_type: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE star_reconciliation_runs SET completed_at=CURRENT_TIMESTAMP,
                    status=?, observed_count=?, applied_count=?,
                    discrepancy_count=?, error_type=? WHERE id=?
                """,
                (
                    status,
                    observed_count,
                    applied_count,
                    discrepancy_count,
                    error_type,
                    run_id,
                ),
            )
            conn.commit()

    def get_latest_star_reconciliation_run(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM star_reconciliation_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def count_star_discrepancies(self) -> int:
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM star_transactions WHERE status='discrepancy'"
                ).fetchone()[0]
            )

    def list_star_discrepancies(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT review_token AS review_id, direction, amount, occurred_at,
                       transaction_type, user_id, status
                FROM star_transactions
                WHERE status='discrepancy'
                ORDER BY occurred_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 20)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def approve_star_discrepancy(self, review_id: str, admin_id: int) -> bool:
        """Approve one reviewed ledger entry without modifying VPN access."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            transaction = conn.execute(
                """
                SELECT review_token AS review_id, transaction_id, direction, user_id
                FROM star_transactions
                WHERE review_token=? AND status='discrepancy'
                """,
                (review_id,),
            ).fetchone()
            if not transaction:
                conn.rollback()
                return False
            conn.execute(
                """
                UPDATE star_transactions SET status='approved_historical'
                WHERE review_token=? AND status='discrepancy'
                """,
                (review_id,),
            )
            conn.execute(
                """
                INSERT INTO operation_logs(peer_name, operation, details)
                VALUES (?, 'stars_historical_transaction_approved', ?)
                """,
                (
                    f"telegram:{transaction['user_id'] or 'unknown'}",
                    json.dumps(
                        {
                            "admin_id": int(admin_id),
                            "direction": transaction["direction"],
                            "review_id": review_id,
                            "transaction_id": transaction["transaction_id"],
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
            return True

    def log_operation(self, peer_name: str, operation: str, details: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (peer_name, operation, details),
            )
            conn.commit()
