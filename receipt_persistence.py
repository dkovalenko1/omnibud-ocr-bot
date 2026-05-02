import html
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ledger_service import (
    LedgerError,
    create_saved_receipt_transactions,
    ensure_receipts_can_be_saved,
    format_balance_message,
    get_balance_kopecks,
)
from receipt_formatting import build_excel_string
from sheets_service import append_receipt, mark_receipt_canceled


@dataclass
class SaveReceiptsResult:
    sheet_url: str
    saved_lines: list[str]
    saved_receipts: list[dict]
    balance_text: str


AppendReceiptFunc = Callable[[str, float, datetime, str, str, str, str, str], dict]
CancelReceiptFunc = Callable[[str, int], None]


def _cancel_sheet_rows(sheet_records: list[dict], cancel_receipt_func: CancelReceiptFunc) -> None:
    for record in sheet_records:
        try:
            cancel_receipt_func(str(record["tab"]), int(record["row"]))
        except Exception:
            logging.exception("Failed to cancel sheet row after ledger save failure")


def save_confirmed_receipts(
    chat_id: int,
    message_id: int,
    receipts: list[dict],
    caption: str,
    created_by_user_id: int | None,
    created_by_name: str | None,
    created_at: datetime,
    append_receipt_func: AppendReceiptFunc = append_receipt,
    cancel_receipt_func: CancelReceiptFunc = mark_receipt_canceled,
) -> SaveReceiptsResult:
    ensure_receipts_can_be_saved(chat_id, message_id, receipts)

    sheet_records = []
    saved_lines = []
    sheet_url = ""

    try:
        for data in receipts:
            excel_str = build_excel_string(data)
            total = data.get("total") or 0
            object_name = data.get("object_name") or "—"
            basis = data.get("basis") or "—"
            description = data.get("description") or "—"
            section = data.get("section") or "—"
            foreman = data.get("foreman") or "—"
            sheet_record = append_receipt_func(
                excel_str, total, created_at, object_name,
                basis, description, section, foreman,
            )
            sheet_url = sheet_record["url"]
            sheet_records.append(sheet_record)
            snippet = f"{excel_str[:60]}{'...' if len(excel_str) > 60 else ''}"
            saved_lines.append(
                f"• {html.escape(snippet)}"
                f" — <b>{html.escape(str(total))} грн</b> ({html.escape(object_name)})"
            )

        saved_receipts = create_saved_receipt_transactions(
            chat_id=chat_id,
            message_id=message_id,
            receipts=receipts,
            sheet_records=sheet_records,
            description=caption,
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
            created_at=created_at,
        )
        if len(saved_receipts) != len(receipts):
            raise LedgerError("Не всі чеки вдалося записати в облік.")

        return SaveReceiptsResult(
            sheet_url=sheet_url,
            saved_lines=saved_lines,
            saved_receipts=saved_receipts,
            balance_text=format_balance_message(get_balance_kopecks(chat_id), created_at),
        )
    except Exception:
        _cancel_sheet_rows(sheet_records, cancel_receipt_func)
        raise
