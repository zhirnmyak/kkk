from tests import TEST_ROOT
import asyncio
import json
import unittest
from unittest import mock
import llm
from study_intent import CurrentRequestPermission, CURRENT_REQUEST_SYSTEM


class CurrentRequestPermissionTests(unittest.TestCase):
    def test_only_current_message_is_sent_and_decision_is_cached(self):
        async def check():
            with mock.patch.object(llm, "chat", mock.AsyncMock(return_value='{"quiz":true,"cards":false}')) as chat:
                permission = CurrentRequestPermission("Составь тест")
                self.assertTrue(await permission.allows("quiz"))
                self.assertFalse(await permission.allows("cards"))
                self.assertTrue(await permission.allows("quiz"))
                chat.assert_awaited_once()
                self.assertEqual(chat.call_args.args[0], CURRENT_REQUEST_SYSTEM)
                self.assertEqual(json.loads(chat.call_args.args[1]), {"current_message": "Составь тест"})
        asyncio.run(check())

    def test_invalid_or_non_boolean_answers_deny_both(self):
        async def check():
            for raw in ("bad JSON", "[]", '{"quiz":"true","cards":false}', '{"quiz":true}',
                        '{"quiz":1,"cards":true}'):
                with self.subTest(raw=raw), mock.patch.object(llm, "chat", mock.AsyncMock(return_value=raw)):
                    permission = CurrentRequestPermission("Сообщение")
                    self.assertFalse(await permission.allows("quiz"))
                    self.assertFalse(await permission.allows("cards"))
        asyncio.run(check())

    def test_summary_does_not_need_quiz_or_card_permission(self):
        async def check():
            with mock.patch.object(llm, "chat", mock.AsyncMock()) as chat:
                self.assertTrue(await CurrentRequestPermission("Сделай конспект").allows("summary"))
                chat.assert_not_called()
        asyncio.run(check())
