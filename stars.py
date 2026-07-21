import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from aiogram import Bot

from cascade_api import CascadeRouter
from database import Database
from payment import PaymentManager
from utils import generate_peer_name

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    observed: int = 0
    applied: int = 0
    discrepancies: int = 0
    received_stars: int = 0
    refunded_stars: int = 0


class StarsReconciler:
    """Reconcile Telegram's Star ledger with the local payment journal."""

    def __init__(
        self,
        bot: Bot,
        db: Database,
        payment_manager: PaymentManager,
        cascade_router: CascadeRouter,
        notify_admins: Callable[[str], Awaitable[None]],
        interval_seconds: int,
    ) -> None:
        self.bot = bot
        self.db = db
        self.payment_manager = payment_manager
        self.cascade_router = cascade_router
        self.notify_admins = notify_admins
        self.interval_seconds = interval_seconds
        self._run_lock = asyncio.Lock()
        self._last_daily_report: date | None = None

    async def run(self) -> None:
        while True:
            try:
                result = await self.run_once()
                if self._last_daily_report != date.today():
                    await self.notify_admins(self.format_report(result, daily=True))
                    self._last_daily_report = date.today()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Stars reconciliation cycle failed")
                await asyncio.sleep(60)

    async def run_once(self) -> ReconciliationResult:
        async with self._run_lock:
            run_id = await asyncio.to_thread(self.db.start_star_reconciliation_run)
            result = ReconciliationResult()
            try:
                await asyncio.to_thread(self.db.ensure_telegram_daily_metrics_day)
                repaired = await asyncio.to_thread(
                    self.db.repair_legacy_star_payment_matches
                )
                if repaired:
                    logger.info(
                        "Backfilled %s exact legacy Stars payment matches", repaired
                    )
                offset = 0
                while True:
                    page = await self.bot.get_star_transactions(offset=offset, limit=100)
                    transactions = list(page.transactions)
                    if not transactions:
                        break
                    new_in_page = 0
                    for transaction in transactions:
                        inserted, applied = await self._process_transaction(transaction)
                        if inserted:
                            new_in_page += 1
                            result.observed += 1
                            if getattr(transaction, "source", None) is not None:
                                result.received_stars += int(transaction.amount)
                            elif getattr(transaction, "receiver", None) is not None:
                                result.refunded_stars += abs(int(transaction.amount))
                        if applied:
                            result.applied += 1
                    if len(transactions) < 100 or new_in_page == 0:
                        break
                    offset += len(transactions)
                result.discrepancies = await asyncio.to_thread(
                    self.db.count_star_discrepancies
                )
                await asyncio.to_thread(
                    self.db.finish_star_reconciliation_run,
                    run_id,
                    status="completed",
                    observed_count=result.observed,
                    applied_count=result.applied,
                    discrepancy_count=result.discrepancies,
                )
                return result
            except Exception as exc:
                await asyncio.to_thread(
                    self.db.finish_star_reconciliation_run,
                    run_id,
                    status="failed",
                    observed_count=result.observed,
                    applied_count=result.applied,
                    discrepancy_count=result.discrepancies,
                    error_type=type(exc).__name__,
                )
                raise

    async def _process_transaction(self, transaction: Any) -> tuple[bool, bool]:
        source = getattr(transaction, "source", None)
        receiver = getattr(transaction, "receiver", None)
        direction = "incoming" if source is not None else "outgoing"
        partner = source or receiver
        transaction_type = getattr(partner, "transaction_type", None)
        user = getattr(partner, "user", None)
        user_id = getattr(user, "id", None)
        invoice_payload = getattr(partner, "invoice_payload", None)
        transaction_id = str(transaction.id)
        amount = int(transaction.amount)
        inserted = await asyncio.to_thread(
            self.db.record_star_transaction,
            transaction_id,
            direction,
            amount,
            int(transaction.date.timestamp())
            if hasattr(transaction.date, "timestamp")
            else int(transaction.date),
            transaction_type=transaction_type,
            user_id=user_id,
            invoice_payload=invoice_payload,
        )
        if not inserted:
            return False, False
        if direction == "outgoing" and user_id is not None:
            payment = await asyncio.to_thread(
                self.db.get_payment_by_telegram_charge, transaction_id
            )
            if payment:
                await asyncio.to_thread(
                    self.db.mark_stars_refund_observed,
                    transaction_id,
                    abs(amount),
                )
                await asyncio.to_thread(
                    self.db.update_star_transaction_match,
                    transaction_id,
                    direction,
                    payment["payment_id"],
                    "refund_pending_review",
                )
                await asyncio.to_thread(
                    self.db.log_operation,
                    f"telegram:{payment['user_id']}",
                    "stars_refund_observed",
                    f"payment_id={payment['payment_id']}",
                )
                await self.notify_admins(
                    "⚠️ Обнаружен возврат Telegram Stars\n\n"
                    f"Payment ID: {payment['payment_id']}\n"
                    f"Telegram ID: {payment['user_id']}\n"
                    "Доступ автоматически не изменен."
                )
            else:
                await asyncio.to_thread(
                    self.db.update_star_transaction_match,
                    transaction_id,
                    direction,
                    None,
                    "discrepancy",
                )
                await asyncio.to_thread(
                    self.db.log_operation,
                    "telegram:unknown",
                    "stars_reconciliation_discrepancy",
                    "reason=unmatched_refund",
                )
            return True, False
        if direction != "incoming" or transaction_type != "invoice_payment":
            return True, False

        payment = await asyncio.to_thread(
            self.db.get_payment_by_telegram_charge, transaction_id
        )
        if payment is None and invoice_payload:
            payment = await asyncio.to_thread(
                self.db.get_payment_by_invoice_payload, invoice_payload
            )
        if not payment or payment["status"] != "pending" or not invoice_payload:
            status = "matched" if payment else "discrepancy"
            await asyncio.to_thread(
                self.db.update_star_transaction_match,
                transaction_id,
                direction,
                payment["payment_id"] if payment else None,
                status,
            )
            if status == "discrepancy":
                await asyncio.to_thread(
                    self.db.log_operation,
                    f"telegram:{user_id or 'unknown'}",
                    "stars_reconciliation_discrepancy",
                    "reason=missing_pending_intent",
                )
            return True, False

        parsed = self.payment_manager.parse_invoice_payload(invoice_payload)
        if (
            not parsed
            or int(payment["user_id"]) != int(user_id or 0)
            or int(payment["amount"]) != amount
            or payment["tariff_key"] != parsed[1]
        ):
            await asyncio.to_thread(
                self.db.update_star_transaction_match,
                transaction_id,
                direction,
                payment["payment_id"],
                "discrepancy",
            )
            await asyncio.to_thread(
                self.db.log_operation,
                f"telegram:{user_id or 'unknown'}",
                "stars_reconciliation_discrepancy",
                f"payment_id={payment['payment_id']};reason=verification_mismatch",
            )
            return True, False

        metadata = json.loads(payment.get("metadata") or "{}")
        tariff = self.payment_manager.tariffs[payment["tariff_key"]]
        applied = await asyncio.to_thread(
            self.db.apply_verified_payment,
            payment["payment_id"],
            int(payment["user_id"]),
            metadata.get("username") or None,
            amount,
            "stars",
            payment["tariff_key"],
            int(tariff["days"]),
            telegram_payment_charge_id=transaction_id,
            invoice_payload=invoice_payload,
        )
        if not applied:
            return True, False
        payment_user_id = int(payment["user_id"])
        primary = await asyncio.to_thread(
            self.db.get_primary_client_peer, payment_user_id
        )
        if primary:
            sync_result = await self.cascade_router.sync_user_access(
                payment_user_id, applied["expire_date"]
            )
            if sync_result["failed"]:
                await asyncio.to_thread(
                    self.db.add_provisioning_task,
                    payment_user_id,
                    "sync_access",
                    {"expire_date": applied["expire_date"]},
                    f"Failed peers: {sync_result['failed']}",
                )
        else:
            await asyncio.to_thread(
                self.db.add_provisioning_task,
                payment_user_id,
                "create_peer",
                {
                    "username": metadata.get("username") or "",
                    "peer_name": generate_peer_name(
                        metadata.get("username") or None, payment_user_id
                    ),
                    "expire_date": applied["expire_date"],
                    "tariff_key": payment["tariff_key"],
                },
                "Recovered by Telegram Stars reconciliation",
            )
        await asyncio.to_thread(
            self.db.update_star_transaction_match,
            transaction_id,
            direction,
            payment["payment_id"],
            "applied",
        )
        await asyncio.to_thread(
            self.db.log_operation,
            f"telegram:{payment_user_id}",
            "stars_reconciliation_applied",
            f"payment_id={payment['payment_id']}",
        )
        return True, True

    def format_report(self, result: ReconciliationResult, *, daily: bool = False) -> str:
        title = "📊 Ежедневный отчет Telegram Stars" if daily else "📊 Сверка Telegram Stars"
        if daily:
            summary = self.db.get_star_daily_summary()
            result = ReconciliationResult(**summary)
        return (
            f"{title}\n\n"
            f"Новых операций: {result.observed}\n"
            f"Получено: {result.received_stars} Stars\n"
            f"Возвращено: {result.refunded_stars} Stars\n"
            f"Восстановлено платежей: {result.applied}\n"
            f"Требуют проверки: {result.discrepancies}"
        )
