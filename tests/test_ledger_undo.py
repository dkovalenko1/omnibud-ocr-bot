import os
import tempfile
import unittest
from datetime import datetime

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_SHEET_ID", "test-sheet")


class LedgerUndoTests(unittest.TestCase):
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

    def test_voided_receipt_is_removed_from_balance(self):
        from ledger_service import (
            bind_chat_account,
            create_opening_balance,
            create_saved_receipt_transactions,
            get_balance_kopecks,
            undo_saved_receipt,
        )

        now = datetime(2026, 4, 23, 12, 0, 0)
        bind_chat_account(10, "Ілля", now)
        create_opening_balance(10, 10_000_00, 1, "Accountant", now)
        created = create_saved_receipt_transactions(
            chat_id=10,
            message_id=200,
            receipts=[{"total": 296, "object_name": "Об'єкт", "items": ["Клей"]}],
            sheet_records=[{"tab": "23.04.2026", "row": 7}],
            description="caption",
            created_by_user_id=2,
            created_by_name="Foreman",
            created_at=now,
        )

        self.assertEqual(created[0]["saved_receipt_id"], 1)
        self.assertEqual(get_balance_kopecks(10), 9_704_00)

        undone = undo_saved_receipt(
            saved_receipt_id=1,
            undone_by_user_id=1,
            undone_by_name="Accountant",
            undone_at=now,
        )

        self.assertEqual(undone["sheet_tab"], "23.04.2026")
        self.assertEqual(undone["sheet_row"], 7)
        self.assertEqual(get_balance_kopecks(10), 10_000_00)

    def test_undo_is_idempotent(self):
        from ledger_service import (
            bind_chat_account,
            create_opening_balance,
            create_saved_receipt_transactions,
            undo_saved_receipt,
        )

        now = datetime(2026, 4, 23, 12, 0, 0)
        bind_chat_account(10, "Ілля", now)
        create_opening_balance(10, 1_000_00, 1, "Accountant", now)
        create_saved_receipt_transactions(
            chat_id=10,
            message_id=200,
            receipts=[{"total": 100, "object_name": "Об'єкт", "items": []}],
            sheet_records=[{"tab": "23.04.2026", "row": 2}],
            description="caption",
            created_by_user_id=2,
            created_by_name="Foreman",
            created_at=now,
        )

        first = undo_saved_receipt(1, 1, "Accountant", now)
        second = undo_saved_receipt(1, 1, "Accountant", now)

        self.assertFalse(first["already_undone"])
        self.assertTrue(second["already_undone"])


if __name__ == "__main__":
    unittest.main()
