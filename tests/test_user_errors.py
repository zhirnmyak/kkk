from tests import TEST_ROOT  # set up offline isolation before app imports
import asyncio
import unittest
from unittest import mock
import httpx
import user_errors
import ocr_vision
from bot.max_client import MaxApiError


class UserErrorTests(unittest.TestCase):
    def test_api_messages_are_safe_and_actionable(self):
        for code, word in ((401, "ключ"), (403, "доступ"), (429, "лимит"), (503, "недоступен")):
            exc = httpx.HTTPStatusError("SECRET RAW BODY", request=httpx.Request("POST", "https://example.test"),
                                       response=httpx.Response(code))
            text = user_errors.explain(exc)
            self.assertIn(word, text.lower())
            self.assertNotIn("SECRET", text)
        self.assertIn("не успела", user_errors.explain(httpx.ReadTimeout("SECRET")))

    def test_suspended_dialog_is_logged_without_traceback(self):
        from bot import handlers
        client = mock.Mock()
        client.to_event.return_value = {"kind": "message", "user_id": 1, "chat_id": "c:1"}
        error = MaxApiError("error.dialog.suspended", 403)
        with mock.patch.object(handlers, "on_message", mock.AsyncMock(side_effect=error)), \
                self.assertLogs("neurotutor.handlers", level="WARNING") as logs:
            asyncio.run(handlers.handle_update(client, {}))
        self.assertEqual(len(logs.records), 1)
        self.assertIsNone(logs.records[0].exc_info)

    def test_ocr_does_not_turn_total_api_failure_into_unreadable_document(self):
        with mock.patch.object(ocr_vision, "_progress"), mock.patch.object(ocr_vision, "_say"):
            with self.assertRaises(user_errors.ApiFailure):
                ocr_vision._pages_to_text([b"a", b"b"], mock.Mock(side_effect=user_errors.ApiFailure(401)))
