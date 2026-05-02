import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_SHEET_ID", "test-sheet")


class FakeStatusMessage:
    chat_id = 123

    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit_text(self, text):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class BotWorkflowTests(unittest.TestCase):
    def test_preview_message_escapes_model_text_for_html(self):
        import bot

        preview = bot.build_preview_message([
            {
                "doc_type": "к/чек",
                "doc_number": "1`bad",
                "date": "2026-04-23",
                "vendor": "<script>",
                "items": ["Клей `міцний` <x>"],
                "total": 296,
                "object_name": "Об'єкт <main>",
                "basis": "Касовий чек",
                "description": "Матеріали & інструменти",
                "section": "Роботи",
                "foreman": "Ілля",
            }
        ])

        self.assertIn("&lt;script&gt;", preview)
        self.assertIn("1`bad", preview)
        self.assertIn("&lt;x&gt;", preview)
        self.assertNotIn("<script>", preview)
        self.assertNotIn("`Клей", preview)

    def test_kyiv_message_date_uses_daylight_saving_time(self):
        import bot

        converted = bot._kyiv_message_date(
            datetime(2026, 6, 30, 21, 30, tzinfo=timezone.utc)
        )

        self.assertEqual(converted.date().isoformat(), "2026-07-01")
        self.assertEqual(converted.hour, 0)

    def test_text_only_retry_uses_text_recognizer(self):
        import bot

        state = {
            "source_kind": "text",
            "caption": "Сума: 100",
            "image_paths": [],
        }

        with (
            patch.object(bot, "parse_text_receipt", return_value=[{"total": 100}]) as parse_text,
            patch.object(bot, "extract_receipt_data", side_effect=AssertionError("image OCR called")),
        ):
            receipts = bot._recognize_pending_state(state, retry=True)

        self.assertEqual(receipts, [{"total": 100}])
        parse_text.assert_called_once_with("Сума: 100")


class BotAsyncWorkflowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import bot  # noqa: F401

    async def asyncTearDown(self):
        import bot

        bot.message_buffer.clear()
        bot.pending.clear()

    async def test_process_buffer_runs_recognition_in_worker_thread(self):
        import bot

        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append((func, args, kwargs))
            return [{"total": 100}]

        status = FakeStatusMessage()
        context = SimpleNamespace(bot=FakeBot())
        bot.message_buffer[1] = {
            "image_paths": [],
            "text": "Сума: 100",
            "message_date": datetime(2026, 4, 23, 12, tzinfo=timezone.utc),
            "status_msg": status,
            "timer_task": None,
        }

        with patch.object(bot.asyncio, "to_thread", new=fake_to_thread):
            await bot._process_buffer(1, context)

        self.assertTrue(calls)
        self.assertEqual(bot.pending[1]["source_kind"], "text")
        self.assertEqual(context.bot.messages[0]["parse_mode"], "HTML")


if __name__ == "__main__":
    unittest.main()
