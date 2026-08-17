import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
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

    async def post_yookassa_webhook(self, client, database, process):
        with (
            patch.object(webhook_server, "db", database),
            patch.object(webhook_server, "yookassa_client", client, create=True),
            patch.object(webhook_server, "process_successful_payment", process),
        ):
            transport = httpx.ASGITransport(app=webhook_server.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http_client:
                return await http_client.post(
                    "/webhook/yookassa",
                    json={
                        "type": "notification",
                        "event": "payment.succeeded",
                        "object": {"id": "payment-1"},
                    },
                )

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

    async def test_yookassa_webhook_accepts_payment_without_metadata(self):
        payment = {
            "id": "payment-1",
            "status": "succeeded",
            "amount": {"value": "150.00", "currency": "RUB"},
        }
        database = Mock()
        database.get_payment_by_id.return_value = {
            "payment_id": "payment-1",
            "user_id": 10,
            "amount": 15000,
            "currency": "RUB",
            "payment_method": "yookassa",
            "tariff_key": "14_days",
        }
        client = SimpleNamespace(
            parse_webhook=lambda body: json.loads(body),
            get_payment=AsyncMock(return_value=payment),
            get_payment_amount=lambda _data: 15000,
        )
        process = AsyncMock()

        response = await self.post_yookassa_webhook(client, database, process)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        process.assert_awaited_once_with(payment)

    async def test_yookassa_webhook_rejects_local_payment_mismatches(self):
        cases = (
            ("amount", {"amount": 14900}),
            ("currency", {"currency": "USD"}),
            ("payment method", {"payment_method": "stars"}),
        )
        for label, override in cases:
            with self.subTest(label=label):
                payment = {
                    "id": "payment-1",
                    "status": "succeeded",
                    "amount": {"value": "150.00", "currency": "RUB"},
                }
                database = Mock()
                database.get_payment_by_id.return_value = {
                    "payment_id": "payment-1",
                    "user_id": 10,
                    "amount": 15000,
                    "currency": "RUB",
                    "payment_method": "yookassa",
                    "tariff_key": "14_days",
                    **override,
                }
                client = SimpleNamespace(
                    parse_webhook=lambda body: json.loads(body),
                    get_payment=AsyncMock(return_value=payment),
                    get_payment_amount=lambda _data: 15000,
                )
                process = AsyncMock()

                response = await self.post_yookassa_webhook(
                    client, database, process
                )

                self.assertEqual(response.json(), {"status": "ignored"})
                process.assert_not_awaited()

    async def test_yookassa_webhook_rejects_unknown_payment_and_status_mismatch(self):
        cases = (
            ("unknown payment", "succeeded", None),
            ("status mismatch", "pending", {"payment_method": "yookassa"}),
        )
        for label, provider_status, local_payment in cases:
            with self.subTest(label=label):
                payment = {
                    "id": "payment-1",
                    "status": provider_status,
                    "amount": {"value": "150.00", "currency": "RUB"},
                }
                database = Mock()
                database.get_payment_by_id.return_value = local_payment
                client = SimpleNamespace(
                    parse_webhook=lambda body: json.loads(body),
                    get_payment=AsyncMock(return_value=payment),
                    get_payment_amount=lambda _data: 15000,
                )
                process = AsyncMock()

                response = await self.post_yookassa_webhook(
                    client, database, process
                )

                self.assertEqual(response.json(), {"status": "ignored"})
                process.assert_not_awaited()

    async def test_yookassa_extension_uses_unified_payment_message(self):
        database = Mock()
        database.get_payment_by_id.return_value = {
            "payment_id": "payment-1",
            "user_id": 10,
            "tariff_key": "14_days",
            "metadata": "{}",
        }
        database.apply_verified_payment.return_value = {
            "expire_date": "2099-01-01 00:00:00",
            "is_extension": True,
        }
        cascade_router = SimpleNamespace(
            sync_user_access=AsyncMock(return_value={"failed": 0})
        )
        send_message = AsyncMock(return_value=True)
        notify = AsyncMock()
        payment_data = {
            "id": "payment-1",
            "amount": {"value": "150.00", "currency": "RUB"},
        }

        with (
            patch.object(webhook_server, "db", database),
            patch.object(webhook_server, "cascade_router", cascade_router, create=True),
            patch.object(
                webhook_server,
                "yookassa_client",
                SimpleNamespace(get_payment_amount=lambda _data: 15000),
                create=True,
            ),
            patch.object(webhook_server, "send_telegram_message", send_message),
            patch.object(webhook_server, "notify_admins", notify),
            patch.object(webhook_server, "delete_payment_message", AsyncMock()),
            patch.object(
                webhook_server,
                "get_tariffs",
                return_value={
                    "14_days": {
                        "days": 14,
                        "name": "2 недели",
                    }
                },
            ),
        ):
            await webhook_server.process_successful_payment(payment_data)

        content = send_message.await_args_list[0].args[1]
        self.assertTrue(content.plain.startswith("✅ Оплачено!"))
        self.assertIn("продлен на 2 недели", content.plain)
        self.assertIn("📅 Осталось:", content.plain)
        cascade_router.sync_user_access.assert_awaited_once()

    async def test_banned_yookassa_payment_extends_without_user_reply_or_provisioning(self):
        database = Mock()
        database.get_payment_by_id.return_value = {
            "payment_id": "payment-1",
            "user_id": 10,
            "tariff_key": "14_days",
            "metadata": "{}",
        }
        database.apply_verified_payment.return_value = {
            "expire_date": "2099-01-01 00:00:00"
        }
        database.is_client_banned.return_value = True
        cascade_router = SimpleNamespace(
            sync_client_state=AsyncMock(
                return_value={"updated": 1, "missing": 0, "failed": 0}
            )
        )
        send_message = AsyncMock(return_value=True)
        notify = AsyncMock()
        payment_data = {
            "id": "payment-1",
            "amount": {"value": "150.00", "currency": "RUB"},
        }

        with (
            patch.object(webhook_server, "db", database),
            patch.object(webhook_server, "cascade_router", cascade_router, create=True),
            patch.object(
                webhook_server,
                "yookassa_client",
                SimpleNamespace(get_payment_amount=lambda _data: 15000),
                create=True,
            ),
            patch.object(webhook_server, "send_telegram_message", send_message),
            patch.object(webhook_server, "notify_admins", notify),
            patch.object(webhook_server, "delete_payment_message", AsyncMock()),
            patch.object(
                webhook_server,
                "get_tariffs",
                return_value={"14_days": {"days": 14, "name": "2 недели"}},
            ),
        ):
            await webhook_server.process_successful_payment(payment_data)

        database.apply_verified_payment.assert_called_once()
        send_message.assert_not_awaited()
        cascade_router.sync_client_state.assert_awaited_once_with(10)
        notify.assert_awaited_once()

    async def test_yookassa_first_payment_keeps_config_separate(self):
        database = Mock()
        database.get_payment_by_id.return_value = {
            "payment_id": "payment-1",
            "user_id": 10,
            "tariff_key": "30_days",
            "metadata": "{}",
        }
        database.apply_verified_payment.return_value = {
            "expire_date": "2099-01-01 00:00:00",
            "is_extension": False,
        }
        database.count_managed_configs.return_value = 0
        cascade_router = SimpleNamespace(
            sync_user_access=AsyncMock(
                return_value={"total": 0, "updated": 0, "missing": 0, "failed": 0}
            )
        )
        send_message = AsyncMock(return_value=True)
        payment_data = {
            "id": "payment-1",
            "amount": {"value": "300.00", "currency": "RUB"},
        }

        with (
            patch.object(webhook_server, "db", database),
            patch.object(webhook_server, "cascade_router", cascade_router, create=True),
            patch.object(
                webhook_server,
                "yookassa_client",
                SimpleNamespace(get_payment_amount=lambda _data: 30000),
                create=True,
            ),
            patch.object(webhook_server, "send_telegram_message", send_message),
            patch.object(webhook_server, "notify_admins", AsyncMock()),
            patch.object(webhook_server, "delete_payment_message", AsyncMock()),
            patch.object(
                webhook_server,
                "get_tariffs",
                return_value={
                    "30_days": {
                        "days": 30,
                        "name": "1 месяц",
                    }
                },
            ),
        ):
            await webhook_server.process_successful_payment(payment_data)

        self.assertTrue(send_message.await_args_list[0].args[1].plain.startswith("✅ Оплачено!"))
        self.assertEqual(len(send_message.await_args_list), 1)
        self.assertEqual(
            send_message.await_args_list[0].args[2]["inline_keyboard"][0][0]["text"],
            "📥 Создать файл конфигурации",
        )
        self.assertEqual(
            send_message.await_args_list[0].args[2]["inline_keyboard"][1],
            [{"text": "На главную", "callback_data": "main"}],
        )
        database.add_provisioning_task.assert_not_called()

    async def test_paid_access_with_config_also_has_home_button(self):
        database = Mock()
        database.count_managed_configs.return_value = 1

        with patch.object(webhook_server, "db", database):
            markup = webhook_server.create_access_reply_markup(10)

        self.assertEqual(
            markup["inline_keyboard"],
            [
                [{"text": "📥 Файлы конфигурации", "callback_data": "get_config"}],
                [{"text": "На главную", "callback_data": "main"}],
            ],
        )

    async def test_refund_webhook_reports_inactive_subscription_once(self):
        payment = {
            "payment_id": "payment-1",
            "user_id": 10,
            "amount": 15000,
            "status": "succeeded",
            "tariff_key": "14_days",
        }
        refunded_payment = {**payment, "status": "refunded"}
        database = Mock()
        database.get_payment_by_id.side_effect = [payment, refunded_payment]
        database.apply_refund.return_value = SimpleNamespace(
            user_id=10, expire_date="2000-01-01 00:00:00", applied=True
        )
        cascade_router = SimpleNamespace(
            sync_user_access=AsyncMock(return_value={"failed": 0})
        )
        yookassa_client = SimpleNamespace(get_payment_amount=lambda _data: 15000)
        send_message = AsyncMock()
        refund_data = {
            "payment_id": "payment-1",
            "amount": {"value": "150.00", "currency": "RUB"},
        }

        with (
            patch.object(webhook_server, "db", database),
            patch.object(webhook_server, "cascade_router", cascade_router, create=True),
            patch.object(
                webhook_server,
                "yookassa_client",
                yookassa_client,
                create=True,
            ),
            patch.object(webhook_server, "send_telegram_message", send_message),
            patch.object(
                webhook_server,
                "get_tariffs",
                return_value={"14_days": {"days": 14, "name": "2 недели"}},
            ),
        ):
            await webhook_server.process_refund_succeeded(refund_data)
            await webhook_server.process_refund_succeeded(refund_data)

        database.apply_refund.assert_called_once_with("payment-1", 14)
        cascade_router.sync_user_access.assert_awaited_once_with(
            10,
            "2000-01-01 00:00:00",
        )
        send_message.assert_awaited_once()
        content = send_message.await_args.args[1]
        self.assertTrue(
            content.plain.endswith("📅 Осталось: подписка не активна.")
        )
        self.assertIn("уменьшен на 2 недели", content.plain)

    async def test_partial_refund_still_requires_manual_adjustment(self):
        database = Mock()
        database.get_payment_by_id.return_value = {
            "payment_id": "payment-1",
            "user_id": 10,
            "amount": 15000,
            "status": "succeeded",
            "tariff_key": "14_days",
        }
        yookassa_client = SimpleNamespace(get_payment_amount=lambda _data: 5000)
        notify = AsyncMock()
        send_message = AsyncMock()
        refund_data = {
            "payment_id": "payment-1",
            "amount": {"value": "50.00", "currency": "RUB"},
        }

        with (
            patch.object(webhook_server, "db", database),
            patch.object(
                webhook_server,
                "yookassa_client",
                yookassa_client,
                create=True,
            ),
            patch.object(webhook_server, "notify_admins", notify),
            patch.object(webhook_server, "send_telegram_message", send_message),
        ):
            await webhook_server.process_refund_succeeded(refund_data)

        database.apply_refund.assert_not_called()
        notify.assert_awaited_once()
        send_message.assert_not_awaited()
