import unittest

from utils import generate_peer_name, location_config_filename


class PeerNameTests(unittest.TestCase):
    def test_username_is_used_without_telegram_id(self):
        self.assertEqual(generate_peer_name("irina_071090", 1009866772), "irina_071090")

    def test_telegram_id_is_used_when_username_is_missing(self):
        self.assertEqual(generate_peer_name(None, 1009866772), "1009866772")

    def test_location_filename_uses_hyphens_and_removes_punctuation(self):
        self.assertEqual(location_config_filename("USA NY"), "USA-NY.conf")
        self.assertEqual(
            location_config_filename("Finland / Helsinki"),
            "Finland-Helsinki.conf",
        )


if __name__ == "__main__":
    unittest.main()
