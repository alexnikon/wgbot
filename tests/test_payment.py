import unittest
from types import SimpleNamespace

from payment import PaymentManager


class PaymentSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_invoice_payload_parser_binds_owner(self):
        self.assertEqual(
            PaymentManager.parse_invoice_payload("vpn_access_stars_14_days_123"),
            ("stars", "14_days", 123),
        )
        self.assertIsNone(PaymentManager.parse_invoice_payload("vpn_access_stars_bad_123"))

    async def test_successful_payment_rejects_different_payer(self):
        manager = PaymentManager.__new__(PaymentManager)
        payment = SimpleNamespace(
            invoice_payload="vpn_access_stars_14_days_123", total_amount=50
        )
        confirmed, payment_type, amount = await manager.confirm_payment(
            payment, payer_user_id=456
        )
        self.assertFalse(confirmed)
        self.assertEqual(payment_type, "")
        self.assertEqual(amount, 0)


if __name__ == "__main__":
    unittest.main()
