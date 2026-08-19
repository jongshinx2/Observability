import unittest
from pathlib import Path

import yaml


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent


class ObservabilityConfigTests(unittest.TestCase):
    def test_collector_routes_otlp_logs_to_loki(self):
        config = yaml.safe_load(
            (BACKEND_DIR / "docker" / "otel-collector.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            config["exporters"]["otlp_http/loki"]["endpoint"],
            "http://loki:3100/otlp",
        )
        self.assertEqual(
            config["service"]["pipelines"]["logs"],
            {
                "receivers": ["otlp"],
                "processors": ["batch"],
                "exporters": ["otlp_http/loki"],
            },
        )

    def test_loki_examples_use_normalized_service_name_label(self):
        config = yaml.safe_load(
            (BACKEND_DIR / "datasource_config.yaml").read_text(encoding="utf-8")
        )
        queries = [example["query"] for example in config["loki"]["examples"]]

        self.assertTrue(all('service_name="payment-api"' in query for query in queries))

    def test_sample_app_dependencies_are_declared(self):
        requirements = set(
            (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
        )

        self.assertIn("opentelemetry-api==1.44.0", requirements)
        self.assertIn("opentelemetry-sdk==1.44.0", requirements)
        self.assertIn("opentelemetry-exporter-otlp-proto-http==1.44.0", requirements)

    def test_grafana_panel_does_not_use_dynamic_inner_html(self):
        panel = (PROJECT_DIR / "frontend-plugin" / "grafana.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("innerHTML", panel)
        self.assertIn("textContent", panel)


if __name__ == "__main__":
    unittest.main()
