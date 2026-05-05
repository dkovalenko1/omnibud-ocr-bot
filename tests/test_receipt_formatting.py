import unittest

from receipt_formatting import build_excel_string


class ReceiptFormattingTests(unittest.TestCase):
    def test_excel_string_keeps_missing_date_and_hides_none_vendor(self):
        text = build_excel_string(
            {
                "doc_type": "к/чек",
                "doc_number": "2668",
                "date": None,
                "vendor": None,
                "items": ["Бензин"],
            },
        )

        self.assertIn("від —", text)
        self.assertNotIn("None", text)


if __name__ == "__main__":
    unittest.main()
