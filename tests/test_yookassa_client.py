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


if __name__ == "__main__":
    unittest.main()
