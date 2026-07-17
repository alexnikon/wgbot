import unittest

from utils import generate_peer_name


class PeerNameTests(unittest.TestCase):
    def test_username_is_used_without_telegram_id(self):
        self.assertEqual(generate_peer_name("irina_071090", 1009866772), "irina_071090")

    def test_telegram_id_is_used_when_username_is_missing(self):
        self.assertEqual(generate_peer_name(None, 1009866772), "1009866772")


if __name__ == "__main__":
    unittest.main()
