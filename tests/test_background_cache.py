from tests import TEST_ROOT  # set up offline isolation before app imports
import io
import json
import os
import threading
import time
import unittest
from unittest import mock

import capability_cache as disk
import config
import console_settings as ui
import model_checks
import reasoning_support as reasoning
import vision_support as vision
from tests.cache_test_support import CacheIsolation


class BackgroundCacheTests(CacheIsolation, unittest.TestCase):
    def test_persistent_cache_and_expiry(self):
        reasoning.remember("https://secret-host.test/v1", "secret-key", "a", True, ("low",))
        vision.remember("https://secret-host.test/v1", "secret-key", "a", False)
        text = disk.CACHE_PATH.read_text("utf-8")
        self.assertNotIn("secret-key", text)
        self.assertNotIn("secret-host", text)
        reasoning._cache.clear()
        vision._cache.clear()
        self.assertTrue(reasoning.get("https://secret-host.test/v1", "secret-key", "a")["supported"])
        self.assertFalse(vision.get("https://secret-host.test/v1", "secret-key", "a")["supported"])
        self.assertIsNone(vision.get("https://secret-host.test/v1", "other-key", "a"))
        with mock.patch.object(disk.time, "time", return_value=time.time() + disk.TTL + 1):
            self.assertIsNone(reasoning.get("https://secret-host.test/v1", "secret-key", "a"))

    def test_corrupted_cache_is_ignored(self):
        disk.CACHE_PATH.write_text("oops", "utf-8")
        self.assertIsNone(vision.get("url", "key", "a"))
        vision.remember("url", "key", "a", True)
        self.assertEqual(json.loads(disk.CACHE_PATH.read_text())["version"], 1)

    def test_cancel_prevents_late_cache_write_and_next_request(self):
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            release.wait(3)
            vision.remember("url", "key", "a", True)
            self.fail("Cancelled worker must not continue")
        job = model_checks.Job(work)
        self.assertTrue(started.wait(2))
        job.cancel()
        release.set()
        self.assertTrue(job.done.wait(2))
        self.assertIsNone(vision.get("url", "key", "a"))
        self.assertIsNone(job.error)

    @unittest.skipUnless(os.name == "nt", "Windows console")
    def test_menu_can_cancel_and_exit_while_network_is_blocked(self):
        started, release = threading.Event(), threading.Event()
        jobs = []
        original_job = model_checks.Job
        def blocked(*args):
            started.set()
            release.wait(3)
            return ["late-model"], "late result", "late-model"
        def create(fn):
            job = original_job(fn)
            jobs.append(job)
            self.assertTrue(started.wait(2))
            return job
        with mock.patch.dict(config.__dict__), mock.patch.dict(os.environ), \
                mock.patch.object(ui, "SETTINGS_PATH", disk.CACHE_PATH.parent / "settings.json"), \
                mock.patch.object(ui, "save"), mock.patch.object(ui.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(ui.sys, "stdout", io.StringIO()), \
                mock.patch.object(ui, "_background_action", side_effect=blocked), \
                mock.patch.object(model_checks, "Job", side_effect=create), \
                mock.patch.object(ui, "_poll_key", side_effect=["down", "c", "esc"]):
            os.environ.pop("MAXIMKA_QODER_EXPLICIT", None)
            config.LLM_PROVIDER = "openai"
            settings = ui.load()
            before = settings["model"]
            start = time.monotonic()
            result = ui.run(settings)
            self.assertLess(time.monotonic() - start, 1)
            self.assertEqual(result["model"], before)
            self.assertTrue(jobs[0].cancelled.is_set())
            release.set()
            self.assertTrue(jobs[0].done.wait(2))
            self.assertEqual(result["model"], before)

    def test_forced_recheck_replaces_saved_result(self):
        settings = {"base_url": "url", "api_key": "key", "model": "a", "effort": "high"}
        reasoning.remember("url", "key", "a", False)
        with mock.patch.object(ui, "_api_request", return_value={"choices": [{}]}) as request:
            ui.detect_reasoning(settings)
            request.assert_not_called()
            ui.detect_reasoning(settings, force=True)
            request.assert_called_once()
        reasoning._cache.clear()
        self.assertTrue(reasoning.get("url", "key", "a")["supported"])

    def test_cancelled_forced_probe_keeps_previous_result(self):
        settings = {"base_url": "url", "api_key": "key", "model": "a", "effort": "auto"}
        reasoning.remember("url", "key", "a", False)
        with mock.patch.object(ui, "_api_request", side_effect=model_checks.Cancelled()):
            with self.assertRaises(model_checks.Cancelled):
                ui.detect_reasoning(settings, force=True)
        self.assertFalse(reasoning.get("url", "key", "a")["supported"])
