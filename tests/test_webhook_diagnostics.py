import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_config_document_uses_location_filename(self):
        client = SimpleNamespace(
            post=AsyncMock(return_value=SimpleNamespace(is_success=True))
        )
        with patch.object(
            webhook_server, "get_telegram_http_client", return_value=client
        ):
            sent = await webhook_server.send_config_with_confirmation(
                10,
                b"config",
                filename="USA-NY.conf",
            )

        self.assertTrue(sent)
        self.assertEqual(
            client.post.await_args.kwargs["files"]["document"][0],
            "USA-NY.conf",
        )
        data = client.post.await_args.kwargs["data"]
        self.assertEqual(data["parse_mode"], "HTML")
        self.assertIn("только на одном устройстве", data["caption"])

    async def test_authored_webhook_message_uses_rich_message(self):
        client = SimpleNamespace(
            post=AsyncMock(return_value=SimpleNamespace(is_success=True))
        )
        with patch.object(
            webhook_server, "get_telegram_http_client", return_value=client
        ):
            sent = await webhook_server.send_telegram_message(
                10,
                "✅ Платеж обработан.\n💰 Стоимость: 250 руб.",
            )

        self.assertTrue(sent)
        self.assertTrue(client.post.await_args.args[0].endswith("/sendRichMessage"))
        rich_html = client.post.await_args.kwargs["json"]["rich_message"]["html"]
        self.assertIn("<b>✅ Платеж обработан.</b>", rich_html)
        self.assertIn("<code>250</code> руб.", rich_html)
