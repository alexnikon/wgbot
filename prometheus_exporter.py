import asyncio
import logging
from collections.abc import Iterable
from time import monotonic
from typing import Any

from fastapi import FastAPI
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from starlette.responses import Response

logger = logging.getLogger(__name__)


class WGBotCollector:
    """Collect low-cardinality financial and operational WGBot metrics."""

    def __init__(self, services: Any) -> None:
        self._services = services

    @staticmethod
    def _gauge(name: str, documentation: str, value: float) -> GaugeMetricFamily:
        metric = GaugeMetricFamily(name, documentation)
        metric.add_metric([], value)
        return metric

    def _database_metrics(self, snapshot: dict[str, Any]) -> Iterable[Any]:
        clients = snapshot["clients"]
        yield self._gauge(
            "wgbot_paid_clients",
            "Clients with a currently valid paid subscription period.",
            clients["paid"],
        )
        access = GaugeMetricFamily(
            "wgbot_access_clients",
            "Clients with effective access by access type.",
            labels=["type"],
        )
        access.add_metric(["paid"], clients["paid_access"])
        access.add_metric(["complimentary"], clients["complimentary_access"])
        yield access
        yield self._gauge(
            "wgbot_paid_clients_blocked",
            "Clients with paid time remaining but blocked or unverified access.",
            clients["paid_blocked"],
        )
        yield self._gauge(
            "wgbot_active_clients_without_config",
            "Clients with effective access and no bound managed configuration.",
            clients["active_without_config"],
        )

        payment_records = GaugeMetricFamily(
            "wgbot_payment_records",
            "Current payment records by method and status.",
            labels=["method", "status"],
        )
        for row in snapshot["payment_records"]:
            payment_records.add_metric(
                [str(row["method"]), str(row["status"])], int(row["count"])
            )
        yield payment_records

        completed = CounterMetricFamily(
            "wgbot_payments_completed",
            "Payments successfully applied, including payments later refunded.",
            labels=["method", "tariff"],
        )
        for row in snapshot["completed_payments"]:
            completed.add_metric(
                [str(row["method"]), str(row["tariff"])], int(row["count"])
            )
        yield completed

        amounts = snapshot["amounts"]
        for name, documentation, key in (
            (
                "wgbot_yookassa_received_rubles",
                "Gross rubles received through YooKassa.",
                "yookassa_received",
            ),
            (
                "wgbot_yookassa_refunded_rubles",
                "Rubles refunded through YooKassa.",
                "yookassa_refunded",
            ),
            ("wgbot_stars_received", "Gross Telegram Stars received.", "stars_received"),
            ("wgbot_stars_refunded", "Telegram Stars refunded.", "stars_refunded"),
        ):
            metric = CounterMetricFamily(name, documentation)
            metric.add_metric([], amounts[key])
            yield metric

        expiring = GaugeMetricFamily(
            "wgbot_access_expiring_clients",
            "Effective paid clients expiring within the cumulative day window.",
            labels=["within_days"],
        )
        for days, value in snapshot["expiry"]["windows"].items():
            expiring.add_metric([str(days)], value)
        yield expiring
        if snapshot["expiry"]["nearest"] is not None:
            yield self._gauge(
                "wgbot_access_nearest_expiry_timestamp_seconds",
                "Unix timestamp of the nearest effective paid access expiry.",
                snapshot["expiry"]["nearest"],
            )

        server_clients = GaugeMetricFamily(
            "wgbot_server_clients",
            "Distinct managed configuration owners on each server; a client on multiple servers is counted on each.",
            labels=["server_key", "access"],
        )
        for row in snapshot["server_clients"]:
            server_clients.add_metric(
                [str(row["server_key"]), str(row["access"])], int(row["count"])
            )
        yield server_clients
        server_configs = GaugeMetricFamily(
            "wgbot_server_configs",
            "Bound managed configurations on each server by effective configuration state.",
            labels=["server_key", "state"],
        )
        for row in snapshot["server_configs"]:
            server_configs.add_metric(
                [str(row["server_key"]), str(row["state"])], int(row["count"])
            )
        yield server_configs

        provisioning = GaugeMetricFamily(
            "wgbot_provisioning_tasks",
            "Current durable provisioning tasks by status.",
            labels=["status"],
        )
        for row in snapshot["provisioning"]:
            provisioning.add_metric([str(row["status"])], int(row["count"]))
        yield provisioning

        operational = snapshot["operational"]
        reachability = GaugeMetricFamily(
            "wgbot_telegram_clients",
            "Clients by Telegram reachability state.",
            labels=["reachability"],
        )
        for label, key in (
            ("reachable", "telegram_reachable"),
            ("blocked", "telegram_blocked"),
            ("unknown", "telegram_unknown"),
        ):
            reachability.add_metric([label], operational[key])
        yield reachability
        yield self._gauge(
            "wgbot_stars_discrepancies",
            "Unresolved Telegram Stars reconciliation discrepancies.",
            operational["stars_discrepancies"],
        )
        if operational["stars_last_success_age_seconds"] is not None:
            yield self._gauge(
                "wgbot_stars_last_success_age_seconds",
                "Age of the latest successful Telegram Stars reconciliation.",
                operational["stars_last_success_age_seconds"],
            )

    def _runtime_metrics(self, snapshot: dict[str, Any]) -> Iterable[Any]:
        yield self._gauge(
            "wgbot_ready",
            "Whether all WGBot runtimes completed startup.",
            int(self._services.runtime_ready),
        )
        yield self._gauge(
            "wgbot_start_time_seconds",
            "Unix timestamp when the WGBot process services started.",
            self._services.metrics.started_at.timestamp(),
        )

        cascade_requests = CounterMetricFamily(
            "wgbot_cascade_requests",
            "Cascade API requests since process start by server and result.",
            labels=["server_key", "result"],
        )
        cascade_duration = CounterMetricFamily(
            "wgbot_cascade_request_duration_seconds",
            "Total Cascade API request duration since process start by server.",
            labels=["server_key"],
        )
        for server_key, values in snapshot["cascade"].items():
            requests = int(values["requests"])
            errors = int(values["errors"])
            cascade_requests.add_metric([server_key, "success"], requests - errors)
            cascade_requests.add_metric([server_key, "error"], errors)
            cascade_duration.add_metric([server_key], values["duration_seconds"])
        yield cascade_requests
        yield cascade_duration

        provisioning = snapshot["provisioning"]
        for event in ("claimed", "completed", "failed"):
            metric = CounterMetricFamily(
                f"wgbot_provisioning_{event}",
                f"Provisioning tasks {event} since process start.",
            )
            metric.add_metric([], provisioning[event])
            yield metric

        telegram = snapshot["telegram"]
        for name, documentation in (
            ("active_handlers", "Current Telegram event handlers."),
            ("peak_handlers", "Peak concurrent Telegram event handlers."),
        ):
            yield self._gauge(f"wgbot_telegram_{name}", documentation, telegram[name])
        for name, documentation in (
            ("saturation_events", "Telegram handler saturation events since start."),
            ("unhandled_errors", "Unhandled Telegram errors since start."),
            ("legacy_callbacks", "Legacy Telegram callbacks since start."),
        ):
            metric = CounterMetricFamily(f"wgbot_telegram_{name}", documentation)
            metric.add_metric([], telegram[name])
            yield metric

    def collect(self) -> Iterable[Any]:
        started = monotonic()
        success = 1
        try:
            database_snapshot = self._services.db.get_prometheus_metrics_snapshot()
            runtime_snapshot = self._services.metrics.snapshot()
            yield from self._database_metrics(database_snapshot)
            yield from self._runtime_metrics(runtime_snapshot)
        except Exception as exc:
            success = 0
            logger.warning(
                "Prometheus metric collection failed: %s", type(exc).__name__
            )
        yield self._gauge(
            "wgbot_metrics_collection_success",
            "Whether database and runtime metric collection succeeded.",
            success,
        )
        yield self._gauge(
            "wgbot_metrics_collection_duration_seconds",
            "Duration of the latest metric collection.",
            monotonic() - started,
        )


def create_metrics_app(services: Any) -> FastAPI:
    """Create an isolated Prometheus exposition application."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(WGBotCollector(services))
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/metrics")
    async def metrics() -> Response:
        payload = await asyncio.to_thread(generate_latest, registry)
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    return app
