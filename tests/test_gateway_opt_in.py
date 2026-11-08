from tests import TEST_ROOT  # set up offline isolation before app imports
import os
import unittest
from unittest import mock
import config
import console_settings as ui
import gateway_manager as gateway
from tests.cache_test_support import CacheIsolation


class GatewayOptInTests(CacheIsolation, unittest.TestCase):
    def test_openai_local_or_remote_never_starts_gateway(self):
        with mock.patch.object(config, "LLM_MOCK", False), \
                mock.patch.object(config, "LLM_PROVIDER", "qoder"), \
                mock.patch.object(gateway, "_spawn") as spawn, \
                mock.patch.object(gateway, "is_up") as health:
            for url in ("http://localhost:1234/v1", "http://127.0.0.1:8787/v1", "https://api.example.test/v1"):
                settings = {"provider": "openai", "base_url": url}
                self.assertIn("не нужен", gateway.ensure(settings))
                self.assertIn("не нужен", gateway.restart(settings))
            spawn.assert_not_called()
            health.assert_not_called()

    def test_qoder_explicit_selection_starts_gateway(self):
        with mock.patch.object(config, "LLM_MOCK", False), \
                mock.patch.object(gateway, "is_up", return_value=False), \
                mock.patch.object(gateway, "_start", return_value="запущен") as start:
            settings = {"provider": "qoder", "base_url": "http://127.0.0.1:8787/v1"}
            self.assertEqual(gateway.ensure(settings), "запущен")
            start.assert_called_once_with(settings, "запущен")

    def test_low_level_spawn_also_requires_qoder(self):
        with mock.patch.object(config, "LLM_MOCK", False), mock.patch.object(gateway.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "Qoder"):
                gateway._spawn({"provider": "openai", "base_url": "http://localhost:1234/v1"})
            spawn.assert_not_called()

    def test_model_flag_does_not_select_qoder_and_local_api_is_kept(self):
        with mock.patch.dict(config.__dict__), mock.patch.dict(os.environ), \
                mock.patch.object(ui, "SETTINGS_PATH", self._cache_file().with_name("settings.json")):
            config.LLM_PROVIDER = "openai"
            config.LLM_BASE_URL = "http://localhost:1234/v1"
            config.LLM_MODEL = "local-model"
            os.environ["MAXIMKA_MODEL_EXPLICIT"] = "1"
            os.environ.pop("MAXIMKA_QODER_EXPLICIT", None)
            settings = ui.load()
            self.assertEqual(settings["provider"], "openai")
            self.assertEqual(settings["base_url"], "http://localhost:1234/v1")
            os.environ["MAXIMKA_QODER_EXPLICIT"] = "1"
            self.assertEqual(ui.load()["provider"], "qoder")

    def _cache_file(self):
        import capability_cache
        return capability_cache.CACHE_PATH
