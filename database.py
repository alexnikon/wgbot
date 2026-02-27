import json
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import DATABASE_FILE

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_file: str = DATABASE_FILE):
        self.db_file = db_file
        self.init_database()

    def init_database(self):
        """Initialize the database and create required tables."""
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()

            # Table for peer records
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS peers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_name TEXT UNIQUE NOT NULL,
                    peer_id TEXT UNIQUE NOT NULL,
                    job_id TEXT UNIQUE NOT NULL,
                    telegram_user_id INTEGER,
                    telegram_username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expire_date TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    payment_status TEXT DEFAULT 'unpaid',
                    stars_paid INTEGER DEFAULT 0,
                    last_payment_date TIMESTAMP,
                    notification_sent BOOLEAN DEFAULT 0,
                    expired_notification_sent BOOLEAN DEFAULT 0
                )
            """)

            # Table for operation logs
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_name TEXT,
                    operation TEXT,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Table for payments
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    currency TEXT DEFAULT 'RUB',
                    status TEXT DEFAULT 'pending',
                    payment_method TEXT,
                    tariff_key TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT
                )
            """)

            # Migration: add new columns if missing
            self._migrate_database(cursor)

            conn.commit()
            logger.info("Database initialized")

    def _migrate_database(self, cursor):
        """Run database migrations to add new columns."""
        try:
            # Check if payment_status column exists
            cursor.execute("PRAGMA table_info(peers)")
            columns = [column[1] for column in cursor.fetchall()]

            if "payment_status" not in columns:
                cursor.execute(
                    "ALTER TABLE peers ADD COLUMN payment_status TEXT DEFAULT 'unpaid'"
                )
                logger.info("Added column: payment_status")

            if "stars_paid" not in columns:
                cursor.execute(
                    "ALTER TABLE peers ADD COLUMN stars_paid INTEGER DEFAULT 0"
                )
                logger.info("Added column: stars_paid")

            if "last_payment_date" not in columns:
                cursor.execute(
                    "ALTER TABLE peers ADD COLUMN last_payment_date TIMESTAMP"
                )
                logger.info("Added column: last_payment_date")

            if "notification_sent" not in columns:
                cursor.execute(
                    "ALTER TABLE peers ADD COLUMN notification_sent BOOLEAN DEFAULT 0"
                )
                logger.info("Added column: notification_sent")

            if "expired_notification_sent" not in columns:
                cursor.execute(
                    "ALTER TABLE peers ADD COLUMN expired_notification_sent BOOLEAN DEFAULT 0"
                )
                logger.info("Added column: expired_notification_sent")

            # Add columns for the new tariff system
            if "tariff_key" not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN tariff_key TEXT")
                logger.info("Added column: tariff_key")

            if "payment_method" not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN payment_method TEXT")
                logger.info("Added column: payment_method")

            if "rub_paid" not in columns:
                cursor.execute(
                    "ALTER TABLE peers ADD COLUMN rub_paid INTEGER DEFAULT 0"
                )
                logger.info("Added column: rub_paid")

        except Exception as e:
            logger.error(f"Database migration error: {e}")

    def add_peer(
        self,
        peer_name: str,
        peer_id: str,
        job_id: str,
        telegram_user_id: int,
        telegram_username: str,
        expire_date: str,
        payment_status: str = "paid",
        stars_paid: int = 0,
        tariff_key: str = None,
        payment_method: str = None,
        rub_paid: int = 0,
    ) -> bool:
        """
        Add a new peer record to the database.

        Args:
            peer_name: Peer name
            peer_id: Peer ID in WireGuard
            job_id: Job ID for restriction
            telegram_user_id: Telegram user ID
            telegram_username: Telegram username
            expire_date: Expiration date
            payment_status: Payment status ('paid', 'unpaid')
            stars_paid: Stars paid amount
            tariff_key: Tariff key (7_days, 30_days)
            payment_method: Payment method (stars, yookassa)
            rub_paid: RUB paid amount

        Returns:
            True if successfully inserted
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO peers (peer_name, peer_id, job_id, telegram_user_id,
                                     telegram_username, created_at, expire_date, is_active,
                                     payment_status, stars_paid, last_payment_date,
                                     notification_sent, tariff_key, payment_method, rub_paid)
                    VALUES (?, ?, ?, ?, ?, datetime('now'), ?, 1, ?, ?, NULL, 0, ?, ?, ?)
                """,
                    (
                        peer_name,
                        peer_id,
                        job_id,
                        telegram_user_id,
                        telegram_username,
                        expire_date,
                        payment_status,
                        stars_paid,
                        tariff_key,
                        payment_method,
                        rub_paid,
                    ),
                )
                conn.commit()

                # Log operation
                self.log_operation(
                    peer_name,
                    "CREATE_PEER",
                    f"Created peer {peer_name}, tariff {tariff_key}",
                )
                return True

        except sqlite3.IntegrityError as e:
            logger.error(f"Failed to add peer {peer_name}: {e}")
            return False

    def stage_peer_record(
        self,
        peer_name: str,
        telegram_user_id: int,
        telegram_username: str,
        expire_date: str,
        payment_status: str = "paid",
        tariff_key: str = None,
        payment_method: str = None,
        rub_paid: int = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Create/update a peer record in a temporary pending state.
        This enforces ordering: DB -> WG peer -> WG job -> clients.json.
        """
        pending_peer_id = f"pending_peer_{uuid.uuid4()}"
        pending_job_id = f"pending_job_{uuid.uuid4()}"

        try:
            with sqlite3.connect(self.db_file) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM peers WHERE telegram_user_id = ? AND is_active = 1",
                    (telegram_user_id,),
                )
                existing = cursor.fetchone()

                if existing:
                    existing_dict = dict(existing)
                    cursor.execute(
                        """
                        UPDATE peers
                        SET peer_name = ?,
                            peer_id = ?,
                            job_id = ?,
                            telegram_username = ?,
                            expire_date = ?,
                            payment_status = ?,
                            tariff_key = COALESCE(?, tariff_key),
                            payment_method = COALESCE(?, payment_method),
                            rub_paid = CASE WHEN ? IS NULL THEN rub_paid ELSE ? END
                        WHERE id = ?
                        """,
                        (
                            peer_name,
                            pending_peer_id,
                            pending_job_id,
                            telegram_username,
                            expire_date,
                            payment_status,
                            tariff_key,
                            payment_method,
                            rub_paid,
                            rub_paid,
                            existing_dict["id"],
                        ),
                    )
                    conn.commit()
                    return {
                        "mode": "update",
                        "pending_peer_id": pending_peer_id,
                        "pending_job_id": pending_job_id,
                        "previous": {
                            "peer_name": existing_dict.get("peer_name"),
                            "peer_id": existing_dict.get("peer_id"),
                            "job_id": existing_dict.get("job_id"),
                            "telegram_username": existing_dict.get("telegram_username"),
                            "expire_date": existing_dict.get("expire_date"),
                            "payment_status": existing_dict.get("payment_status"),
                            "tariff_key": existing_dict.get("tariff_key"),
                            "payment_method": existing_dict.get("payment_method"),
                            "rub_paid": existing_dict.get("rub_paid"),
                        },
                    }

                cursor.execute(
                    """
                    INSERT INTO peers (peer_name, peer_id, job_id, telegram_user_id,
                                     telegram_username, created_at, expire_date, is_active,
                                     payment_status, stars_paid, last_payment_date,
                                     notification_sent, tariff_key, payment_method, rub_paid)
                    VALUES (?, ?, ?, ?, ?, datetime('now'), ?, 1, ?, 0, NULL, 0, ?, ?, ?)
                    """,
                    (
                        peer_name,
                        pending_peer_id,
                        pending_job_id,
                        telegram_user_id,
                        telegram_username,
                        expire_date,
                        payment_status,
                        tariff_key,
                        payment_method,
                        0 if rub_paid is None else rub_paid,
                    ),
                )
                conn.commit()
                return {
                    "mode": "create",
                    "pending_peer_id": pending_peer_id,
                    "pending_job_id": pending_job_id,
                }

        except sqlite3.IntegrityError as e:
            logger.error(f"Failed to stage peer {peer_name}: {e}")
            return None

    def finalize_staged_peer(
        self,
        telegram_user_id: int,
        stage_info: Dict[str, Any],
        peer_name: str,
        peer_id: str,
        job_id: str,
        expire_date: str,
        telegram_username: str,
        payment_status: str = "paid",
        tariff_key: str = None,
        payment_method: str = None,
        rub_paid: int = None,
    ) -> bool:
        """Finalize a pending record by writing real peer_id/job_id."""
        if not stage_info:
            return False

        pending_peer_id = stage_info.get("pending_peer_id")
        pending_job_id = stage_info.get("pending_job_id")
        mode = stage_info.get("mode", "update")

        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE peers
                    SET peer_name = ?,
                        peer_id = ?,
                        job_id = ?,
                        telegram_username = ?,
                        expire_date = ?,
                        payment_status = ?,
                        tariff_key = COALESCE(?, tariff_key),
                        payment_method = COALESCE(?, payment_method),
                        rub_paid = CASE WHEN ? IS NULL THEN rub_paid ELSE ? END
                    WHERE telegram_user_id = ?
                      AND peer_id = ?
                      AND job_id = ?
                      AND is_active = 1
                    """,
                    (
                        peer_name,
                        peer_id,
                        job_id,
                        telegram_username,
                        expire_date,
                        payment_status,
                        tariff_key,
                        payment_method,
                        rub_paid,
                        rub_paid,
                        telegram_user_id,
                        pending_peer_id,
                        pending_job_id,
                    ),
                )
                conn.commit()
                if cursor.rowcount <= 0:
                    return False

                operation = "CREATE_PEER" if mode == "create" else "UPDATE_PEER"
                details = (
                    f"Created peer {peer_name}, tariff {tariff_key}"
                    if mode == "create"
                    else f"Updated peer {peer_name} with new ID"
                )
                self.log_operation(peer_name, operation, details)
                return True

        except Exception as e:
            logger.error(f"Failed to finalize peer {peer_name}: {e}")
            return False

    def rollback_staged_peer(self, telegram_user_id: int, stage_info: Dict[str, Any]) -> bool:
        """Rollback a pending record on create/update errors."""
        if not stage_info:
            return False

        pending_peer_id = stage_info.get("pending_peer_id")
        pending_job_id = stage_info.get("pending_job_id")
        mode = stage_info.get("mode")

        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()

                if mode == "create":
                    cursor.execute(
                        """
                        DELETE FROM peers
                        WHERE telegram_user_id = ?
                          AND peer_id = ?
                          AND job_id = ?
                          AND is_active = 1
                        """,
                        (telegram_user_id, pending_peer_id, pending_job_id),
                    )
                else:
                    previous = stage_info.get("previous") or {}
                    cursor.execute(
                        """
                        UPDATE peers
                        SET peer_name = ?,
                            peer_id = ?,
                            job_id = ?,
                            telegram_username = ?,
                            expire_date = ?,
                            payment_status = ?,
                            tariff_key = ?,
                            payment_method = ?,
                            rub_paid = ?
                        WHERE telegram_user_id = ?
                          AND peer_id = ?
                          AND job_id = ?
                          AND is_active = 1
                        """,
                        (
                            previous.get("peer_name"),
                            previous.get("peer_id"),
                            previous.get("job_id"),
                            previous.get("telegram_username"),
                            previous.get("expire_date"),
                            previous.get("payment_status"),
                            previous.get("tariff_key"),
                            previous.get("payment_method"),
                            previous.get("rub_paid"),
                            telegram_user_id,
                            pending_peer_id,
                            pending_job_id,
                        ),
                    )

                conn.commit()
                return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to rollback staged peer for user {telegram_user_id}: {e}")
            return False

    def get_peer_by_name(self, peer_name: str) -> Optional[Dict[str, Any]]:
        """Get peer info by name."""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM peers WHERE peer_name = ?", (peer_name,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_peer_by_telegram_id(
        self, telegram_user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Get peer info by Telegram user ID."""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM peers WHERE telegram_user_id = ? AND is_active = 1",
                (telegram_user_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_peers(self) -> List[Dict[str, Any]]:
        """Get all active peers."""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM peers WHERE is_active = 1 ORDER BY created_at DESC"
            )
            return [dict(row) for row in cursor.fetchall()]

    def delete_peer(self, peer_name: str) -> bool:
        """
        Mark a peer as inactive in the database.

        Args:
            peer_name: Peer name to deactivate

        Returns:
            True if successfully deactivated
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE peers SET is_active = 0 WHERE peer_name = ?", (peer_name,)
                )
                conn.commit()

                # Log operation
                self.log_operation(peer_name, "DELETE_PEER", f"Deleted peer {peer_name}")
                return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to delete peer {peer_name}: {e}")
            return False

    def get_job_data(self, peer_name: str) -> Optional[Dict[str, Any]]:
        """Get job data for deletion."""
        peer = self.get_peer_by_name(peer_name)
        if not peer:
            return None

        return {
            "JobID": peer["job_id"],
            "Configuration": "awg0",  # Could be moved to config
            "Peer": peer["peer_id"],
            "Field": "date",
            "Operator": "lgt",
            "Value": peer["expire_date"],
            "CreationDate": peer["created_at"],
            "ExpireDate": peer["expire_date"],
            "Action": "restrict",
        }

    def log_operation(self, peer_name: str, operation: str, details: str):
        """Log an operation to the database."""
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO operation_logs (peer_name, operation, details)
                    VALUES (?, ?, ?)
                """,
                    (peer_name, operation, details),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log operation: {e}")

    def get_operation_logs(
        self, peer_name: str = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get operation logs."""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if peer_name:
                cursor.execute(
                    """
                    SELECT * FROM operation_logs
                    WHERE peer_name = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """,
                    (peer_name, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM operation_logs
                    ORDER BY timestamp DESC
                    LIMIT ?
                """,
                    (limit,),
                )

            return [dict(row) for row in cursor.fetchall()]

    def get_expired_peers(self) -> List[Dict[str, Any]]:
        """Get expired peers that have not been notified yet."""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM peers
                WHERE is_active = 1 AND expire_date < datetime('now') AND expired_notification_sent = 0
                ORDER BY expire_date ASC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def update_peer_info(
        self,
        peer_name: str,
        new_peer_id: str,
        new_job_id: str,
        new_expire_date: str = None,
    ) -> bool:
        """
        Update peer info (ID, job_id, and expiration date).

        Args:
            peer_name: Peer name
            new_peer_id: New peer ID
            new_job_id: New job ID
            new_expire_date: New expiration date (optional)

        Returns:
            True if successfully updated
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()

                if new_expire_date:
                    cursor.execute(
                        """
                        UPDATE peers
                        SET peer_id = ?, job_id = ?, expire_date = ?
                        WHERE peer_name = ? AND is_active = 1
                    """,
                        (new_peer_id, new_job_id, new_expire_date, peer_name),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE peers
                        SET peer_id = ?, job_id = ?
                        WHERE peer_name = ? AND is_active = 1
                    """,
                        (new_peer_id, new_job_id, peer_name),
                    )

                conn.commit()

                # Log operation
                self.log_operation(
                    peer_name, "UPDATE_PEER", f"Updated peer {peer_name} with new ID"
                )
                return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to update peer {peer_name}: {e}")
            return False

    def update_payment_status(
        self,
        telegram_user_id: int,
        payment_status: str,
        amount_paid: int = 0,
        payment_method: str = None,
        tariff_key: str = None,
    ) -> bool:
        """
        Update payment status for a user.

        Args:
            telegram_user_id: Telegram user ID
            payment_status: Payment status ('paid', 'unpaid')
            amount_paid: Amount paid (stars or rubles)
            payment_method: Payment method ('stars', 'yookassa')
            tariff_key: Tariff key (7_days, 30_days)

        Returns:
            True if successfully updated
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()

                # Update fields depending on payment method
                if payment_method == "stars":
                    cursor.execute(
                        """
                        UPDATE peers
                        SET payment_status = ?, stars_paid = ?, last_payment_date = datetime('now'),
                            payment_method = ?, tariff_key = ?
                        WHERE telegram_user_id = ? AND is_active = 1
                    """,
                        (
                            payment_status,
                            amount_paid,
                            payment_method,
                            tariff_key,
                            telegram_user_id,
                        ),
                    )
                elif payment_method == "yookassa":
                    cursor.execute(
                        """
                        UPDATE peers
                        SET payment_status = ?, rub_paid = ?, last_payment_date = datetime('now'),
                            payment_method = ?, tariff_key = ?
                        WHERE telegram_user_id = ? AND is_active = 1
                    """,
                        (
                            payment_status,
                            amount_paid,
                            payment_method,
                            tariff_key,
                            telegram_user_id,
                        ),
                    )
                else:
                    # Backward compatibility
                    cursor.execute(
                        """
                        UPDATE peers
                        SET payment_status = ?, stars_paid = ?, last_payment_date = datetime('now')
                        WHERE telegram_user_id = ? AND is_active = 1
                    """,
                        (payment_status, amount_paid, telegram_user_id),
                    )

                conn.commit()

                # Log operation
                self.log_operation(
                    f"user_{telegram_user_id}",
                    "PAYMENT_UPDATE",
                    f"Payment status updated: {payment_status}, {payment_method}: {amount_paid}, tariff: {tariff_key}",
                )
                return cursor.rowcount > 0

        except Exception as e:
            logger.error(
                f"Failed to update payment status for user {telegram_user_id}: {e}"
            )
            return False

    def extend_access(self, telegram_user_id: int, days: int = 30) -> tuple[bool, str]:
        """
        Extend user access by the specified number of days.

        Args:
            telegram_user_id: Telegram user ID
            days: Number of days to extend

        Returns:
            Tuple (success: bool, new_expire_date: str)
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()

                # Fetch current expiration date
                cursor.execute(
                    """
                    SELECT expire_date FROM peers
                    WHERE telegram_user_id = ? AND is_active = 1
                """,
                    (telegram_user_id,),
                )
                result = cursor.fetchone()

                if not result:
                    return False, ""

                current_expire_date_str = result[0]

                # If expired, start from now; otherwise extend current expiration
                cursor.execute(
                    """
                    SELECT
                        CASE
                            WHEN datetime(?) < datetime('now') THEN datetime('now', '+{} days')
                            ELSE datetime(?, '+{} days')
                        END
                """.format(days, days),
                    (current_expire_date_str, current_expire_date_str),
                )
                new_expire_date = cursor.fetchone()[0]

                # Update expiration date
                cursor.execute(
                    """
                    UPDATE peers
                    SET expire_date = ?, notification_sent = 0, expired_notification_sent = 0
                    WHERE telegram_user_id = ? AND is_active = 1
                """,
                    (new_expire_date, telegram_user_id),
                )
                conn.commit()

                # Log operation
                self.log_operation(
                    f"user_{telegram_user_id}",
                    "EXTEND_ACCESS",
                    f"Extended access by {days} days. New date: {new_expire_date}",
                )
                return cursor.rowcount > 0, new_expire_date

        except Exception as e:
            logger.error(
                f"Failed to extend access for user {telegram_user_id}: {e}"
            )
            return False, ""

    def decrease_access(self, telegram_user_id: int, days: int) -> tuple[bool, str]:
        """
        Decrease user access by the specified number of days.
        
        Args:
            telegram_user_id: Telegram user ID
            days: Number of days to decrease
            
        Returns:
            Tuple (success: bool, new_expire_date: str)
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                
                # Fetch current expiration date
                cursor.execute(
                    """
                    SELECT expire_date FROM peers 
                    WHERE telegram_user_id = ? AND is_active = 1
                    """,
                    (telegram_user_id,),
                )
                result = cursor.fetchone()
                
                if not result:
                    return False, ""
                
                current_expire_date_str = result[0]
                
                # Subtract days
                cursor.execute(
                    """
                    SELECT datetime(?, '-{} days')
                    """.format(days),
                    (current_expire_date_str,),
                )
                new_expire_date = cursor.fetchone()[0]
                
                # Update expiration date
                cursor.execute(
                    """
                    UPDATE peers 
                    SET expire_date = ?
                    WHERE telegram_user_id = ? AND is_active = 1
                    """,
                    (new_expire_date, telegram_user_id),
                )
                conn.commit()
                
                # Log operation
                self.log_operation(
                    f"user_{telegram_user_id}", 
                    "DECREASE_ACCESS", 
                    f"Decreased access by {days} days. New date: {new_expire_date}"
                )
                return cursor.rowcount > 0, new_expire_date
                
        except Exception as e:
            logger.error(
                f"Failed to decrease access for user {telegram_user_id}: {e}"
            )
            return False, ""

    def get_users_for_notification(self, days_before: int = 3) -> List[Dict[str, Any]]:
        """
        Get users who should receive upcoming expiration notifications.

        Args:
            days_before: How many days before expiration to notify

        Returns:
            List of users to notify
        """
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM peers
                WHERE is_active = 1
                AND payment_status = 'paid'
                AND notification_sent = 0
                AND expire_date <= datetime('now', '+{} days')
                AND expire_date > datetime('now')
                ORDER BY expire_date ASC
            """.format(days_before)
            )
            return [dict(row) for row in cursor.fetchall()]

    def mark_notification_sent(self, telegram_user_id: int) -> bool:
        """
        Mark that a notification was sent to the user.

        Args:
            telegram_user_id: Telegram user ID

        Returns:
            True if successfully marked
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE peers
                    SET notification_sent = 1
                    WHERE telegram_user_id = ? AND is_active = 1
                """,
                    (telegram_user_id,),
                )
                conn.commit()
                return cursor.rowcount > 0

        except Exception as e:
            logger.error(
                f"Failed to mark notification for user {telegram_user_id}: {e}"
            )
            return False

    def mark_expired_notification_sent(self, telegram_user_id: int) -> bool:
        """
        Mark that the expiration notification was sent (one-time).
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE peers
                    SET expired_notification_sent = 1
                    WHERE telegram_user_id = ? AND is_active = 1
                """,
                    (telegram_user_id,),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(
                f"Failed to mark expired notification for user {telegram_user_id}: {e}"
            )
            return False

    def add_payment(
        self,
        payment_id: str,
        user_id: int,
        amount: int,
        payment_method: str,
        tariff_key: str,
        metadata: dict = None,
    ) -> bool:
        """
        Add a new payment record.

        Args:
            payment_id: YooKassa payment ID
            user_id: Telegram user ID
            amount: Amount in kopeks
            payment_method: Payment method
            tariff_key: Tariff key
            metadata: Extra metadata

        Returns:
            True if successfully added
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO payments (payment_id, user_id, amount, payment_method,
                                         tariff_key, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        payment_id,
                        user_id,
                        amount,
                        payment_method,
                        tariff_key,
                        json.dumps(metadata) if metadata else None,
                    ),
                )
                conn.commit()

                # Log operation
                self.log_operation(
                    f"user_{user_id}",
                    "CREATE_PAYMENT",
                    f"Created payment {payment_id}, amount: {amount}",
                )
                return True

        except sqlite3.IntegrityError as e:
            logger.error(f"Failed to add payment {payment_id}: {e}")
            return False

    def update_payment_status_by_id(self, payment_id: str, status: str) -> bool:
        """
        Update payment status by ID.

        Args:
            payment_id: Payment ID
            status: New status (pending, succeeded, canceled, refunded)

        Returns:
            True if successfully updated
        """
        try:
            # Validate status
            valid_statuses = ["pending", "succeeded", "canceled", "refunded"]
            if status not in valid_statuses:
                logger.error(f"Invalid payment status: {status}")
                return False

            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE payments
                    SET status = ?, updated_at = datetime('now')
                    WHERE payment_id = ?
                """,
                    (status, payment_id),
                )
                conn.commit()

                # Log operation
                self.log_operation(
                    f"payment_{payment_id}",
                    "UPDATE_PAYMENT_STATUS",
                    f"Payment status updated to: {status}",
                )
                return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"Failed to update payment status {payment_id}: {e}")
            return False

    def get_payment_by_id(self, payment_id: str) -> Optional[Dict[str, Any]]:
        """
        Get payment info by ID.

        Args:
            payment_id: Payment ID

        Returns:
            Payment data or None
        """
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_payments_by_user(self, user_id: int) -> List[Dict[str, Any]]:
        """
        Get all payments for a user.

        Args:
            user_id: User ID

        Returns:
            List of payments
        """
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM payments
                WHERE user_id = ?
                ORDER BY created_at DESC
            """,
                (user_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_pending_payments(self) -> List[Dict[str, Any]]:
        """
        Get all pending payments.

        Returns:
            List of pending payments
        """
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM payments
                WHERE status = 'pending'
                ORDER BY created_at ASC
            """)
            return [dict(row) for row in cursor.fetchall()]
