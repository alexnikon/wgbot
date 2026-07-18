import asyncio
from dataclasses import dataclass, field

from cascade_api import CascadeRouter
from database import Database
from runtime_metrics import RuntimeMetrics
from yookassa_client import YooKassaClient


@dataclass
class AppServices:
    """Own application services and their shared lifecycle."""

    db: Database
    cascade_router: CascadeRouter
    yookassa_client: YooKassaClient
    metrics: RuntimeMetrics
    runtime_ready: bool = False
    _closed: bool = False
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self.runtime_ready = False
            self._closed = True
            await self.yookassa_client.aclose()
            await self.cascade_router.close()


def create_services() -> AppServices:
    """Create services explicitly during application startup."""
    metrics = RuntimeMetrics()
    db = Database()
    cascade_router = CascadeRouter(db, metrics=metrics)
    return AppServices(
        db=db,
        cascade_router=cascade_router,
        yookassa_client=YooKassaClient(),
        metrics=metrics,
    )
