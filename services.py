import asyncio

from cascade_api import CascadeRouter
from database import Database
from yookassa_client import YooKassaClient


db = Database()
cascade_router = CascadeRouter(db)
yookassa_client = YooKassaClient()
_close_lock = asyncio.Lock()
_services_closed = False
_runtime_ready = False


def set_runtime_ready(value: bool) -> None:
    global _runtime_ready
    _runtime_ready = value


def is_runtime_ready() -> bool:
    return _runtime_ready


async def close_shared_services() -> None:
    """Close shared clients used across bot polling and webhook processing."""
    global _services_closed, _runtime_ready
    async with _close_lock:
        if _services_closed:
            return
        _runtime_ready = False
        _services_closed = True
        await yookassa_client.aclose()
        await cascade_router.close()
