from tests import TEST_ROOT  # set up offline isolation before app imports
"""Проверки подключения без сети и без изменения пользовательских настроек."""
import asyncio
import json
import io
import os
import tempfile
import unittest
import threading
from pathlib import Path
from unittest import mock

import httpx
from openai import AsyncOpenAI

import config
import console_settings as settings_ui
import gateway_manager
import llm
from tests.cache_test_support import CacheIsolation


class ProviderSettingsTests(CacheIsolation, unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Настройки работают в Windows")
    @mock.patch.object(settings_ui, "detect_reasoning", return_value=None)
    @mock.patch.object(settings_ui, "detect_vision", return_value=None)
    def test_buttons_are_activated_with_enter(self, _vision, _detect):
        class CompletedJob:
            def __init__(self, fn):
                self.result, self.error = fn(), None
                self.done, self.cancelled = threading.Event(), threading.Event()
                self.done.set()

            def cancel(self):
                self.cancelled.set()

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(settings_ui, "SETTINGS_PATH", Path(directory) / "settings.json"), \
                mock.patch.dict(config.__dict__), mock.patch.dict(os.environ), \
                mock.patch.object(settings_ui, "save"), \
                mock.patch.object(settings_ui.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(settings_ui.sys, "stdout", io.StringIO()), \
                mock.patch.object(settings_ui, "fetch_models", return_value=["first", "second"]) as fetch, \
                mock.patch.object(settings_ui, "test_connection", return_value="Тест пройден") as test, \
                mock.patch.object(settings_ui.model_checks, "Job", side_effect=CompletedJob), \
                mock.patch.object(settings_ui, "_poll_key", side_effect=[
                    "down", "down", "down", "enter", "down", "down", "enter", "esc"]):
            os.environ.pop("MAXIMKA_MODEL_EXPLICIT", None)
            settings = settings_ui.load()
            result = settings_ui.run(settings)
            self.assertEqual(fetch.call_count, 2)
            test.assert_called_once_with(result)
            self.assertEqual(result["model"], "first")

    def test_endpoint_models_and_test_button(self):
        requests = []

        def reply(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"data": [
                    {"id": "model-a"}, {"id": "model-b"}, {"id": "model-a"}, None]})
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "OK"}}]})

        original_client = httpx.Client
        settings = {"base_url": "https://example.test/custom/v1", "api_key": "test-secret", "model": "old"}
        with mock.patch.object(httpx, "Client", side_effect=lambda **kw: original_client(
                **kw, transport=httpx.MockTransport(reply))):
            models, status = settings_ui._settings_action("load_models", settings, [])
            self.assertEqual(models, ["model-a", "model-b"])
            self.assertEqual(settings["model"], "model-a")
            self.assertIn("2", status)
            same_models, status = settings_ui._settings_action("test_connection", settings, models)
            self.assertEqual(same_models, models)
            self.assertIn("Тест пройден", status)
        self.assertEqual(str(requests[0].url), "https://example.test/custom/v1/models")
        self.assertEqual(str(requests[1].url), "https://example.test/custom/v1/chat/completions")
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-secret")
        self.assertEqual(json.loads(requests[1].content)["model"], "model-a")

    def test_api_error_is_visible_without_key_or_fake_models(self):
        original_client = httpx.Client
        settings = {"base_url": "https://example.test/v1", "api_key": "test-secret", "model": "manual"}
        with mock.patch.object(httpx, "Client", side_effect=lambda **kw: original_client(
                **kw, transport=httpx.MockTransport(lambda req: httpx.Response(
                    401, json={"error": "test-secret"})))):
            models, status = settings_ui._settings_action("load_models", settings, [])
            self.assertEqual(models, ["manual"])
            self.assertIn("401", status)
            self.assertNotIn("test-secret", status)

    def test_empty_or_malformed_models_are_reported(self):
        settings = {"model": "manual"}
        for data in ({"data": []}, {"data": "wrong"}, {"error": "failure"}):
            with mock.patch.object(settings_ui, "_api_request", return_value=data):
                models, status = settings_ui._settings_action("load_models", settings, [])
                self.assertEqual(models, ["manual"])
                self.assertIn("Ошибка", status)

    def test_profiles_survive_save_and_reload(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(settings_ui, "SETTINGS_PATH", Path(directory) / "settings.json"), \
                mock.patch.object(settings_ui, "MODEL_HINT_PATH", Path(directory) / "model.txt"), \
                mock.patch.dict(os.environ), mock.patch.dict(config.__dict__), \
                mock.patch.object(gateway_manager, "stop"):
            os.environ.pop("MAXIMKA_MODEL_EXPLICIT", None)
            config.LLM_PROVIDER = "openai"
            config.LLM_BASE_URL = "http://127.0.0.1:8787/v1"
            settings = settings_ui.load()
            self.assertEqual(settings["provider"], "openai")
            self.assertEqual(settings["base_url"], "http://127.0.0.1:8787/v1")
            settings.update(base_url="https://example.test/v1", api_key="secret-test", model="custom")
            settings_ui.apply(settings)
            self.assertNotIn("secret-test", settings_ui.render(settings, 0, []))
            settings_ui._cycle(settings, "provider", [], 1)
            settings_ui.apply(settings)
            self.assertEqual(settings["provider"], "qoder")
            self.assertEqual(config.LLM_BASE_URL, "http://127.0.0.1:8787/v1")
            settings_ui._cycle(settings, "provider", [], 1)
            settings_ui.apply(settings)
            settings_ui.save(settings)
            loaded = settings_ui.load()
            self.assertEqual(loaded["model"], "custom")
            self.assertEqual(loaded["api_key"], "secret-test")
            self.assertEqual(loaded["base_url"], "https://example.test/v1")

    def test_local_openai_does_not_start_qoder(self):
        with mock.patch.object(config, "LLM_MOCK", False), \
                mock.patch.object(config, "LLM_PROVIDER", "openai"), \
                mock.patch.object(config, "LLM_BASE_URL", "http://localhost:1234/v1"), \
                mock.patch.object(gateway_manager, "_spawn") as spawn:
            self.assertIn("не нужен", gateway_manager.ensure({}))
            spawn.assert_not_called()

    def test_requests_use_changed_url_key_and_model(self):
        async def check():
            requests = []

            def reply(request):
                requests.append(request)
                return httpx.Response(200, json={
                    "id": "test", "object": "chat.completion", "created": 1,
                    "model": "custom", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop"}],
                })

            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
                client = AsyncOpenAI(base_url="https://old.test/v1", api_key="old", http_client=transport)
                with mock.patch.object(llm, "client", client), \
                        mock.patch.dict(config.__dict__), mock.patch.dict(os.environ):
                    config.LLM_MOCK = False
                    config.LLM_PROVIDER = "openai"
                    config.LLM_BASE_URL = "https://new.test/v1"
                    config.LLM_API_KEY = "new-test-key"
                    config.LLM_MODEL = "custom"
                    os.environ["LLM_REASONING_EFFORT"] = "auto"
                    os.environ["LLM_CONTEXT_WINDOW"] = "1000000"
                    llm.set_reasoning_sink(None)
                    self.assertEqual(await llm.chat("system", "hello"), "ok")
                    request = requests[0]
                    self.assertEqual(str(request.url), "https://new.test/v1/chat/completions")
                    self.assertEqual(request.headers["authorization"], "Bearer new-test-key")
                    body = json.loads(request.content)
                    self.assertEqual(body["model"], "custom")
                    self.assertNotIn("context_window", body)
                    self.assertNotIn("reasoning_effort", body)
        asyncio.run(check())
