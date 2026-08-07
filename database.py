import json
import logging
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from config import DATABASE_FILE

logger = logging.getLogger(__name__)
LEGACY_PRIMARY_CONFIG_NAME = "Основной конфиг"
DEFAULT_CONFIG_NAME = "Конфигурация 1"
MANAGED_CONFIG_ROLE = "managed"
MAX_CLIENT_CONFIGS = 3
# Kept as an import compatibility alias for older maintenance scripts.
MAX_CONFIG_NAME_LENGTH = 48
COMPLIMENTARY_CASCADE_EXPIRY = "2099-12-31 23:59:59"


class ActiveSubscriptionError(RuntimeError):
    """Block destructive client deletion while paid access is active."""


@dataclass(frozen=True)
class ClientAccessState:
    """Effective access after ban, identity, complimentary, and paid precedence."""

    active: bool
    source: str
    cascade_expiry: str | None
    is_banned: bool
    is_complimentary: bool
    paid_expiry: str | None
    identity_verified: bool


@dataclass(frozen=True)
class InvitationClaimResult:
    """Describe an invitation claim without relying on UI-specific status text."""

    status: str
    invitation: dict[str, Any] | None = None
    client: dict[str, Any] | None = None
    conflict_reason: str | None = None


@dataclass(frozen=True)
class RefundApplication:
    """Describe an idempotent full-payment refund application."""

    user_id: int
    expire_date: str
    applied: bool


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
                    role TEXT NOT NULL DEFAULT 'managed',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_name TEXT,
                    admin_enabled INTEGER NOT NULL DEFAULT 1,
                    client_group TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(server_key, interface_id, cascade_peer_id),
                    UNIQUE(telegram_user_id, public_key)
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

                CREATE TABLE IF NOT EXISTS client_invitations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    expected_username TEXT NOT NULL,
                    token TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    promo INTEGER NOT NULL DEFAULT 0,
                    is_complimentary INTEGER NOT NULL DEFAULT 0,
                    complimentary_at TEXT,
                    complimentary_by INTEGER,
                    claimant_user_id INTEGER,
                    claimant_username TEXT,
                    conflict_reason TEXT,
                    claimed_at TEXT,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT NOT NULL
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
                CREATE INDEX IF NOT EXISTS idx_provisioning_pending
                    ON provisioning_tasks(status, next_attempt_at);
                DROP TABLE IF EXISTS server_reservations;
                """
            )
            self._ensure_column(conn, "provisioning_tasks", "lease_owner", "TEXT")
            self._ensure_column(conn, "provisioning_tasks", "lease_until", "TEXT")
            self._ensure_column(conn, "clients", "telegram_reachable", "INTEGER")
            self._ensure_column(conn, "clients", "telegram_blocked_at", "TEXT")
            self._ensure_column(conn, "clients", "last_telegram_error", "TEXT")
            self._ensure_column(
                conn, "clients", "is_banned", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "clients", "banned_at", "TEXT")
            self._ensure_column(conn, "clients", "banned_by", "INTEGER")
            self._ensure_column(conn, "clients", "ban_reason", "TEXT")
            self._ensure_column(
                conn, "clients", "is_complimentary", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "clients", "complimentary_at", "TEXT")
            self._ensure_column(conn, "clients", "complimentary_by", "INTEGER")
            self._ensure_column(
                conn, "clients", "identity_verified", "INTEGER NOT NULL DEFAULT 1"
            )
            self._ensure_column(conn, "clients", "identity_verified_at", "TEXT")
            self._ensure_column(
                conn,
                "clients",
                "identity_source",
                "TEXT NOT NULL DEFAULT 'telegram_id'",
            )
            self._ensure_column(conn, "client_invitations", "conflict_reason", "TEXT")
            self._ensure_column(
                conn, "clients", "telegram_reachability_updated_at", "TEXT"
            )
            self._ensure_column(conn, "client_peers", "config_name", "TEXT")
            self._ensure_column(
                conn, "client_peers", "admin_enabled", "INTEGER NOT NULL DEFAULT 1"
            )
            conn.execute("DROP INDEX IF EXISTS idx_client_peers_config_name")
            legacy_rows = conn.execute(
                """
                SELECT id, telegram_user_id FROM client_peers
                WHERE role='primary' AND (
                    config_name=? OR config_name IS NULL OR trim(config_name)=''
                )
                ORDER BY telegram_user_id, id
                """,
                (LEGACY_PRIMARY_CONFIG_NAME,),
            ).fetchall()
            for peer_id, user_id in legacy_rows:
                existing_names = {
                    str(row[0]).casefold()
                    for row in conn.execute(
                        """
                        SELECT config_name FROM client_peers
                        WHERE telegram_user_id=? AND id != ? AND config_name IS NOT NULL
                        """,
                        (user_id, peer_id),
                    ).fetchall()
                }
                number = 1
                config_name = DEFAULT_CONFIG_NAME
                while config_name.casefold() in existing_names:
                    number += 1
                    config_name = f"Конфигурация {number}"
                conn.execute(
                    "UPDATE client_peers SET config_name=? WHERE id=?",
                    (config_name, peer_id),
                )
            unnamed_rows = conn.execute(
                """
                SELECT id, telegram_user_id FROM client_peers
                WHERE role IN ('primary', 'additional')
                  AND (config_name IS NULL OR trim(config_name)='')
                ORDER BY telegram_user_id, id
                """
            ).fetchall()
            for peer_id, user_id in unnamed_rows:
                existing_names = {
                    str(row[0]).casefold()
                    for row in conn.execute(
                        """
                        SELECT config_name FROM client_peers
                        WHERE telegram_user_id=? AND config_name IS NOT NULL
                        """,
                        (user_id,),
                    ).fetchall()
                }
                number = 1
                config_name = DEFAULT_CONFIG_NAME
                while config_name.casefold() in existing_names:
                    number += 1
                    config_name = f"Конфигурация {number}"
                conn.execute(
                    "UPDATE client_peers SET config_name=? WHERE id=?",
                    (config_name, peer_id),
                )
            conn.execute(
                """
                UPDATE client_peers SET role=?
                WHERE role IN ('primary', 'additional')
                """,
                (MANAGED_CONFIG_ROLE,),
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_client_peers_config_name
                ON client_peers(telegram_user_id, config_name COLLATE NOCASE)
                WHERE config_name IS NOT NULL AND role='managed'
                """
            )
            conn.execute(
                """
                UPDATE provisioning_tasks
                SET status='completed', lease_owner=NULL, lease_until=NULL,
                    last_error='Retired during config-neutral onboarding migration',
                    updated_at=CURRENT_TIMESTAMP
                WHERE operation='create_peer' AND status IN ('pending', 'running')
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
                "refund_applied_at": "TEXT",
                "invoice_message_id": "INTEGER",
            }.items():
                self._ensure_column(conn, "payments", column, definition)
            conn.execute(
                """
                UPDATE payments SET refund_applied_at=COALESCE(updated_at, CURRENT_TIMESTAMP)
                WHERE payment_method='yookassa' AND status='refunded'
                  AND refund_applied_at IS NULL
                """
            )
            self._ensure_column(conn, "client_peers", "client_group", "TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_provisioning_claim
                ON provisioning_tasks(status, next_attempt_at, lease_until)
                """
            )
            conn.execute(
                """
                UPDATE clients SET identity_verified=0,
                    identity_verified_at=NULL, identity_source='telegram_id'
                WHERE identity_verified_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM operation_logs logs
                      WHERE logs.operation='admin_add_client'
                        AND logs.peer_name='telegram:' || clients.telegram_user_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM telegram_ui_panels panels
                      WHERE panels.telegram_user_id=clients.telegram_user_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM payments payments
                      WHERE payments.user_id=clients.telegram_user_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM star_transactions stars
                      WHERE stars.user_id=clients.telegram_user_id
                  )
                """
            )
            conn.execute(
                """
                UPDATE clients SET identity_verified_at=COALESCE(identity_verified_at, created_at)
                WHERE identity_verified=1
                """
            )
            conn.execute(
                """
                UPDATE client_invitations SET status='conflict',
                    conflict_reason=COALESCE(conflict_reason, 'legacy_manual_review')
                WHERE status='claim_pending'
                """
            )
            conn.execute("DROP INDEX IF EXISTS idx_active_invitation_username")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_invitation_username
                ON client_invitations(lower(expected_username))
                WHERE status IN ('pending', 'conflict')
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

    def is_client_banned(self, user_id: int) -> bool:
        """Return whether an existing client is administratively banned."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_banned FROM clients WHERE telegram_user_id=?", (user_id,)
            ).fetchone()
        return bool(row and row[0])

    def is_client_identity_verified(self, user_id: int) -> bool:
        """Allow outbound delivery unless an existing client awaits first contact."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT identity_verified FROM clients WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
        return row is None or bool(row[0])

    def set_client_ban(
        self,
        user_id: int,
        admin_id: int,
        banned: bool,
        reason: str | None = None,
    ) -> bool:
        """Persist a reversible ban and its audit record atomically."""
        normalized_reason = " ".join((reason or "").split()) or None
        if normalized_reason and len(normalized_reason) > 500:
            raise ValueError("Ban reason must not exceed 500 characters")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE clients SET is_banned=?,
                    banned_at=CASE WHEN ?=1 THEN CURRENT_TIMESTAMP ELSE NULL END,
                    banned_by=CASE WHEN ?=1 THEN ? ELSE NULL END,
                    ban_reason=CASE WHEN ?=1 THEN ? ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE telegram_user_id=?
                """,
                (
                    int(banned),
                    int(banned),
                    int(banned),
                    admin_id,
                    int(banned),
                    normalized_reason,
                    user_id,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"telegram:{user_id}",
                    "admin_ban_client" if banned else "admin_unban_client",
                    json.dumps(
                        {
                            "admin_id": admin_id,
                            "client_id": user_id,
                            "reason": normalized_reason,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
        return True

    def has_active_subscription(self, user_id: int) -> bool:
        """Return whether paid access is currently valid."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM subscriptions
                WHERE telegram_user_id=? AND is_active=1 AND payment_status='paid'
                  AND expire_date IS NOT NULL AND datetime(expire_date) > datetime('now')
                """,
                (user_id,),
            ).fetchone()
        return row is not None

    def get_client_access_state(self, user_id: int) -> ClientAccessState:
        """Return the authoritative access state for one client."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT c.is_banned, c.is_complimentary, c.identity_verified,
                       s.expire_date, s.is_active, s.payment_status
                FROM clients c
                LEFT JOIN subscriptions s USING(telegram_user_id)
                WHERE c.telegram_user_id=?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return ClientAccessState(False, "none", None, False, False, None, False)
        is_banned = bool(row["is_banned"])
        is_complimentary = bool(row["is_complimentary"])
        identity_verified = bool(row["identity_verified"])
        paid_expiry = str(row["expire_date"]) if row["expire_date"] else None
        try:
            paid_active = bool(
                row["is_active"]
                and row["payment_status"] == "paid"
                and paid_expiry
                and datetime.fromisoformat(paid_expiry).replace(tzinfo=UTC)
                > datetime.now(UTC)
            )
        except ValueError:
            paid_active = False
        if is_banned:
            return ClientAccessState(
                False,
                "none",
                None,
                True,
                is_complimentary,
                paid_expiry,
                identity_verified,
            )
        if not identity_verified:
            return ClientAccessState(
                False, "none", None, False, is_complimentary, paid_expiry, False
            )
        if is_complimentary:
            return ClientAccessState(
                True,
                "complimentary",
                COMPLIMENTARY_CASCADE_EXPIRY,
                False,
                True,
                paid_expiry,
                True,
            )
        if paid_active:
            return ClientAccessState(
                True, "paid", paid_expiry, False, False, paid_expiry, True
            )
        return ClientAccessState(False, "none", None, False, False, paid_expiry, True)

    def has_active_access(self, user_id: int) -> bool:
        return self.get_client_access_state(user_id).active

    def set_client_complimentary(
        self, user_id: int, admin_id: int, enabled: bool
    ) -> bool:
        """Set complimentary access without mutating paid subscription history."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE clients SET is_complimentary=?,
                    complimentary_at=CASE WHEN ?=1 THEN CURRENT_TIMESTAMP ELSE NULL END,
                    complimentary_by=CASE WHEN ?=1 THEN ? ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE telegram_user_id=?
                """,
                (int(enabled), int(enabled), int(enabled), admin_id, user_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"telegram:{user_id}",
                    "admin_enable_complimentary"
                    if enabled
                    else "admin_disable_complimentary",
                    json.dumps(
                        {"admin_id": admin_id, "client_id": user_id}, sort_keys=True
                    ),
                ),
            )
            conn.commit()
        return True

    def find_clients_by_username(self, username: str) -> list[dict[str, Any]]:
        normalized = username.strip().lstrip("@").casefold()
        if not normalized:
            return []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM clients
                WHERE lower(telegram_username)=?
                ORDER BY telegram_user_id
                """,
                (normalized,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _new_invitation_token() -> str:
        return secrets.token_urlsafe(18)

    def create_client_invitation(
        self, expected_username: str, admin_id: int, ttl_days: int = 7
    ) -> dict[str, Any]:
        normalized = expected_username.strip().lstrip("@").casefold()
        if not normalized or not normalized.isascii() or len(normalized) > 32 or not all(
            character.isalnum() or character == "_" for character in normalized
        ):
            raise ValueError("Invalid Telegram username")
        token = self._new_invitation_token()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM client_invitations
                WHERE lower(expected_username)=?
                  AND status IN ('pending', 'conflict')
                """,
                (normalized,),
            ).fetchone()
            if existing:
                conn.rollback()
                return dict(existing)
            cursor = conn.execute(
                """
                INSERT INTO client_invitations(
                    expected_username, token, created_by, expires_at
                ) VALUES (?, ?, ?, datetime('now', ?))
                """,
                (normalized, token, admin_id, f"{int(ttl_days):+d} days"),
            )
            invitation_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"invite:{invitation_id}",
                    "admin_create_client_invitation",
                    json.dumps(
                        {
                            "admin_id": admin_id,
                            "expected_username": normalized,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            row = conn.execute(
                "SELECT * FROM client_invitations WHERE id=?", (invitation_id,)
            ).fetchone()
            conn.commit()
        return dict(row)

    def get_client_invitation(self, invitation_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT *, CASE
                    WHEN status='pending' AND expires_at <= datetime('now') THEN 'expired'
                    ELSE status END AS display_status
                FROM client_invitations WHERE id=?
                """,
                (invitation_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_client_invitations(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *, CASE
                    WHEN status='pending' AND expires_at <= datetime('now') THEN 'expired'
                    ELSE status END AS display_status
                FROM client_invitations
                WHERE status != 'claimed'
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def reissue_client_invitation(
        self, invitation_id: int, admin_id: int, ttl_days: int = 7
    ) -> dict[str, Any] | None:
        token = self._new_invitation_token()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE client_invitations SET token=?, status='pending',
                    claimant_user_id=NULL, claimant_username=NULL, claimed_at=NULL,
                    conflict_reason=NULL,
                    expires_at=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status != 'claimed'
                """,
                (token, f"{int(ttl_days):+d} days", invitation_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"invite:{invitation_id}",
                    "admin_reissue_client_invitation",
                    json.dumps({"admin_id": admin_id}, sort_keys=True),
                ),
            )
            row = conn.execute(
                "SELECT * FROM client_invitations WHERE id=?", (invitation_id,)
            ).fetchone()
            conn.commit()
        return dict(row)

    def claim_client_invitation(
        self, token: str, user_id: int, username: str | None
    ) -> InvitationClaimResult:
        """Consume an invitation and auto-bind only an unambiguous claimant."""
        actual_username = (username or "").strip().lstrip("@")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            invitation = conn.execute(
                """
                SELECT * FROM client_invitations
                WHERE token=? AND status='pending' AND expires_at > datetime('now')
                """,
                (token,),
            ).fetchone()
            if not invitation:
                conn.rollback()
                return InvitationClaimResult("invalid")

            expected_username = str(invitation["expected_username"])
            conflict_reason: str | None = None
            if not actual_username:
                conflict_reason = "username_missing"
            elif actual_username.casefold() != expected_username.casefold():
                conflict_reason = "username_mismatch"
            else:
                other_owners = conn.execute(
                    """
                    SELECT telegram_user_id FROM clients
                    WHERE lower(telegram_username)=lower(?)
                      AND telegram_user_id != ?
                    ORDER BY telegram_user_id
                    """,
                    (expected_username, user_id),
                ).fetchall()
                claimant = conn.execute(
                    "SELECT is_banned FROM clients WHERE telegram_user_id=?",
                    (user_id,),
                ).fetchone()
                if other_owners:
                    conflict_reason = "username_owned_by_other_client"
                elif claimant and claimant["is_banned"]:
                    conflict_reason = "claimant_banned"

            invitation_id = int(invitation["id"])
            if conflict_reason:
                conn.execute(
                    """
                    UPDATE client_invitations SET token=NULL, status='conflict',
                        claimant_user_id=?, claimant_username=?,
                        conflict_reason=?, claimed_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (user_id, actual_username, conflict_reason, invitation_id),
                )
                conn.execute(
                    "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                    (
                        f"invite:{invitation_id}",
                        "client_claim_invitation_conflict",
                        json.dumps(
                            {
                                "claimant_user_id": user_id,
                                "claimant_username": actual_username or None,
                                "conflict_reason": conflict_reason,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                claimed = conn.execute(
                    "SELECT * FROM client_invitations WHERE id=?", (invitation_id,)
                ).fetchone()
                conn.commit()
                return InvitationClaimResult(
                    "conflict", dict(claimed), None, conflict_reason
                )

            conn.execute(
                """
                UPDATE client_invitations SET token=NULL,
                    claimant_user_id=?, claimant_username=?, claimed_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (user_id, actual_username, invitation_id),
            )
            refreshed = conn.execute(
                "SELECT * FROM client_invitations WHERE id=?", (invitation_id,)
            ).fetchone()
            merge = self._bind_invitation_client(
                conn, refreshed, identity_source="username_invite"
            )
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"telegram:{user_id}",
                    "client_auto_approve_invitation",
                    json.dumps(
                        {
                            "invitation_id": invitation_id,
                            "claimant_user_id": user_id,
                            "claimant_username": actual_username,
                            **merge,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            claimed = conn.execute(
                "SELECT * FROM client_invitations WHERE id=?", (invitation_id,)
            ).fetchone()
            conn.commit()
        return InvitationClaimResult(
            "auto_approved",
            dict(claimed),
            self.get_admin_client_details(user_id),
        )

    @staticmethod
    def _bind_invitation_client(
        conn: sqlite3.Connection,
        invitation: sqlite3.Row,
        *,
        identity_source: str,
    ) -> dict[str, Any]:
        """Bind a reviewed invitation while only improving existing benefits."""
        user_id = int(invitation["claimant_user_id"])
        username = str(invitation["claimant_username"] or "")
        existing = conn.execute(
            """
            SELECT promo, is_complimentary, complimentary_at, complimentary_by
            FROM clients WHERE telegram_user_id=?
            """,
            (user_id,),
        ).fetchone()
        previous_promo = int(existing["promo"]) if existing else 0
        previous_complimentary = bool(existing["is_complimentary"]) if existing else False
        invitation_promo = int(invitation["promo"])
        invitation_complimentary = bool(invitation["is_complimentary"])
        merged_promo = max(previous_promo, invitation_promo)
        merged_complimentary = previous_complimentary or invitation_complimentary
        if previous_complimentary:
            complimentary_at = existing["complimentary_at"]
            complimentary_by = existing["complimentary_by"]
        elif invitation_complimentary:
            complimentary_at = invitation["complimentary_at"]
            complimentary_by = invitation["complimentary_by"]
        else:
            complimentary_at = None
            complimentary_by = None
        conn.execute(
            """
            INSERT INTO clients(
                telegram_user_id, telegram_username, promo, is_complimentary,
                complimentary_at, complimentary_by, identity_verified,
                identity_verified_at, identity_source
            ) VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username=CASE WHEN excluded.telegram_username != ''
                    THEN excluded.telegram_username ELSE clients.telegram_username END,
                promo=excluded.promo,
                is_complimentary=excluded.is_complimentary,
                complimentary_at=excluded.complimentary_at,
                complimentary_by=excluded.complimentary_by,
                identity_verified=1,
                identity_verified_at=CURRENT_TIMESTAMP,
                identity_source=excluded.identity_source,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                user_id,
                username,
                merged_promo,
                int(merged_complimentary),
                complimentary_at,
                complimentary_by,
                identity_source,
            ),
        )
        conn.execute(
            """
            INSERT INTO subscriptions(telegram_user_id, payment_status)
            VALUES (?, 'unpaid') ON CONFLICT(telegram_user_id) DO NOTHING
            """,
            (user_id,),
        )
        conn.execute(
            """
            UPDATE client_invitations SET status='claimed', conflict_reason=NULL,
                updated_at=CURRENT_TIMESTAMP WHERE id=?
            """,
            (int(invitation["id"]),),
        )
        return {
            "previous_promo": previous_promo,
            "merged_promo": merged_promo,
            "previous_complimentary": previous_complimentary,
            "merged_complimentary": merged_complimentary,
        }

    def approve_client_invitation(
        self, invitation_id: int, admin_id: int
    ) -> dict[str, Any] | None:
        """Atomically bind a reviewed invitation to its claimant."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            invitation = conn.execute(
                """
                SELECT * FROM client_invitations
                WHERE id=? AND status='conflict' AND claimant_user_id IS NOT NULL
                """,
                (invitation_id,),
            ).fetchone()
            if not invitation:
                conn.rollback()
                return None
            user_id = int(invitation["claimant_user_id"])
            merge = self._bind_invitation_client(
                conn, invitation, identity_source="admin_override"
            )
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"telegram:{user_id}",
                    "admin_approve_client_invitation",
                    json.dumps(
                        {
                            "admin_id": admin_id,
                            "invitation_id": invitation_id,
                            **merge,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
        return self.get_admin_client_details(user_id)

    def reject_client_invitation(self, invitation_id: int, admin_id: int) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE client_invitations SET status='rejected', token=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='conflict'
                """,
                (invitation_id,),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"invite:{invitation_id}",
                    "admin_reject_client_invitation",
                    json.dumps({"admin_id": admin_id}, sort_keys=True),
                ),
            )
            conn.commit()
        return True

    def set_invitation_promo(
        self, invitation_id: int, admin_id: int, promo: int
    ) -> bool:
        if not 0 <= promo <= 90:
            return False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE client_invitations SET promo=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status != 'claimed'
                """,
                (promo, invitation_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                    (
                        f"invite:{invitation_id}",
                        "admin_set_invitation_discount",
                        json.dumps(
                            {"admin_id": admin_id, "promo": promo}, sort_keys=True
                        ),
                    ),
                )
            conn.commit()
        return cursor.rowcount == 1

    def set_invitation_complimentary(
        self, invitation_id: int, admin_id: int, enabled: bool
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE client_invitations SET is_complimentary=?,
                    complimentary_at=CASE WHEN ?=1 THEN CURRENT_TIMESTAMP ELSE NULL END,
                    complimentary_by=CASE WHEN ?=1 THEN ? ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status != 'claimed'
                """,
                (int(enabled), int(enabled), int(enabled), admin_id, invitation_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                    (
                        f"invite:{invitation_id}",
                        "admin_enable_invitation_complimentary"
                        if enabled
                        else "admin_disable_invitation_complimentary",
                        json.dumps({"admin_id": admin_id}, sort_keys=True),
                    ),
                )
            conn.commit()
        return cursor.rowcount == 1

    def delete_client_invitation(self, invitation_id: int, admin_id: int) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT expected_username FROM client_invitations WHERE id=?",
                (invitation_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                return False
            conn.execute("DELETE FROM client_invitations WHERE id=?", (invitation_id,))
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"invite:{invitation_id}",
                    "admin_delete_client_invitation",
                    json.dumps(
                        {"admin_id": admin_id, "expected_username": row[0]},
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
        return True

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
        role: str = MANAGED_CONFIG_ROLE,
        enabled: bool = True,
        config_name: str | None = None,
        admin_enabled: bool = True,
        client_group: str | None = None,
    ) -> bool:
        self.upsert_client(user_id)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if role in {"primary", "additional"}:
                    role = MANAGED_CONFIG_ROLE
                if role == MANAGED_CONFIG_ROLE and config_name is None:
                    existing_names = {
                        str(row[0]).casefold()
                        for row in conn.execute(
                            """
                            SELECT config_name FROM client_peers
                            WHERE telegram_user_id=? AND role='managed'
                              AND config_name IS NOT NULL
                            """,
                            (user_id,),
                        ).fetchall()
                    }
                    number = 1
                    config_name = DEFAULT_CONFIG_NAME
                    while config_name.casefold() in existing_names:
                        number += 1
                        config_name = f"Конфигурация {number}"
                if config_name is not None:
                    config_name = normalize_config_name(config_name)
                    existing_names = conn.execute(
                        """
                        SELECT config_name, role FROM client_peers
                        WHERE telegram_user_id=? AND role='managed'
                          AND config_name IS NOT NULL
                          AND public_key != ?
                        """,
                        (user_id, public_key),
                    ).fetchall()
                    if any(
                        str(row[0]).casefold() == config_name.casefold()
                        and role == MANAGED_CONFIG_ROLE
                        for row in existing_names
                    ):
                        return False
                conn.execute(
                    """
                    INSERT INTO client_peers(
                        telegram_user_id, server_key, interface_id, cascade_peer_id,
                        public_key, peer_name, role, enabled, config_name,
                        admin_enabled, client_group
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id, public_key) DO UPDATE SET
                        server_key=excluded.server_key,
                        interface_id=excluded.interface_id,
                        cascade_peer_id=excluded.cascade_peer_id,
                        peer_name=excluded.peer_name,
                        role=excluded.role,
                        enabled=excluded.enabled,
                        config_name=COALESCE(excluded.config_name, client_peers.config_name),
                        admin_enabled=excluded.admin_enabled,
                        client_group=COALESCE(excluded.client_group, client_peers.client_group),
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
                        client_group,
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
        sql += " ORDER BY id"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, (user_id,)).fetchall()]

    def get_client_cascade_peers(self, user_id: int) -> list[dict[str, Any]]:
        """Return every known Cascade peer, including queued orphan cleanup targets."""
        peers = self.get_client_peers(user_id, bound_only=True)
        identities = {
            (
                str(peer["server_key"]),
                str(peer["interface_id"]),
                str(peer["cascade_peer_id"]),
            )
            for peer in peers
        }
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload FROM provisioning_tasks
                WHERE telegram_user_id=? AND operation='delete_cascade_peer'
                """,
                (user_id,),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row[0])
                if not isinstance(payload, dict):
                    continue
                identity = (
                    str(payload.get("server_key") or "").strip(),
                    str(payload.get("interface_id") or "").strip(),
                    str(payload.get("cascade_peer_id") or "").strip(),
                )
            except (TypeError, ValueError):
                continue
            if not all(identity) or identity in identities:
                continue
            identities.add(identity)
            peers.append(
                {
                    "server_key": identity[0],
                    "interface_id": identity[1],
                    "cascade_peer_id": identity[2],
                    "role": "orphan_cleanup",
                }
            )
        return peers

    def set_client_peer_group(self, peer_id: int, group_name: str | None) -> bool:
        """Store the last group verified for one managed Cascade peer."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE client_peers SET client_group=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND role='managed'
                """,
                (group_name, peer_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    def set_client_peer_groups(self, user_id: int, group_name: str) -> int:
        """Persist a verified unified group for all managed peers of one client."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE client_peers SET client_group=?, updated_at=CURRENT_TIMESTAMP
                WHERE telegram_user_id=? AND role='managed'
                """,
                (group_name, user_id),
            )
            conn.commit()
            return cursor.rowcount

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

    def get_admin_managed_config(
        self, peer_id: int, user_id: int
    ) -> dict[str, Any] | None:
        """Return an admin-visible peer with the owner's current payment status."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT cp.*, s.payment_status, c.is_complimentary, c.is_banned,
                       c.identity_verified,
                       CASE WHEN c.is_banned=0 AND c.identity_verified=1 AND (
                           c.is_complimentary=1 OR (
                               s.is_active=1 AND s.payment_status='paid'
                               AND s.expire_date IS NOT NULL
                               AND datetime(s.expire_date) > datetime('now')
                           )
                       ) THEN 1 ELSE 0 END AS has_active_access
                FROM client_peers cp
                LEFT JOIN subscriptions s USING(telegram_user_id)
                JOIN clients c USING(telegram_user_id)
                WHERE cp.id=? AND cp.telegram_user_id=?
                  AND cp.role='managed'
                  AND cp.server_key IS NOT NULL
                  AND cp.interface_id IS NOT NULL
                  AND cp.cascade_peer_id IS NOT NULL
                LIMIT 1
                """,
                (peer_id, user_id),
            ).fetchone()
            return dict(row) if row else None

    def get_admin_client_configs(self, user_id: int) -> list[dict[str, Any]]:
        """Return managed configurations visible to administrators."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT cp.*, s.payment_status, c.is_complimentary, c.is_banned,
                       c.identity_verified,
                       CASE WHEN c.is_banned=0 AND c.identity_verified=1 AND (
                           c.is_complimentary=1 OR (
                               s.is_active=1 AND s.payment_status='paid'
                               AND s.expire_date IS NOT NULL
                               AND datetime(s.expire_date) > datetime('now')
                           )
                       ) THEN 1 ELSE 0 END AS has_active_access
                FROM client_peers cp
                LEFT JOIN subscriptions s USING(telegram_user_id)
                JOIN clients c USING(telegram_user_id)
                WHERE cp.telegram_user_id=?
                  AND cp.role='managed'
                  AND cp.server_key IS NOT NULL
                  AND cp.interface_id IS NOT NULL
                  AND cp.cascade_peer_id IS NOT NULL
                ORDER BY cp.id
                """,
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

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
            WHERE telegram_user_id=? AND role='managed'
              AND server_key IS NOT NULL AND interface_id IS NOT NULL
              AND cascade_peer_id IS NOT NULL
        """
        if available_only:
            sql += " AND admin_enabled=1 AND enabled=1"
        sql += " ORDER BY id"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, (user_id,)).fetchall()]

    def get_client_visible_configs(self, user_id: int) -> list[dict[str, Any]]:
        """Return configurations a client may manage, including missing peers."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM client_peers
                WHERE telegram_user_id=? AND role='managed'
                  AND admin_enabled=1
                ORDER BY id
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_managed_configs(self, user_id: int) -> int:
        """Count all managed records, including inactive admin-created ones."""
        with self._connect() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM client_peers
                    WHERE telegram_user_id=? AND role='managed'
                    """,
                    (user_id,),
                ).fetchone()[0]
            )

    def get_all_managed_client_peers(self) -> list[dict[str, Any]]:
        """Return all bound peers eligible for Cascade group reconciliation."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM client_peers
                WHERE role='managed'
                  AND server_key IS NOT NULL AND interface_id IS NOT NULL
                  AND cascade_peer_id IS NOT NULL
                ORDER BY telegram_user_id, id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def rename_managed_config(self, peer_id: int, user_id: int, name: str) -> bool:
        normalized = normalize_config_name(name)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing_names = conn.execute(
                    """
                    SELECT config_name FROM client_peers
                    WHERE telegram_user_id=? AND id != ?
                      AND role='managed'
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
                      AND role='managed'
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
                WHERE id=? AND telegram_user_id=? AND role='managed'
                """,
                (int(admin_enabled), peer_id, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_managed_config(self, peer_id: int, user_id: int) -> bool:
        """Delete one managed configuration owned by the selected client."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM client_peers
                WHERE id=? AND telegram_user_id=? AND role='managed'
                """,
                (peer_id, user_id),
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
        client_group: str | None = None,
    ) -> None:
        details = json.dumps(
            {
                "admin_id": admin_id,
                "client_id": user_id,
                "peer_id": peer_id,
                "server_key": server_key,
                "client_group": client_group,
            },
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (f"telegram:{user_id}", operation, details),
            )
            conn.commit()

    def log_client_config_change(
        self,
        user_id: int,
        peer_id: int,
        operation: str,
        *,
        server_key: str | None = None,
        config_name: str | None = None,
        cascade_missing: bool | None = None,
    ) -> None:
        """Audit a self-service configuration mutation."""
        details = json.dumps(
            {
                "client_id": user_id,
                "peer_id": peer_id,
                "server_key": server_key,
                "config_name": config_name,
                "cascade_missing": cascade_missing,
            },
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (f"telegram:{user_id}", operation, details),
            )
            conn.commit()

    def log_client_state_sync(
        self, admin_id: int, user_id: int, operation: str, result: dict[str, int]
    ) -> None:
        """Audit an administrative Cascade state synchronization."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"telegram:{user_id}",
                    operation,
                    json.dumps(
                        {"admin_id": admin_id, "client_id": user_id, **result},
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()

    def log_identity_activation_sync(
        self, user_id: int, result: dict[str, int]
    ) -> None:
        """Audit Cascade reconciliation triggered by first-contact verification."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"telegram:{user_id}",
                    "client_identity_activation_sync",
                    json.dumps({"client_id": user_id, **result}, sort_keys=True),
                ),
            )
            conn.commit()

    def log_invitation_activation_sync(
        self, invitation_id: int, user_id: int, result: dict[str, int]
    ) -> None:
        """Audit Cascade reconciliation after an automatic invitation binding."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"telegram:{user_id}",
                    "client_invitation_activation_sync",
                    json.dumps(
                        {
                            "invitation_id": invitation_id,
                            "client_id": user_id,
                            **result,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()

    def log_admin_client_group_change(
        self,
        admin_id: int,
        user_id: int,
        old_groups: list[str],
        new_group: str,
        peer_count: int,
        operation: str = "admin_change_client_group",
    ) -> None:
        details = json.dumps(
            {
                "admin_id": admin_id,
                "client_id": user_id,
                "old_groups": sorted(old_groups),
                "new_group": new_group,
                "peer_count": peer_count,
            },
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (f"telegram:{user_id}", operation, details),
            )
            conn.commit()

    def log_admin_client_deletion(
        self,
        admin_id: int,
        user_id: int,
        operation: str,
        *,
        deleted: int,
        already_missing: int,
        failed: int,
        forced_without_refund: bool = False,
        subscription_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Audit a successful or failed administrative client deletion."""
        details = json.dumps(
            {
                "admin_id": admin_id,
                "client_id": user_id,
                "deleted": deleted,
                "already_missing": already_missing,
                "failed": failed,
                "forced_without_refund": forced_without_refund,
                "subscription": subscription_snapshot,
            },
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (f"telegram:{user_id}", operation, details),
            )
            conn.commit()

    def delete_client_operational_data(
        self,
        admin_id: int,
        user_id: int,
        *,
        deleted: int,
        already_missing: int,
        allow_active_subscription: bool = False,
    ) -> dict[str, int] | None:
        """Atomically remove client runtime state while retaining finance and audit data."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            client = conn.execute(
                """
                SELECT c.telegram_user_id, s.expire_date, s.is_active,
                       s.payment_status, s.payment_method
                FROM clients c
                LEFT JOIN subscriptions s USING(telegram_user_id)
                WHERE c.telegram_user_id=?
                """,
                (user_id,),
            ).fetchone()
            if not client:
                conn.rollback()
                return None
            paid_active = bool(
                client[2]
                and client[3] == "paid"
                and client[1]
                and conn.execute(
                    "SELECT datetime(?) > datetime('now')", (client[1],)
                ).fetchone()[0]
            )
            subscription_snapshot = {
                "expire_date": client[1],
                "is_active": bool(client[2]),
                "payment_status": client[3],
                "payment_method": client[4],
            }
            if paid_active and not allow_active_subscription:
                conn.rollback()
                raise ActiveSubscriptionError(
                    f"Client {user_id} still has an active paid subscription"
                )

            counts = {
                "peers": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM client_peers WHERE telegram_user_id=?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "provisioning_tasks": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM provisioning_tasks WHERE telegram_user_id=?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "telegram_ui_panels": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM telegram_ui_panels WHERE telegram_user_id=?",
                        (user_id,),
                    ).fetchone()[0]
                ),
            }
            workflow_cursor = conn.execute(
                """
                DELETE FROM admin_workflows
                WHERE admin_id=?
                   OR CAST(json_extract(data, '$.user_id') AS INTEGER)=?
                """,
                (user_id, user_id),
            )
            counts["admin_workflows"] = workflow_cursor.rowcount
            conn.execute(
                "DELETE FROM provisioning_tasks WHERE telegram_user_id=?", (user_id,)
            )
            conn.execute(
                "DELETE FROM telegram_ui_panels WHERE telegram_user_id=?", (user_id,)
            )
            client_cursor = conn.execute(
                "DELETE FROM clients WHERE telegram_user_id=?", (user_id,)
            )
            if client_cursor.rowcount != 1:
                conn.rollback()
                return None
            details = json.dumps(
                {
                    "admin_id": admin_id,
                    "client_id": user_id,
                    "deleted": deleted,
                    "already_missing": already_missing,
                    "failed": 0,
                    "forced_without_refund": bool(
                        paid_active and allow_active_subscription
                    ),
                    "subscription": subscription_snapshot,
                    "operational_rows": counts,
                },
                sort_keys=True,
            )
            conn.execute(
                """
                INSERT INTO operation_logs(peer_name, operation, details)
                VALUES (?, ?, ?)
                """,
                (
                    f"telegram:{user_id}",
                    "admin_delete_client_without_refund"
                    if paid_active and allow_active_subscription
                    else "admin_delete_client",
                    details,
                ),
            )
            conn.commit()
            return counts

    def get_peer_by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
        """Return a compatibility view consumed by existing bot UI handlers."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT c.telegram_user_id, c.telegram_username, c.promo,
                       c.is_complimentary, c.complimentary_at, c.complimentary_by,
                       c.identity_verified, c.identity_verified_at, c.identity_source,
                       c.is_banned,
                       s.*, cp.peer_name, cp.public_key, cp.cascade_peer_id,
                       cp.server_key, cp.interface_id, cp.role, cp.enabled
                FROM clients c
                LEFT JOIN subscriptions s USING(telegram_user_id)
                LEFT JOIN client_peers cp
                  ON cp.id=(SELECT MIN(first_peer.id) FROM client_peers first_peer
                            WHERE first_peer.telegram_user_id=c.telegram_user_id
                              AND first_peer.role='managed')
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
                    WHERE (telegram_reachable IS NULL OR telegram_reachable=1)
                      AND is_banned=0
                      AND identity_verified=1
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
            "telegram_reachable": int(row[5]),
            "telegram_blocked": int(row[6]),
            "telegram_reachability_unknown": int(row[7]),
            "stars_discrepancies": int(row[8]),
            "stars_last_success_age_seconds": int(row[9])
            if row[9] is not None
            else None,
            "legacy_callbacks_today": int(row[10] or 0),
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
                       c.is_complimentary, c.complimentary_at, c.complimentary_by,
                       c.identity_verified, c.identity_verified_at, c.identity_source,
                       c.is_banned, c.banned_at, c.banned_by, c.ban_reason,
                       s.expire_date, s.is_active, s.payment_status, s.payment_method,
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
                       (
                           SELECT group_concat(client_group, ', ')
                           FROM (
                               SELECT DISTINCT peers.client_group
                               FROM client_peers peers
                               WHERE peers.telegram_user_id=c.telegram_user_id
                                 AND peers.role='managed'
                                 AND peers.server_key IS NOT NULL
                                 AND peers.interface_id IS NOT NULL
                                 AND peers.cascade_peer_id IS NOT NULL
                                 AND peers.client_group IS NOT NULL
                               ORDER BY peers.client_group
                           )
                       ) AS client_groups,
                       (SELECT COUNT(*) FROM client_peers peers
                        WHERE peers.telegram_user_id=c.telegram_user_id
                          AND peers.role='managed'
                          AND peers.server_key IS NOT NULL
                          AND peers.interface_id IS NOT NULL
                          AND peers.cascade_peer_id IS NOT NULL
                          AND peers.client_group IS NULL) AS unknown_group_count,
                       (SELECT COUNT(*) FROM client_peers devices
                        WHERE devices.telegram_user_id=c.telegram_user_id) AS device_count
                FROM clients c
                LEFT JOIN subscriptions s USING(telegram_user_id)
                LEFT JOIN client_peers cp
                  ON cp.id=(SELECT MIN(first_peer.id) FROM client_peers first_peer
                            WHERE first_peer.telegram_user_id=c.telegram_user_id
                              AND first_peer.role='managed')
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
                       c.is_complimentary, c.complimentary_at, c.complimentary_by,
                       c.identity_verified, c.identity_verified_at, c.identity_source,
                       c.is_banned, c.banned_at, c.banned_by, c.ban_reason,
                       s.expire_date, s.is_active, s.payment_status, s.payment_method,
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
                       (
                           SELECT group_concat(client_group, ', ')
                           FROM (
                               SELECT DISTINCT peers.client_group
                               FROM client_peers peers
                               WHERE peers.telegram_user_id=c.telegram_user_id
                                 AND peers.role='managed'
                                 AND peers.server_key IS NOT NULL
                                 AND peers.interface_id IS NOT NULL
                                 AND peers.cascade_peer_id IS NOT NULL
                                 AND peers.client_group IS NOT NULL
                               ORDER BY peers.client_group
                           )
                       ) AS client_groups,
                       (SELECT COUNT(*) FROM client_peers peers
                        WHERE peers.telegram_user_id=c.telegram_user_id
                          AND peers.role='managed'
                          AND peers.server_key IS NOT NULL
                          AND peers.interface_id IS NOT NULL
                          AND peers.cascade_peer_id IS NOT NULL
                          AND peers.client_group IS NULL) AS unknown_group_count,
                       (SELECT COUNT(*) FROM client_peers devices
                        WHERE devices.telegram_user_id=c.telegram_user_id) AS device_count
                FROM clients c
                LEFT JOIN subscriptions s USING(telegram_user_id)
                LEFT JOIN client_peers cp
                  ON cp.id=(SELECT MIN(first_peer.id) FROM client_peers first_peer
                            WHERE first_peer.telegram_user_id=c.telegram_user_id
                              AND first_peer.role='managed')
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

    def admin_add_client(self, user_id: int, admin_id: int) -> dict[str, Any]:
        """Create an unpaid client profile before their first bot interaction."""
        if user_id <= 0:
            raise ValueError("Telegram user ID must be positive")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            created = conn.execute(
                """
                INSERT INTO clients(
                    telegram_user_id, identity_verified,
                    identity_verified_at, identity_source
                ) VALUES (?, 0, NULL, 'telegram_id')
                ON CONFLICT DO NOTHING
                """,
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO subscriptions(telegram_user_id, payment_status)
                VALUES (?, 'unpaid') ON CONFLICT(telegram_user_id) DO NOTHING
                """,
                (user_id,),
            )
            if created.rowcount == 1:
                conn.execute(
                    "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                    (
                        f"telegram:{user_id}",
                        "admin_add_client",
                        json.dumps(
                            {"admin_id": admin_id, "client_id": user_id},
                            sort_keys=True,
                        ),
                    ),
                )
            conn.commit()
        result = self.get_admin_client_details(user_id)
        if not result:
            raise RuntimeError("Created client could not be read")
        return result

    def verify_preadded_client(
        self, user_id: int, username: str | None
    ) -> dict[str, Any] | None:
        """Verify an admin-added Telegram ID on its first real bot interaction."""
        normalized_username = (username or "").strip().lstrip("@")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE clients SET identity_verified=1,
                    identity_verified_at=CURRENT_TIMESTAMP,
                    identity_source='telegram_id',
                    telegram_username=CASE WHEN ? != '' THEN ? ELSE telegram_username END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE telegram_user_id=? AND identity_verified=0
                """,
                (normalized_username, normalized_username, user_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            duplicates: list[int] = []
            if normalized_username:
                duplicates = [
                    int(row[0])
                    for row in conn.execute(
                        """
                        SELECT telegram_user_id FROM clients
                        WHERE lower(telegram_username)=lower(?)
                          AND telegram_user_id != ?
                        ORDER BY telegram_user_id
                        """,
                        (normalized_username, user_id),
                    ).fetchall()
                ]
            details = {
                "client_id": user_id,
                "username": normalized_username or None,
                "duplicate_username_ids": duplicates,
            }
            conn.execute(
                "INSERT INTO operation_logs(peer_name, operation, details) VALUES (?, ?, ?)",
                (
                    f"telegram:{user_id}",
                    "client_verify_telegram_id",
                    json.dumps(details, sort_keys=True),
                ),
            )
            conn.commit()
        client = self.get_admin_client_details(user_id)
        if not client:
            return None
        return {"client": client, **details}

    def set_admin_subscription_expiry(
        self,
        admin_id: int,
        user_id: int,
        expire_date: str,
    ) -> dict[str, Any] | None:
        """Set an existing subscription expiry and audit the administrative grant."""
        target = datetime.fromisoformat(expire_date)
        if target.tzinfo is not None:
            target = target.astimezone(UTC).replace(tzinfo=None)
        normalized_expiry = target.strftime("%Y-%m-%d %H:%M:%S")
        is_future = target > datetime.now(UTC).replace(tzinfo=None)
        payment_status = "paid" if is_future else "expired"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """
                SELECT expire_date, is_active, payment_status
                FROM subscriptions WHERE telegram_user_id=?
                """,
                (user_id,),
            ).fetchone()
            if not current:
                conn.rollback()
                return None
            updated = conn.execute(
                """
                UPDATE subscriptions SET
                    expire_date=?, is_active=?, payment_status=?,
                    notification_sent=0, hour_notification_sent=0,
                    expired_notification_sent=0
                WHERE telegram_user_id=?
                """,
                (normalized_expiry, int(is_future), payment_status, user_id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            details = json.dumps(
                {
                    "admin_id": admin_id,
                    "client_id": user_id,
                    "old_expire_date": current["expire_date"],
                    "new_expire_date": normalized_expiry,
                    "old_payment_status": current["payment_status"],
                    "new_payment_status": payment_status,
                },
                sort_keys=True,
            )
            conn.execute(
                """
                INSERT INTO operation_logs(peer_name, operation, details)
                VALUES (?, 'admin_set_expire_date', ?)
                """,
                (f"telegram:{user_id}", details),
            )
            conn.commit()
        return {
            "old_expire_date": current["expire_date"],
            "expire_date": normalized_expiry,
            "is_active": int(is_future),
            "payment_status": payment_status,
        }

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
            can_apply_canceled_yookassa = (
                payment["status"] == "canceled"
                and payment_method == "yookassa"
                and payment["payment_method"] == "yookassa"
            )
            if payment["status"] != "pending" and not can_apply_canceled_yookassa:
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
                WHERE payment_id=? AND status IN ('pending', 'canceled')
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

    def apply_refund(
        self, payment_id: str, days: int
    ) -> RefundApplication | None:
        """Atomically and idempotently apply one confirmed full-payment refund."""
        if days <= 0:
            raise ValueError("Refund duration must be positive")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            payment = conn.execute(
                """
                SELECT user_id, status, refund_applied_at FROM payments
                WHERE payment_id=? AND status IN ('succeeded', 'refunded')
                """,
                (payment_id,),
            ).fetchone()
            if not payment:
                conn.rollback()
                return None
            user_id = int(payment["user_id"])
            subscription = conn.execute(
                "SELECT expire_date FROM subscriptions WHERE telegram_user_id=?",
                (user_id,),
            ).fetchone()
            if not subscription or not subscription[0]:
                conn.rollback()
                return None
            if payment["refund_applied_at"]:
                conn.rollback()
                return RefundApplication(user_id, str(subscription[0]), False)
            new_expiry = datetime.fromisoformat(subscription[0]) - timedelta(days=days)
            value = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
            is_future = new_expiry > datetime.now(UTC).replace(tzinfo=None)
            payment_status = "paid" if is_future else "expired"
            conn.execute(
                """
                UPDATE subscriptions SET expire_date=?, is_active=?, payment_status=?,
                    notification_sent=0,
                    hour_notification_sent=0, expired_notification_sent=0
                WHERE telegram_user_id=?
                """,
                (value, int(is_future), payment_status, user_id),
            )
            conn.execute(
                """
                UPDATE payments SET status='refunded',
                    refund_applied_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE payment_id=? AND refund_applied_at IS NULL
                """,
                (payment_id,),
            )
            conn.commit()
            return RefundApplication(user_id, value, True)

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
                    SELECT s.telegram_user_id
                    FROM subscriptions s JOIN clients c USING(telegram_user_id)
                    WHERE s.is_active=1 AND s.payment_status='expired'
                      AND s.expire_date IS NOT NULL
                      AND s.expire_date <= datetime('now')
                      AND c.is_complimentary=0
                )
                """
            )
            conn.commit()
            return cursor.rowcount

    def get_users_for_notification(self, days_before: int = 3) -> list[dict[str, Any]]:
        lower_bound_hours = max(0, int(days_before) * 24 - 1)
        return self._subscription_query(
            f"s.is_active=1 AND s.payment_status='paid' AND s.notification_sent=0 "
            f"AND s.expire_date <= datetime('now', '+{int(days_before)} days') "
            f"AND s.expire_date > datetime('now', '+{lower_bound_hours} hours')"
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
                  AND c.is_banned=0
                  AND c.is_complimentary=0
                  AND c.identity_verified=1
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
