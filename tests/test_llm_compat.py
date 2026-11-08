from tests import TEST_ROOT  # set up offline isolation before app imports
"""Совместимость LLM с серверами без reasoning_effort, без сетевых запросов."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
from openai import AsyncOpenAI, BadRequestError

import config
import llm
import reasoning_support
from tests.cache_test_support import CacheIsolation


class ReasoningCompatibilityTests(CacheIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(reasoning_support._cache, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def check_request(self, streaming=False, error=None, always_fail=False, switch_model=False):
        async def check():
            bodies = []

            def reply(request):
                body = json.loads(request.content)
                bodies.append(body)
                self.assertEqual(str(request.url), "https://example.test/v1/chat/completions")
                self.assertEqual(body["model"], "custom")
                if len(bodies) == 1 and switch_model:
                    config.LLM_BASE_URL = "https://other.test/v1"
                    config.LLM_API_KEY = "other-key"
                    config.LLM_MODEL = "other-model"
                if len(bodies) == 1 or always_fail:
                    return httpx.Response(400, json={"error": {
                        "message": error or "Unrecognized request argument supplied: reasoning_effort",
                        "type": "invalid_request_error"}})
                if streaming:
                    chunk = {"id": "test", "object": "chat.completion.chunk",
                             "created": 1, "model": "custom", "choices": [
                                 {"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]}
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
                return httpx.Response(200, json={
                    "id": "test", "object": "chat.completion", "created": 1,
                    "model": "custom", "choices": [{"index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop"}]})

            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
                client = AsyncOpenAI(base_url="https://example.test/v1", api_key="test",
                                     http_client=transport)
                with mock.patch.object(llm, "client", client), mock.patch.dict(config.__dict__), \
                        mock.patch.dict(llm.os.environ, {"LLM_CONTEXT_WINDOW": "12345"}):
                    config.LLM_MOCK = False
                    config.LLM_PROVIDER = "qoder"
                    config.LLM_BASE_URL = "https://example.test/v1"
                    config.LLM_API_KEY = "test"
                    config.LLM_MODEL = "custom"
                    token = llm._reason_sink.set((lambda text: None) if streaming else None)
                    try:
                        if error or always_fail:
                            with self.assertRaises(BadRequestError):
                                await llm.chat("system", "hello", effort="low")
                            self.assertEqual(len(bodies), 2 if always_fail else 1)
                        else:
                            self.assertEqual(await llm.chat("system", "hello", effort="low"), "ok")
                            self.assertEqual(len(bodies), 2)
                            self.assertEqual(bodies[0]["reasoning_effort"], "low")
                            self.assertNotIn("reasoning_effort", bodies[1])
                            self.assertEqual(bodies[1]["context_window"], 12345)
                            self.assertEqual(bool(bodies[1].get("stream")), streaming)
                            if switch_model:
                                self.assertFalse(reasoning_support.get("https://example.test/v1", "test", "custom")["supported"])
                                self.assertIsNone(reasoning_support.get("https://other.test/v1", "other-key", "other-model"))
                    finally:
                        llm._reason_sink.reset(token)
        asyncio.run(check())

    def test_plain_fallback(self):
        self.check_request()

    def test_stream_fallback(self):
        self.check_request(streaming=True)

    def test_settings_change_does_not_poison_other_model_cache(self):
        self.check_request(switch_model=True)

    def test_other_error_is_not_retried(self):
        self.check_request(error="Unrecognized request argument supplied: temperature")

    def test_invalid_effort_is_not_retried(self):
        self.check_request(error="Invalid value for reasoning_effort: low")

    def test_fallback_failure_is_not_retried(self):
        self.check_request(always_fail=True)

    def test_known_unsupported_model_omits_explicit_effort(self):
        async def check():
            create = mock.AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=None, model="custom"))
            fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            with mock.patch.object(llm, "client") as client, mock.patch.dict(config.__dict__):
                client.with_options.return_value = fake
                config.LLM_MOCK = False
                config.LLM_BASE_URL = "https://example.test/v1"
                config.LLM_API_KEY = "test"
                config.LLM_MODEL = "custom"
                reasoning_support.remember(config.LLM_BASE_URL, "test", "custom", False)
                token = llm._reason_sink.set(None)
                try:
                    self.assertEqual(await llm.chat("system", "hello", effort="low"), "ok")
                finally:
                    llm._reason_sink.reset(token)
                self.assertNotIn("reasoning_effort", create.call_args.kwargs.get("extra_body", {}))
                self.assertEqual(create.await_count, 1)
        asyncio.run(check())
