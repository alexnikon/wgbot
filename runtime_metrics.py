import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)


class RuntimeMetrics:
    """Small in-process metrics registry for operational diagnostics."""

    def __init__(self, database: Any | None = None) -> None:
        self.started_at = datetime.now(UTC)
        self._lock = threading.Lock()
        self._cascade: dict[str, dict[str, float | int]] = {}
        self._provisioning = {"claimed": 0, "completed": 0, "failed": 0}
        self._telegram = {
            "legacy_callbacks": 0,
            "unhandled_errors": 0,
            "active_handlers": 0,
            "peak_handlers": 0,
            "saturation_events": 0,
        }
        self._database = database
        self._telegram_gauge_provider: Callable[[], dict[str, int]] | None = None
        self._last_provisioning_error: dict[str, Any] | None = None

    @staticmethod
    def timer() -> float:
        return monotonic()

    def record_cascade(self, server_key: str, elapsed: float, success: bool) -> None:
        with self._lock:
            server = self._cascade.setdefault(
                server_key,
                {"requests": 0, "errors": 0, "duration_seconds": 0.0},
            )
            server["requests"] += 1
            server["duration_seconds"] += elapsed
            if not success:
                server["errors"] += 1

    def provisioning_claimed(self, count: int) -> None:
        with self._lock:
            self._provisioning["claimed"] += count

    def provisioning_completed(self) -> None:
        with self._lock:
            self._provisioning["completed"] += 1

    def provisioning_failed(self, task_id: str, error: Exception) -> None:
        with self._lock:
            self._provisioning["failed"] += 1
            self._last_provisioning_error = {
                "task_id": task_id,
                "error_type": type(error).__name__,
                "at": datetime.now(UTC).isoformat(),
            }

    def telegram_event(self, name: str) -> None:
        with self._lock:
            if name not in self._telegram:
                self._telegram[name] = 0
            self._telegram[name] += 1
        if self._database is not None and name in {
            "legacy_callbacks",
            "unhandled_errors",
        }:
            try:
                self._database.record_telegram_daily_metric(name)
            except Exception as exc:
                logger.warning(
                    "Failed to persist Telegram metric %s: %s",
                    name,
                    type(exc).__name__,
                )

    def telegram_handler_started(self, concurrency_limit: int) -> None:
        with self._lock:
            self._telegram["active_handlers"] += 1
            self._telegram["peak_handlers"] = max(
                self._telegram["peak_handlers"], self._telegram["active_handlers"]
            )
            if self._telegram["active_handlers"] >= concurrency_limit:
                self._telegram["saturation_events"] += 1

    def telegram_handler_finished(self) -> None:
        with self._lock:
            self._telegram["active_handlers"] = max(
                0, self._telegram["active_handlers"] - 1
            )

    def set_telegram_gauge_provider(
        self, provider: Callable[[], dict[str, int]]
    ) -> None:
        self._telegram_gauge_provider = provider

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cascade = {}
            for server_key, values in self._cascade.items():
                requests = int(values["requests"])
                duration = float(values["duration_seconds"])
                cascade[server_key] = {
                    "requests": requests,
                    "errors": int(values["errors"]),
                    "average_duration_seconds": round(duration / requests, 4)
                    if requests
                    else 0.0,
                }
            result = {
                "started_at": self.started_at.isoformat(),
                "cascade": cascade,
                "provisioning": dict(self._provisioning),
                "telegram": dict(self._telegram),
                "last_provisioning_error": dict(self._last_provisioning_error)
                if self._last_provisioning_error
                else None,
            }
        if self._telegram_gauge_provider is not None:
            result["telegram"].update(self._telegram_gauge_provider())
        return result
