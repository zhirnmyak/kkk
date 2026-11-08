from tests import TEST_ROOT
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import httpx
import admin_setup
import config
import console_settings
import db
import rag
import runtime_settings
from bot import handlers
from bot.max_client import MaxClient
from miniapp.auth import validate_init_data
from miniapp.server import create_http_app, finish_tasks, enable_local_access
from tests.cache_test_support import CacheIsolation

TOKEN = "offline-miniapp-test-token"
ADMIN = 8800001
STUDENT = 8800002


def signed_data(user_id=STUDENT, *, timestamp=None, name="Анна + 100%", extra=None):
    pairs = {"auth_date": str(int(time.time()) if timestamp is None else timestamp),
             "user": json.dumps({"id": user_id, "first_name": name}, ensure_ascii=False),
             "query_id": "offline-query"}
    pairs.update(extra or {})
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    signature = hmac.new(secret, "\n".join(key + "=" + pairs[key] for key in sorted(pairs)).encode(),
                         hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


class AuthTests(unittest.TestCase):
    def test_unicode_and_percent_are_decoded_exactly_once(self):
        self.assertEqual(validate_init_data(signed_data(), TOKEN)["first_name"], "Анна + 100%")

    def test_modified_signature_and_wrong_bot_are_rejected(self):
        for raw, token in ((signed_data().replace("offline-query", "changed"), TOKEN),
                           (signed_data(), "another-bot"), ("", TOKEN)):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                validate_init_data(raw, token)

    def test_duplicate_keys_and_hashes_are_rejected(self):
        for suffix in ("&user=%7B%22id%22%3A8800001%7D", "&hash=" + "0" * 64,
                       "&auth_date=0", "&query_id=again"):
            with self.subTest(suffix=suffix[:20]), self.assertRaises(ValueError):
                validate_init_data(signed_data() + suffix, TOKEN)

    def test_expiry_future_and_invalid_identity(self):
        now = int(time.time())
        for raw in (signed_data(timestamp=now - 3601), signed_data(timestamp=now + 100),
                    signed_data(True), signed_data(0), signed_data(-1), signed_data("8800001")):
            with self.assertRaises(ValueError):
                validate_init_data(raw, TOKEN, now=now)

    def test_malformed_percent_encoding_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_init_data(signed_data(extra={"start_param": "%ZZ"}).replace("%25ZZ", "%ZZ"), TOKEN)


class AdminSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "admin.json"
        for patch in (mock.patch.object(admin_setup, "ADMIN_PATH", path),
                      mock.patch.dict(os.environ, {"MAX_ADMIN_ID": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_first_launch_prompts_and_next_launch_uses_saved_id(self):
        read = mock.Mock(side_effect=["not-a-number", "0", str(ADMIN)])
        self.assertEqual(admin_setup.ensure_admin_id(interactive=True, read=read, write=lambda _: None), ADMIN)
        self.assertEqual(read.call_count, 3)
        again = mock.Mock()
        self.assertEqual(admin_setup.ensure_admin_id(interactive=True, read=again), ADMIN)
        again.assert_not_called()

    def test_noninteractive_start_does_not_consume_scenario_input(self):
        read = mock.Mock()
        self.assertIsNone(admin_setup.ensure_admin_id(interactive=False, read=read, write=lambda _: None))
        read.assert_not_called()
        self.assertIsNone(admin_setup.get_admin_id())

    def test_skip_does_not_grant_admin(self):
        self.assertIsNone(admin_setup.ensure_admin_id(interactive=True, read=lambda _: "", write=lambda _: None))
        self.assertIsNone(admin_setup.get_admin_id())

    def test_environment_has_priority(self):
        with mock.patch.dict(os.environ, {"MAX_ADMIN_ID": str(ADMIN)}):
            self.assertEqual(admin_setup.get_admin_id(), ADMIN)


class ApiTests(CacheIsolation, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for patch in (
            mock.patch.object(config, "MAX_TOKEN", TOKEN),
            mock.patch.object(console_settings, "SETTINGS_PATH", self.root / "settings.json"),
            mock.patch.object(console_settings, "MODEL_HINT_PATH", self.root / "model.txt"),
            mock.patch.dict(config.__dict__),
            mock.patch.dict(os.environ, {"MAX_ADMIN_ID": str(ADMIN), "MAXIMKA_QODER_EXPLICIT": "",
                                        "MAXIMKA_MODEL_EXPLICIT": ""}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config.LLM_PROVIDER = "openai"
        config.LLM_BASE_URL = "https://api.example.test/v1"
        config.LLM_API_KEY = "stored-server-secret"
        config.LLM_MODEL = "original-model"
        self.app = create_http_app()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="http://test", headers={"Authorization": "MAX " + signed_data()})
        for uid in (ADMIN, STUDENT):
            db.touch_user(uid, "u:" + str(uid))
            rag.add_document(uid, "Материал.pdf", pages=["Текст материала о клетке и её ядре. " * 8])
            db.add_document_row(uid, "Материал.pdf")
        self.doc = db.document_id(STUDENT, "Материал.pdf")
        self.other_doc = db.document_id(ADMIN, "Материал.pdf")

    async def asyncTearDown(self):
        await finish_tasks(self.app)
        await self.client.aclose()
        for uid in (ADMIN, STUDENT):
            rag.clear_user(uid)
            db.clear_documents(uid)

    def admin(self):
        return {"Authorization": "MAX " + signed_data(ADMIN)}

    async def test_no_unsigned_or_query_string_login(self):
        response = await self.client.get("/api/me?initData=" + signed_data(ADMIN),
                                         headers={"Authorization": ""})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_local_access_requires_explicit_mode_and_random_credential(self):
        self.assertEqual((await self.client.get("/api/me", headers={"Authorization": "Local guessed"})).status_code, 401)
        enable_local_access(self.app, output=lambda _: None)
        token = self.app.state.local_access["token"]
        header = {"Authorization": "Local " + token}
        response = await self.client.get("http://127.0.0.1/api/me", headers=header)
        self.assertTrue(response.json()["is_admin"])
        self.assertTrue(response.json()["local_mode"])
        self.assertEqual((await self.client.get("/api/me", headers=header)).status_code, 401)
        self.assertEqual((await self.client.get("http://127.0.0.1/api/me",
            headers={**header, "Origin": "https://untrusted.example"})).status_code, 401)
        self.assertEqual((await self.client.get("http://127.0.0.1/api/me",
            headers={**header, "X-Forwarded-For": "127.0.0.1"})).status_code, 401)
        transport = httpx.ASGITransport(app=self.app, client=("198.51.100.12", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as remote:
            self.assertEqual((await remote.get("/api/me", headers=header)).status_code, 401)
        self.app.state.local_access["expires"] = 0
        self.assertEqual((await self.client.get("http://127.0.0.1/api/me", headers=header)).status_code, 401)
        enable_local_access(self.app, output=lambda _: None)
        header = {"Authorization": "Local " + self.app.state.local_access["token"]}
        await finish_tasks(self.app)
        self.assertEqual((await self.client.get("http://127.0.0.1/api/me", headers=header)).status_code, 401)

    async def test_admin_permission_comes_from_verified_identity(self):
        me = (await self.client.get("/api/me")).json()
        self.assertFalse(me["is_admin"])
        self.assertEqual((await self.client.get("/api/admin/settings")).status_code, 403)
        self.assertEqual((await self.client.get("/api/admin/users")).status_code, 403)
        self.assertEqual((await self.client.patch("/api/admin/settings", json={"revision": "0", "patch": {}})).status_code, 403)
        self.assertEqual((await self.client.post("/api/admin/checks/load_models", json={})).status_code, 403)
        self.assertTrue((await self.client.get("/api/me", headers=self.admin())).json()["is_admin"])

    async def test_material_scope_and_guessed_ids(self):
        response = await self.client.get("/api/files")
        self.assertEqual([row["id"] for row in response.json()["files"]], [self.doc])
        self.assertEqual((await self.client.get("/api/files/" + str(self.other_doc))).status_code, 404)
        self.assertEqual((await self.client.delete("/api/files/" + str(self.other_doc))).status_code, 404)
        self.assertEqual((await self.client.get("/api/files?owner_id=" + str(ADMIN))).status_code, 403)
        data = (await self.client.get("/api/files?owner_id=" + str(STUDENT), headers=self.admin())).json()
        self.assertEqual(data["files"][0]["id"], self.doc)
        self.assertGreater(data["files"][0]["fragments"], 0)

    async def test_preview_and_delete_keep_other_users_material(self):
        db.add_cards(STUDENT, [{"front": "Вопрос", "back": "Ответ"}], "Материал.pdf")
        preview = await self.client.get("/api/files/" + str(self.doc))
        self.assertIn("клетке", preview.json()["chunks"][0]["text"])
        self.assertEqual((await self.client.delete("/api/files/" + str(self.doc))).status_code, 200)
        self.assertFalse(db.has_document(STUDENT, "Материал.pdf"))
        self.assertEqual(db.card_totals(STUDENT)[0], 0)
        self.assertTrue(db.has_document(ADMIN, "Материал.pdf"))
        self.assertEqual((await self.client.delete("/api/files/" + str(self.doc))).status_code, 404)

    async def test_processing_material_cannot_be_deleted(self):
        handlers._INDEXING.add((STUDENT, "Материал.pdf"))
        try:
            self.assertEqual((await self.client.delete("/api/files/" + str(self.doc))).status_code, 409)
        finally:
            handlers._INDEXING.discard((STUDENT, "Материал.pdf"))
        self.assertTrue(db.has_document(STUDENT, "Материал.pdf"))

    async def test_settings_hide_keys_save_and_detect_stale_forms(self):
        initial = (await self.client.get("/api/admin/settings", headers=self.admin())).json()
        self.assertNotIn("stored-server-secret", json.dumps(initial))
        self.assertNotIn("api_key", initial["values"])
        response = await self.client.patch("/api/admin/settings", headers=self.admin(),
            json={"revision": initial["revision"], "patch": {"model": "new-model", "api_key": "new-private-secret"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(config.LLM_MODEL, "new-model")
        self.assertEqual(console_settings.load()["api_key"], "new-private-secret")
        self.assertNotIn("new-private-secret", response.text)
        self.assertEqual((await self.client.patch("/api/admin/settings", headers=self.admin(),
            json={"revision": initial["revision"], "patch": {"model": "stale-model"}})).status_code, 409)
        self.assertEqual(config.LLM_MODEL, "new-model")

    async def test_invalid_settings_do_not_change_running_config(self):
        before = config.LLM_MODEL
        for patch in ({"model": ""}, {"ocr_workers": True}, {"provider": "shell"},
                      {"base_url": "file:///etc/passwd"}, {"base_url": "https://user:pass@example.test/v1"},
                      {"run_command": "anything"}):
            response = await self.client.patch("/api/admin/settings", headers=self.admin(),
                json={"revision": "0", "patch": patch})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(config.LLM_MODEL, before)
        self.assertFalse(console_settings.SETTINGS_PATH.exists())

    async def test_empty_key_explicitly_clears_saved_key(self):
        response = await self.client.patch("/api/admin/settings", headers=self.admin(),
            json={"revision": "0", "patch": {"api_key": ""}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["api_key_set"])
        self.assertEqual(console_settings.load()["api_key"], "")

    async def test_check_uses_saved_profile_and_redacts_errors(self):
        with mock.patch.object(console_settings, "_settings_action", return_value=(["original-model"], "Ошибка: stored-server-secret")):
            data = (await self.client.post("/api/admin/checks/test_connection", headers=self.admin(), json={})).json()
        self.assertFalse(data["ok"])
        self.assertNotIn("stored-server-secret", json.dumps(data))

    async def test_study_requires_running_bot_and_own_material(self):
        body = {"kind": "quiz", "count": 3}
        self.assertEqual((await self.client.post("/api/files/" + str(self.doc) + "/study", json=body)).status_code, 503)
        self.app.state.bot_client = mock.Mock()
        with mock.patch.object(handlers, "_execute_study", new_callable=mock.AsyncMock) as execute:
            response = await self.client.post("/api/files/" + str(self.doc) + "/study", json=body)
            self.assertEqual(response.status_code, 202)
            await asyncio.gather(*list(self.app.state.study_tasks.values()))
            self.assertEqual(execute.call_args.args[1:3], (STUDENT, "u:" + str(STUDENT)))
        self.assertEqual((await self.client.post("/api/files/" + str(self.other_doc) + "/study", json=body)).status_code, 404)

    async def test_local_study_never_sends_to_fallback_console_id(self):
        self.app.state.bot_client = mock.Mock()
        with mock.patch.dict(os.environ, {"MAX_ADMIN_ID": ""}), \
             mock.patch.object(admin_setup, "ADMIN_PATH", self.root / "admin.json"), \
             mock.patch.object(config, "CONSOLE_UID", STUDENT), \
             mock.patch.object(handlers, "_execute_study", new_callable=mock.AsyncMock) as execute:
            enable_local_access(self.app, output=lambda _: None)
            header = {"Authorization": "Local " + self.app.state.local_access["token"]}
            url = "http://127.0.0.1/api/files/" + str(self.doc) + "/study"
            response = await self.client.post(url, headers=header, json={"kind": "quiz"})
            self.assertEqual(response.status_code, 409)
            self.assertIn("ID администратора", response.json()["detail"])
            execute.assert_not_awaited()
            self.assertFalse(self.app.state.study_tasks)

    async def test_admin_can_identify_console_materials(self):
        with mock.patch.object(config, "CONSOLE_UID", STUDENT):
            rows = (await self.client.get("/api/admin/users", headers=self.admin())).json()["users"]
        by_user = {row["user_id"]: row for row in rows}
        self.assertEqual(by_user[STUDENT]["label"], "Консоль")
        self.assertEqual(by_user[ADMIN]["files"], 1)

    async def test_startup_loads_saved_web_settings(self):
        await self.client.patch("/api/admin/settings", headers=self.admin(),
            json={"revision": "0", "patch": {"model": "persisted-model"}})
        config.LLM_MODEL = "different-env-model"
        with mock.patch.object(runtime_settings, "ensure_admin_id"):
            runtime_settings.initialize()
        self.assertEqual(config.LLM_MODEL, "persisted-model")


class BotEntryTests(unittest.IsolatedAsyncioTestCase):
    def test_id_aliases(self):
        self.assertEqual(handlers.parse_command("/id")[0], "id")
        self.assertEqual(handlers.parse_command("/айди")[0], "id")

    async def test_menu_sends_documented_open_app_only_when_configured(self):
        client = MaxClient(TOKEN)
        self.addAsyncCleanup(client.close)
        with mock.patch.object(config, "MAX_WEBAPP_BOT", "offline_test_bot"), \
             mock.patch.object(client, "_send_message", new_callable=mock.AsyncMock) as send:
            await handlers.send_menu(client, "u:" + str(ADMIN), "Материалы")
        body = send.call_args.args[1]
        button = body["attachments"][0]["payload"]["buttons"][0][0]
        self.assertEqual(button, {"type": "open_app", "text": "Открыть мини-приложение",
                                  "web_app": "offline_test_bot", "payload": "files"})
        with mock.patch.object(config, "MAX_WEBAPP_BOT", ""):
            self.assertTrue(all(button["type"] != "open_app"
                                for row in handlers.menu_keyboard(client) for button in row))
