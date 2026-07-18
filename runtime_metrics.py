import threading
from datetime import UTC, datetime
from time import monotonic
from typing import Any


class RuntimeMetrics:
    """Small in-process metrics registry for operational diagnostics."""

    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self._lock = threading.Lock()
        self._cascade: dict[str, dict[str, float | int]] = {}
        self._provisioning = {"claimed": 0, "completed": 0, "failed": 0}
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
            return {
                "started_at": self.started_at.isoformat(),
                "cascade": cascade,
                "provisioning": dict(self._provisioning),
                "last_provisioning_error": dict(self._last_provisioning_error)
                if self._last_provisioning_error
                else None,
            }
