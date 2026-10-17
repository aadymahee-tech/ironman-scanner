"""Settings from the environment and the deployment tag table."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tradetron_pnl import config


class TestSettings(unittest.TestCase):
    def test_defaults(self):
        s = config.settings_from_env(env={})
        self.assertEqual(s.base_url, config.DEFAULT_BASE_URL)
        self.assertEqual(s.pnl_semantics, "cumulative")
        self.assertEqual(s.default_segment, "fno")
        self.assertEqual(s.reporting_currency, "INR")
        self.assertTrue(s.fetch_positions)
        self.assertFalse(s.preopen_message)
        self.assertFalse(s.telegram_configured)

    def test_environment_values(self):
        s = config.settings_from_env(env={
            "TRADETRON_API_KEY": " ttk_abc ",
            "TRADETRON_API_BASE_URL": "https://example.test/api/v1/",
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "TELEGRAM_CHAT_ID": "-100",
            "TTPNL_PNL_SEMANTICS": "Today",
            "TTPNL_FETCH_POSITIONS": "off",
            "TTPNL_PREOPEN_MESSAGE": "yes",
            "TTPNL_MAX_POSITIONS": "3",
            "TTPNL_CURRENCY": "usd",
        })
        self.assertEqual(s.api_key, "ttk_abc")
        self.assertEqual(s.base_url, "https://example.test/api/v1")
        self.assertTrue(s.telegram_configured)
        self.assertEqual(s.pnl_semantics, "today")
        self.assertFalse(s.fetch_positions)
        self.assertTrue(s.preopen_message)
        self.assertEqual(s.max_positions, 3)
        self.assertEqual(s.reporting_currency, "USD")

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            config.settings_from_env(env={"TTPNL_PNL_SEMANTICS": "daily"})
        with self.assertRaises(ValueError):
            config.settings_from_env(env={"TTPNL_DEFAULT_SEGMENT": "stocks"})
        with self.assertRaises(ValueError):
            config.settings_from_env(env={"TTPNL_FETCH_POSITIONS": "maybe"})

    def test_dotenv_is_read_but_real_environment_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# secrets\nexport TRADETRON_API_KEY='from-file'\nTELEGRAM_CHAT_ID=\"42\"\n\nBROKEN LINE\n",
                encoding="utf-8",
            )
            s = config.settings_from_env(env={"TRADETRON_API_KEY": "from-env"}, dotenv=env_file)
        self.assertEqual(s.api_key, "from-env")
        self.assertEqual(s.telegram_chat_id, "42")

    def test_truthy(self):
        self.assertTrue(config.truthy("1"))
        self.assertTrue(config.truthy("Yes"))
        self.assertFalse(config.truthy("0"))
        self.assertFalse(config.truthy(""))
        self.assertTrue(config.truthy(None, default=True))


class TestDeploymentTags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.counter = 0

    def write(self, text: str) -> Path:
        self.counter += 1
        path = Path(self.tmp.name) / f"deployments{self.counter}.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_committed_template_loads_empty(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(config.load_deployment_tags(root / "config" / "deployments.csv"), {})

    def test_rows_with_comments_and_defaults(self):
        path = self.write(
            "# tags\n"
            "deployment_id,alias,segment,include,notes\n"
            "1001,Straddle,fno,1,main\n"
            "1002,,intraday_equity,0,\n"
            "1003,Crude,commodity,,\n"
            ",ignored,fno,1,no id\n"
        )
        tags = config.load_deployment_tags(path)
        self.assertEqual(set(tags), {1001, 1002, 1003})
        self.assertEqual(tags[1001].alias, "Straddle")
        self.assertFalse(tags[1002].include)
        self.assertTrue(tags[1003].include)           # blank include -> 1
        self.assertEqual(tags[1003].segment, "commodity")
        self.assertTrue(tags[1001].configured)

    def test_bad_segment_and_bad_id_fail_loudly(self):
        with self.assertRaises(ValueError):
            config.load_deployment_tags(self.write("deployment_id,alias,segment,include,notes\n1,x,stocks,1,\n"))
        with self.assertRaises(ValueError):
            config.load_deployment_tags(self.write("deployment_id,alias,segment,include,notes\nabc,x,fno,1,\n"))

    def test_tag_for_unknown_id_uses_default_segment(self):
        tag = config.tag_for(999, {}, default_segment="crypto")
        self.assertEqual(tag.segment, "crypto")
        self.assertTrue(tag.include)
        self.assertFalse(tag.configured)
        self.assertEqual(tag.alias, "")


if __name__ == "__main__":
    unittest.main()


class TestEndpointOverrides(unittest.TestCase):
    def test_defaults_and_overrides(self):
        from tradetron_pnl.config import settings_from_env
        s = settings_from_env({})
        self.assertEqual(s.deployments_path, "/me/deployments")
        self.assertEqual(s.positions_path, "/me/deployments/{id}/positions")
        s = settings_from_env({"TRADETRON_DEPLOYMENTS_PATH": "/v2/deployments",
                               "TRADETRON_POSITIONS_PATH": "/v2/deployments/{id}/legs",
                               "TRADETRON_NOTIFICATIONS_PATH": "/v2/alerts"})
        self.assertEqual((s.deployments_path, s.positions_path, s.notifications_path),
                         ("/v2/deployments", "/v2/deployments/{id}/legs", "/v2/alerts"))

    def test_positions_path_needs_placeholder(self):
        from tradetron_pnl.config import settings_from_env
        with self.assertRaises(ValueError):
            settings_from_env({"TRADETRON_POSITIONS_PATH": "/positions"})
