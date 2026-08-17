import json
import unittest

import httpx

from yookassa_client import YooKassaClient, YooKassaNotFound, YooKassaUnavailable


class YooKassaVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        client = getattr(self, "client", None)
        if client is not None:
            await client.aclose()

    async def test_transient_api_error_is_not_treated_as_missing_payment(self):
        self.client = YooKassaClient()
        self.client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, request=request)
            )
        )
        with self.assertRaises(YooKassaUnavailable):
            await self.client.get_payment("payment-1")

    async def test_missing_payment_has_distinct_error(self):
        self.client = YooKassaClient()
        self.client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(404, request=request)
            )
        )
        with self.assertRaises(YooKassaNotFound):
            await self.client.get_payment("payment-1")

    async def test_create_payment_does_not_send_customer_metadata(self):
        captured_body = None

        def handle_request(request: httpx.Request) -> httpx.Response:
            nonlocal captured_body
            captured_body = json.loads(request.content)
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "payment-1",
                    "status": "pending",
                    "confirmation": {"confirmation_url": "https://example.test/pay"},
                },
            )

        self.client = YooKassaClient()
        self.client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        )

        result = await self.client.create_payment(
            amount=15000,
            currency="RUB",
            description="Service access for 2 weeks",
            return_url="https://example.test/return",
        )

        self.assertEqual(result["id"], "payment-1")
        self.assertNotIn("metadata", captured_body)


if __name__ == "__main__":
    unittest.main()
