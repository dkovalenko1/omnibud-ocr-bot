import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials
from config import GOOGLE_CREDENTIALS_JSON, GOOGLE_SHEET_ID

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "№",
    "Найменування матеріала",
    "Дата",
    "К-сть",
    "Ціна",
    "Вартість",
    "Об'єкт",
    "Підстава",
    "Пояснення",
    "Розділ",
    "Прораб",
    "Статус",
]

# ─────────────────────────── CONNECTION ───────────────────────────

def get_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_JSON, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID)


# ─────────────────────────── SHEET PER DAY ───────────────────────────

def ensure_header_row(ws):
    actual = ws.row_values(1)
    actual = actual[:len(HEADERS)] + [""] * max(0, len(HEADERS) - len(actual))
    if actual != HEADERS:
        ws.update("A1:L1", [HEADERS], value_input_option="USER_ENTERED")
    ws.format("A1:L1", {"textFormat": {"bold": True}})

def get_or_create_day_sheet(spreadsheet, date: datetime):
    tab_name = date.strftime("%d.%m.%Y")

    existing = [ws.title for ws in spreadsheet.worksheets()]
    if tab_name in existing:
        ws = spreadsheet.worksheet(tab_name)
        ensure_header_row(ws)
        return ws

    # Columns: №, Найменування, Дата, К-сть, Ціна, Вартість, Об'єкт, Підстава, Пояснення, Розділ, Прораб, Статус
    ws = spreadsheet.add_worksheet(title=tab_name, rows=200, cols=12)
    ensure_header_row(ws)
    return ws


# ─────────────────────────── APPEND ROW ───────────────────────────

FORMULA_PREFIXES = ("=", "+", "-", "@")


def _safe_sheet_text(value) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def append_receipt(
    excel_string: str,
    total: float,
    message_date: datetime,
    object_name: str = "—",
    basis: str = "—",
    description: str = "—",
    section: str = "—",
    foreman: str = "—",
) -> dict:
    spreadsheet = get_sheet()
    ws = get_or_create_day_sheet(spreadsheet, message_date)
    ensure_header_row(ws)

    all_values = ws.get_all_values()
    next_row = len(all_values) + 1

    date_str = message_date.strftime("%d.%m.%Y")
    value_formula = f"=E{next_row}*D{next_row}"

    ws.append_row(
        [
            "=ROW()-1",
            _safe_sheet_text(excel_string),
            date_str,
            1,
            total,
            value_formula,
            _safe_sheet_text(object_name),
            _safe_sheet_text(basis),
            _safe_sheet_text(description),
            _safe_sheet_text(section),
            _safe_sheet_text(foreman),
            "",
        ],
        value_input_option="USER_ENTERED",
    )

    sheet_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit#gid={ws.id}"
    return {"url": sheet_url, "tab": ws.title, "row": next_row}


def mark_receipt_canceled(sheet_tab: str, sheet_row: int):
    spreadsheet = get_sheet()
    ws = spreadsheet.worksheet(sheet_tab)
    ensure_header_row(ws)
    ws.update_cell(sheet_row, len(HEADERS), "СКАСОВАНО")
    ws.format(
        f"A{sheet_row}:L{sheet_row}",
        {
            "backgroundColor": {"red": 1.0, "green": 0.82, "blue": 0.82},
        },
    )
