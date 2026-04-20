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
    "Об'єкт",
    "Дата",
    "К-сть",
    "Ціна",
    "Вартість",
    "Підстава",
    "Пояснення",
    "Розділ",
    "Прораб",
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
        ws.update("A1:K1", [HEADERS], value_input_option="USER_ENTERED")
    ws.format("A1:K1", {"textFormat": {"bold": True}})

def get_or_create_day_sheet(spreadsheet, date: datetime):
    tab_name = date.strftime("%d.%m.%Y")

    existing = [ws.title for ws in spreadsheet.worksheets()]
    if tab_name in existing:
        ws = spreadsheet.worksheet(tab_name)
        ensure_header_row(ws)
        return ws

    # Columns: №, Найменування, Об'єкт, Дата, К-сть, Ціна, Вартість, Підстава, Пояснення, Розділ, Прораб
    ws = spreadsheet.add_worksheet(title=tab_name, rows=200, cols=11)
    ensure_header_row(ws)
    return ws


# ─────────────────────────── APPEND ROW ───────────────────────────

def append_receipt(
    excel_string: str,
    total: float,
    message_date: datetime,
    object_name: str = "—",
    basis: str = "—",
    description: str = "—",
    section: str = "—",
    foreman: str = "—",
) -> str:
    spreadsheet = get_sheet()
    ws = get_or_create_day_sheet(spreadsheet, message_date)
    ensure_header_row(ws)

    all_values = ws.get_all_values()
    next_row = len(all_values) + 1

    date_str = message_date.strftime("%d.%m.%Y")
    value_formula = f"=F{next_row}*E{next_row}"

    ws.append_row(
        ["=ROW()-1", excel_string, object_name, date_str, 1, total, value_formula,
         basis, description, section, foreman],
        value_input_option="USER_ENTERED",
    )

    sheet_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit#gid={ws.id}"
    return sheet_url
