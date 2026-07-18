import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from starlette.requests import Request

import webhook_server
from runtime_metrics import RuntimeMetrics


def request_with_authorization(value: str = "") -> Request:
    headers = [(b"authorization", value.encode())] if value else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


class WebhookDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_services = webhook_server.app_services
        self.original_database = getattr(webhook_server, "db", None)
        database = Mock()
        database.get_runtime_stats.return_value = {"clients": 2}
        webhook_server.db = database
        webhook_server.app_services = SimpleNamespace(
            runtime_ready=True,
            metrics=RuntimeMetrics(),
        )

    async def asyncTearDown(self):
        webhook_server.app_services = self.original_services
        if self.original_database is not None:
            webhook_server.db = self.original_database
        else:
            del webhook_server.db

    async def test_metrics_endpoint_is_disabled_without_token(self):
        with patch.object(webhook_server, "INTERNAL_METRICS_TOKEN", ""):
            response = await webhook_server.runtime_metrics(request_with_authorization())
        self.assertEqual(response.status_code, 404)

    async def test_metrics_endpoint_rejects_invalid_token(self):
        with patch.object(webhook_server, "INTERNAL_METRICS_TOKEN", "expected"):
            response = await webhook_server.runtime_metrics(
                request_with_authorization("Bearer wrong")
            )
        self.assertEqual(response.status_code, 401)

    async def test_metrics_endpoint_returns_runtime_and_database_gauges(self):
        with patch.object(webhook_server, "INTERNAL_METRICS_TOKEN", "expected"):
            response = await webhook_server.runtime_metrics(
                request_with_authorization("Bearer expected")
            )
        payload = response
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["database"]["clients"], 2)
