import os
import unittest
from datetime import datetime

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_SHEET_ID", "sheet123")


class FakeWorksheet:
    title = "23.04.2026"
    id = 42

    def __init__(self):
        self.rows = [[""] * 12]
        self.appended = None
        self.updated = []
        self.formatted = []
        self.updated_cells = []

    def row_values(self, row):
        return self.rows[row - 1]

    def update(self, range_name, values, value_input_option=None):
        self.updated.append((range_name, values, value_input_option))
        self.rows[0] = values[0]

    def format(self, range_name, body):
        self.formatted.append((range_name, body))

    def get_all_values(self):
        return self.rows

    def append_row(self, values, value_input_option=None):
        self.appended = (values, value_input_option)
        self.rows.append(values)

    def update_cell(self, row, col, value):
        self.updated_cells.append((row, col, value))


class FakeSpreadsheet:
    def __init__(self, ws):
        self.ws = ws

    def worksheets(self):
        return [self.ws]

    def worksheet(self, title):
        if title != self.ws.title:
            raise KeyError(title)
        return self.ws


class SheetsServiceTests(unittest.TestCase):
    def test_append_receipt_uses_new_column_order_and_returns_row(self):
        import sheets_service

        ws = FakeWorksheet()
        sheets_service.GOOGLE_SHEET_ID = "sheet123"
        sheets_service.get_sheet = lambda: FakeSpreadsheet(ws)

        result = sheets_service.append_receipt(
            excel_string="к/чек № 1",
            total=296,
            message_date=datetime(2026, 4, 23, 12, 0, 0),
            object_name="Об'єкт",
            basis="Касовий чек",
            description="Матеріали",
            section="Роботи",
            foreman="Ілля",
        )

        self.assertEqual(result, {"url": "https://docs.google.com/spreadsheets/d/sheet123/edit#gid=42", "tab": "23.04.2026", "row": 2})
        self.assertEqual(
            ws.appended[0],
            [
                "=ROW()-1",
                "к/чек № 1",
                "23.04.2026",
                1,
                296,
                "=E2*D2",
                "Об'єкт",
                "Касовий чек",
                "Матеріали",
                "Роботи",
                "Ілля",
                "",
            ],
        )

    def test_mark_receipt_canceled_sets_status_and_red_row(self):
        import sheets_service

        ws = FakeWorksheet()
        ws.rows[0] = sheets_service.HEADERS
        sheets_service.get_sheet = lambda: FakeSpreadsheet(ws)

        sheets_service.mark_receipt_canceled("23.04.2026", 5)

        self.assertEqual(ws.updated_cells, [(5, 12, "СКАСОВАНО")])
        self.assertEqual(ws.formatted[-1][0], "A5:L5")
        self.assertNotIn("strikethrough", ws.formatted[-1][1].get("textFormat", {}))

    def test_append_receipt_escapes_formula_like_text_fields(self):
        import sheets_service

        ws = FakeWorksheet()
        sheets_service.GOOGLE_SHEET_ID = "sheet123"
        sheets_service.get_sheet = lambda: FakeSpreadsheet(ws)

        sheets_service.append_receipt(
            excel_string="=IMPORTXML(\"https://example.com\", \"//title\")",
            total=296,
            message_date=datetime(2026, 4, 23, 12, 0, 0),
            object_name="+Об'єкт",
            basis="-Підстава",
            description="@Пояснення",
            section="=Розділ",
            foreman="Ілля",
        )

        row = ws.appended[0]
        self.assertEqual(row[1], "'=IMPORTXML(\"https://example.com\", \"//title\")")
        self.assertEqual(row[6], "'+Об'єкт")
        self.assertEqual(row[7], "'-Підстава")
        self.assertEqual(row[8], "'@Пояснення")
        self.assertEqual(row[9], "'=Розділ")


if __name__ == "__main__":
    unittest.main()
