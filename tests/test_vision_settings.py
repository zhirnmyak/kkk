from tests import TEST_ROOT  # set up offline isolation before app imports
"""Vision: метаданные, чтение тестовой картинки и настоящий формат OCR-запроса."""
import base64
import json
import unittest
from io import BytesIO
from unittest import mock

import httpx
from PIL import Image
import config
import console_settings as ui
import ocr_vision
import vision_support as support
from tests.cache_test_support import CacheIsolation


class VisionSettingsTests(CacheIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(support._cache, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.settings = {"base_url": "https://example.test/v1", "api_key": "test",
                         "model": "selected", "ocr_backend": "llm"}

    def api(self, reply):
        original = httpx.Client
        return mock.patch.object(httpx, "Client", side_effect=lambda **kw: original(
            **kw, transport=httpx.MockTransport(reply)))

    def test_probe_image_is_valid_and_code_is_not_in_prompt(self):
        code, url = support.probe_image()
        image = Image.open(BytesIO(base64.b64decode(url.split(",")[1])))
        self.assertEqual(image.size, (240, 80))
        with mock.patch.object(support, "probe_image", return_value=(code, url)), \
                mock.patch.object(ui, "_api_request", return_value={"choices": [
                    {"message": {"content": code}}]}) as request:
            ui.detect_vision(self.settings)
        body = request.call_args.args[2]
        self.assertNotIn(code, body["messages"][0]["content"][0]["text"])
        self.assertTrue(ui._vision(self.settings)["supported"])
        self.assertEqual(self.settings["ocr_backend"], "llm")
        self.assertIn("selected", ui._value(self.settings, "ocr_backend", []))

    def test_metadata_and_selection(self):
        support.read_metadata(self.settings["base_url"], "test", {
            "id": "selected", "architecture": {"input_modalities": ["text"]}})
        with mock.patch.object(ui, "_api_request") as request:
            ui.detect_vision(self.settings)
            request.assert_not_called()
        self.assertEqual(self.settings["ocr_backend"], "easyocr")
        ui._cycle(self.settings, "ocr_backend", [], 1)
        self.assertEqual(self.settings["ocr_backend"], "easyocr")
        support.read_metadata(self.settings["base_url"], "test", {
            "id": "selected", "capabilities": {"vision": True}})
        ui._cycle(self.settings, "ocr_backend", [], 1)
        self.assertEqual(self.settings["ocr_backend"], "llm")
        self.settings["model"] = "other"
        self.assertIsNone(ui._vision(self.settings))

    def test_explicit_rejection_and_unknown_errors(self):
        with self.api(lambda req: httpx.Response(400, json={"error": {
                "message": "This model does not support image inputs"}})):
            ui.detect_vision(self.settings)
        self.assertFalse(ui._vision(self.settings)["supported"])
        self.assertEqual(self.settings["ocr_backend"], "easyocr")
        for code in (401, 429, 500):
            with self.api(lambda req: httpx.Response(code, json={"error": "failure"})):
                ui.detect_vision(self.settings, force=True)
            self.assertIsNone(ui._vision(self.settings)["supported"])

    def test_ignored_image_is_not_marked_supported(self):
        with mock.patch.object(ui, "_api_request", return_value={"choices": [
                {"message": {"content": "OK"}}]}):
            ui.detect_vision(self.settings)
        self.assertIsNone(ui._vision(self.settings)["supported"])

    def test_ocr_uses_selected_model_without_reasoning_parameter(self):
        bodies = []
        def reply(request):
            self.assertEqual(str(request.url), "https://example.test/v1/chat/completions")
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "text"}}]})
        with mock.patch.dict(config.__dict__), mock.patch.object(ocr_vision, "_to_jpeg", return_value=b"jpeg"), self.api(reply):
            config.LLM_BASE_URL = self.settings["base_url"]
            config.LLM_API_KEY = "test"
            config.LLM_MODEL = "selected"
            self.assertEqual(ocr_vision.transcribe_image_llm(b"image"), "text")
            self.assertEqual(bodies[0]["model"], "selected")
            self.assertNotIn("reasoning_effort", bodies[0])
            self.assertEqual(bodies[0]["messages"][0]["content"][1]["type"], "image_url")
            support.remember(config.LLM_BASE_URL, "test", "selected", False)
            with self.assertRaisesRegex(RuntimeError, "EasyOCR"):
                ocr_vision.transcribe_image_llm(b"image")
            self.assertEqual(len(bodies), 1)
