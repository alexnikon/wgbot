import json
import unittest
from pathlib import Path

DASHBOARD_PATH = Path(__file__).parent.parent / "grafana" / "VPNService.json"
DATASOURCE_NAME = "bf0o2zpwh26tce"


class GrafanaDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))

    def test_dashboard_uses_schema_v2_layout_without_orphaned_elements(self):
        self.assertEqual(self.dashboard["title"], "VPNService")
        self.assertEqual(self.dashboard["layout"]["kind"], "RowsLayout")
        element_names = set(self.dashboard["elements"])
        layout_names = []
        for row in self.dashboard["layout"]["spec"]["rows"]:
            for item in row["spec"]["layout"]["spec"]["items"]:
                layout_names.append(item["spec"]["element"]["name"])
        self.assertEqual(set(layout_names), element_names)
        self.assertEqual(len(layout_names), len(set(layout_names)))

    def test_financial_stats_follow_dashboard_time_range(self):
        expected = {
            "Payment count": (
                'sum(increase(wgbot_payments_completed_total{method=~"$method",'
                'tariff=~"$tariff"}[$__range])) or vector(0)'
            ),
            "Refund count": (
                'sum(increase(wgbot_refunds_total{method=~"$method",'
                'tariff=~"$tariff"}[$__range])) or vector(0)'
            ),
            "Payments RUB": (
                "sum(increase(wgbot_yookassa_received_rubles_total[$__range])) "
                "or vector(0)"
            ),
            "Payments Stars": (
                "sum(increase(wgbot_stars_received_total[$__range])) or vector(0)"
            ),
            "Refunds RUB": (
                "sum(increase(wgbot_yookassa_refunded_rubles_total[$__range])) "
                "or vector(0)"
            ),
            "Refunds Stars": (
                "sum(increase(wgbot_stars_refunded_total[$__range])) or vector(0)"
            ),
        }
        panels = {
            panel["spec"]["title"]: panel
            for panel in self.dashboard["elements"].values()
        }
        for title, expression in expected.items():
            with self.subTest(title=title):
                panel = panels[title]
                query = panel["spec"]["data"]["spec"]["queries"][0]["spec"][
                    "query"
                ]["spec"]
                self.assertEqual(query["expr"], expression)
                self.assertTrue(query["instant"])
                self.assertFalse(query["range"])
                self.assertEqual(panel["spec"]["vizConfig"]["group"], "stat")

    def test_access_names_and_active_configuration_filter_are_clear(self):
        panels = {
            panel["spec"]["title"]: panel
            for panel in self.dashboard["elements"].values()
        }
        self.assertIn("Free access", panels)
        self.assertIn("Active paid access", panels)
        self.assertNotIn("Complimentary access", panels)
        active_access = panels["Active paid access"]["spec"]
        self.assertIn("not blocked", active_access["description"])
        active_configs = panels["Active configurations"]["spec"]
        expression = active_configs["data"]["spec"]["queries"][0]["spec"][
            "query"
        ]["spec"]["expr"]
        self.assertEqual(
            expression,
            'sum(wgbot_server_configs{server_key=~"$server",state="enabled"}) '
            "or vector(0)",
        )

    def test_all_prometheus_queries_keep_existing_datasource(self):
        for element_name, panel in self.dashboard["elements"].items():
            queries = panel["spec"]["data"]["spec"].get("queries", [])
            for query in queries:
                with self.subTest(element=element_name, ref_id=query["spec"]["refId"]):
                    datasource = query["spec"]["query"]["datasource"]
                    self.assertEqual(datasource["name"], DATASOURCE_NAME)


if __name__ == "__main__":
    unittest.main()
