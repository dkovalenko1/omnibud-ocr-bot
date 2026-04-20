import re
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ledger_db import get_connection


UKRAINIAN_MONTHS = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
    5: "травня", 6: "червня", 7: "липня", 8: "серпня",
    9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}


class LedgerError(Exception):
    pass


def parse_amount_to_kopecks(text: str) -> int:
    cleaned = (text or "").strip().lower()
    cleaned = cleaned.replace("грн", "").replace("\u00a0", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        raise LedgerError("Сума має неправильний формат.")

    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise LedgerError("Не вдалося розпізнати суму.") from exc

    kopecks = (amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(kopecks)


def get_chat_account(chat_id: int):
    with get_connection() as conn:
        row = conn.execute(
            """
            select chat_id, foreman_name, opened_at, is_active
            from chat_accounts
            where chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
    return row


def bind_chat_account(chat_id: int, foreman_name: str, opened_at: datetime):
    foreman_name = " ".join((foreman_name or "").split())
    if not foreman_name:
        raise LedgerError("Вкажи ім'я прораба після /bind.")

    with get_connection() as conn:
        conn.execute(
            """
            insert into chat_accounts(chat_id, foreman_name, opened_at, is_active)
            values (?, ?, ?, 1)
            on conflict(chat_id) do update set
              foreman_name = excluded.foreman_name,
              is_active = 1
            """,
            (chat_id, foreman_name, opened_at.isoformat()),
        )


def require_chat_account(chat_id: int):
    account = get_chat_account(chat_id)
    if not account:
        raise LedgerError("Для цього чату ще не налаштовано облік. Виконай /bind Ім'я_Прораба.")
    return account


def _insert_transaction(
    chat_id: int,
    source_key: str,
    message_id: int | None,
    source_kind: str,
    amount_kopecks: int,
    object_name: str | None,
    description: str | None,
    items_text: str | None,
    created_by_user_id: int | None,
    created_by_name: str | None,
    created_at: datetime,
) -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                """
                insert into transactions(
                  chat_id, source_key, message_id, source_kind, amount_kopecks,
                  object_name, description, items_text,
                  created_by_user_id, created_by_name, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    source_key,
                    message_id,
                    source_kind,
                    amount_kopecks,
                    object_name,
                    description,
                    items_text,
                    created_by_user_id,
                    created_by_name,
                    created_at.isoformat(),
                ),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def create_opening_balance(
    chat_id: int,
    amount_kopecks: int,
    created_by_user_id: int | None,
    created_by_name: str | None,
    created_at: datetime,
) -> bool:
    require_chat_account(chat_id)
    return _insert_transaction(
        chat_id=chat_id,
        source_key=f"opening:{chat_id}",
        message_id=None,
        source_kind="opening",
        amount_kopecks=amount_kopecks,
        object_name=None,
        description="Початковий залишок",
        items_text=None,
        created_by_user_id=created_by_user_id,
        created_by_name=created_by_name,
        created_at=created_at,
    )


def create_topup_transaction(
    chat_id: int,
    message_id: int,
    amount_kopecks: int,
    description: str | None,
    created_by_user_id: int | None,
    created_by_name: str | None,
    created_at: datetime,
) -> bool:
    require_chat_account(chat_id)
    return _insert_transaction(
        chat_id=chat_id,
        source_key=f"topup:{chat_id}:{message_id}",
        message_id=message_id,
        source_kind="topup",
        amount_kopecks=amount_kopecks,
        object_name=None,
        description=(description or "").strip() or "Поповнення підзвіту",
        items_text=None,
        created_by_user_id=created_by_user_id,
        created_by_name=created_by_name,
        created_at=created_at,
    )


def create_adjustment_transaction(
    chat_id: int,
    message_id: int | None,
    amount_kopecks: int,
    description: str | None,
    created_by_user_id: int | None,
    created_by_name: str | None,
    created_at: datetime,
) -> bool:
    require_chat_account(chat_id)
    return _insert_transaction(
        chat_id=chat_id,
        source_key=f"adjustment:{chat_id}:{message_id or created_at.isoformat()}",
        message_id=message_id,
        source_kind="adjustment",
        amount_kopecks=amount_kopecks,
        object_name=None,
        description=(description or "").strip() or "Ручне коригування",
        items_text=None,
        created_by_user_id=created_by_user_id,
        created_by_name=created_by_name,
        created_at=created_at,
    )


def create_receipt_transactions(
    chat_id: int,
    message_id: int,
    receipts: list[dict],
    description: str | None,
    created_by_user_id: int | None,
    created_by_name: str | None,
    created_at: datetime,
) -> int:
    require_chat_account(chat_id)
    inserted = 0
    raw_description = (description or "").strip()

    for idx, receipt in enumerate(receipts, 1):
        total_kopecks = parse_amount_to_kopecks(str(receipt.get("total") or "0"))
        items = receipt.get("items") or []
        items_text = "; ".join(str(item) for item in items) or None
        source_key = f"receipt:{chat_id}:{message_id}:{idx}"
        tx_description = raw_description or receipt.get("description") or "Витрата за чеком"

        created = _insert_transaction(
            chat_id=chat_id,
            source_key=source_key,
            message_id=message_id,
            source_kind="receipt",
            amount_kopecks=-total_kopecks,
            object_name=receipt.get("object_name") or None,
            description=tx_description,
            items_text=items_text,
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
            created_at=created_at,
        )
        if created:
            inserted += 1

    return inserted


def get_balance_kopecks(chat_id: int) -> int:
    require_chat_account(chat_id)
    with get_connection() as conn:
        value = conn.execute(
            """
            select coalesce(sum(amount_kopecks), 0) as balance_kopecks
            from transactions
            where chat_id = ?
            """,
            (chat_id,),
        ).fetchone()["balance_kopecks"]
    return int(value or 0)


def format_amount_kopecks(amount_kopecks: int) -> str:
    sign = "+" if amount_kopecks >= 0 else "-"
    absolute = abs(amount_kopecks)
    hryvnia = absolute // 100
    kopecks = absolute % 100
    hryvnia_text = f"{hryvnia:,}".replace(",", " ")
    return f"{sign} {hryvnia_text},{kopecks:02d} грн"


def format_balance_message(amount_kopecks: int, as_of: datetime) -> str:
    date_text = f"{as_of.day} {UKRAINIAN_MONTHS[as_of.month]} {as_of.year} р"
    amount_text = format_amount_kopecks(amount_kopecks)
    return (
        "Залишок грошових коштів у ПІДЗВІТІ за об'єктами "
        f"на {date_text}: {amount_text}"
    )


TOPUP_RE = re.compile(r"^\s*получил\s+([+-]?\d[\d\s.,]*)\s*$", re.IGNORECASE)


def parse_topup_message(text: str) -> int | None:
    match = TOPUP_RE.match(text or "")
    if not match:
        return None
    return parse_amount_to_kopecks(match.group(1))
