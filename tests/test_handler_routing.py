import unittest

import bot


class HandlerRoutingTests(unittest.TestCase):
    def test_feature_routers_precede_fallback_router(self):
        self.assertEqual(
            [router.name for router in bot.dp.sub_routers],
            ["admin", "navigation", "access", "payments", "fallback"],
        )

    def test_dispatcher_has_no_parent_message_fallback(self):
        self.assertEqual(bot.dp.message.handlers, [])
