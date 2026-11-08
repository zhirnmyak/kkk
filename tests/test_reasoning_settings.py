from tests import TEST_ROOT  # set up offline isolation before app imports
"""Определение возможностей моделей через поддельный HTTP API."""
import unittest
from unittest import mock

import httpx
import console_settings as ui
import reasoning_support as support
from tests.cache_test_support import CacheIsolation


class ReasoningSettingsTests(CacheIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(support._cache, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.settings = {"base_url": "https://example.test/v1", "api_key": "test",
                         "model": "model-a", "effort": "high"}

    def api(self, reply):
        original = httpx.Client
        return mock.patch.object(httpx, "Client", side_effect=lambda **kw: original(
            **kw, transport=httpx.MockTransport(reply)))

    def test_metadata_limits_levels(self):
        with self.api(lambda request: httpx.Response(200, json={"data": [
                {"id": "model-a", "reasoning_effort": {"enum": ["low", "high"]}},
                {"id": "model-b", "supported_parameters": ["temperature"]}]})):
            self.assertEqual(ui.fetch_models(self.settings), ["model-a", "model-b"])
        with mock.patch.object(ui, "_api_request") as request:
            ui.detect_reasoning(self.settings)
            request.assert_not_called()
        self.assertEqual(ui._efforts(self.settings), ("auto", "low", "high"))
        ui._cycle(self.settings, "effort", [], 1)
        self.assertEqual(self.settings["effort"], "auto")
        self.settings["model"] = "model-b"
        ui.detect_reasoning(self.settings)
        self.assertEqual(ui._value(self.settings, "effort", []), "не поддерживается")

    def test_probe_rejection_disables_effort(self):
        calls = []
        def reply(request):
            calls.append(request)
            return httpx.Response(400, json={"error": {
                "message": "Unrecognized request argument supplied: reasoning_effort"}})
        with self.api(reply):
            ui.detect_reasoning(self.settings)
            ui.detect_reasoning(self.settings)
        self.assertEqual(len(calls), 1)
        self.assertFalse(ui._support(self.settings)["supported"])
        self.assertEqual(self.settings["effort"], "auto")
        ui._cycle(self.settings, "effort", [], 1)
        self.assertEqual(self.settings["effort"], "auto")

    def test_probe_acceptance_only_offers_tested_level(self):
        with self.api(lambda request: httpx.Response(200, json={"choices": [
                {"message": {"content": "OK"}}]})):
            ui.detect_reasoning(self.settings)
        self.assertEqual(ui._efforts(self.settings), ("auto", "low"))
        self.assertIn("API принял low", ui._value(self.settings, "effort", []))

    def test_errors_do_not_mean_no_reasoning(self):
        for code in (400, 401, 403, 429, 500):
            with self.subTest(code=code), self.api(lambda request: httpx.Response(
                    code, json={"error": {"message": "Invalid model"}})):
                ui.detect_reasoning(self.settings)
                self.assertIsNone(ui._support(self.settings))

    def test_cache_is_scoped_to_connection_and_model(self):
        support.remember(self.settings["base_url"], "test", "model-a", False)
        for field, value in (("model", "model-b"), ("api_key", "other"),
                             ("base_url", "https://other.test/v1")):
            changed = dict(self.settings, **{field: value})
            self.assertIsNone(ui._support(changed))
