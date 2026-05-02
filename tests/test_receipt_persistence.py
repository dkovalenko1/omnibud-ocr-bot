import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_SHEET_ID", "test-sheet")


class ReceiptPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LEDGER_DB_PATH"] = os.path.join(self.tmp.name, "ledger.db")

        import config
        import ledger_db

        config.LEDGER_DB_PATH = os.environ["LEDGER_DB_PATH"]
        ledger_db.LEDGER_DB_PATH = os.environ["LEDGER_DB_PATH"]
        ledger_db.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_unbound_chat_does_not_append_to_sheet(self):
        from ledger_service import LedgerError
        from receipt_persistence import save_confirmed_receipts

        appender = Mock()

        with self.assertRaises(LedgerError):
            save_confirmed_receipts(
                chat_id=10,
                message_id=200,
                receipts=[{"total": 100, "items": []}],
                caption="caption",
                created_by_user_id=1,
                created_by_name="Accountant",
                created_at=datetime(2026, 4, 23, 12),
                append_receipt_func=appender,
            )

        appender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
