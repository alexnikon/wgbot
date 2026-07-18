import unittest
from unittest.mock import AsyncMock, Mock

from runtime_metrics import RuntimeMetrics
from services import AppServices


class AppServicesTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_is_idempotent_and_marks_runtime_not_ready(self):
        database = Mock()
        router = Mock()
        router.close = AsyncMock()
        yookassa = Mock()
        yookassa.aclose = AsyncMock()
        services = AppServices(
            db=database,
            cascade_router=router,
            yookassa_client=yookassa,
            metrics=RuntimeMetrics(),
            runtime_ready=True,
        )

        await services.close()
        await services.close()

        self.assertFalse(services.runtime_ready)
        yookassa.aclose.assert_awaited_once()
        router.close.assert_awaited_once()
