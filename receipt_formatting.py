import html
from datetime import datetime


UKRAINIAN_MONTHS = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
    5: "травня", 6: "червня", 7: "липня", 8: "серпня",
    9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}


def receipt_count_word(n: int) -> str:
    if n == 1:
        return "чек"
    if 2 <= n <= 4:
        return "чеки"
    return "чеків"


def format_date_ua(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.day} {UKRAINIAN_MONTHS[d.month]} {d.year} р"
    except Exception:
        return date_str or "—"


def _text_or_default(value, default: str = "—") -> str:
    if value in (None, ""):
        return default
    return str(value)


def build_excel_string(data: dict) -> str:
    doc_type = _text_or_default(data.get("doc_type"), "к/чек")
    doc_number = _text_or_default(data.get("doc_number"), "б/н")
    date_ua = format_date_ua(data.get("date") or "")
    vendor = _text_or_default(data.get("vendor"), "")
    items = data.get("items", [])

    result = f"{doc_type} № {doc_number} від {date_ua}"
    if vendor:
        result += f" {vendor}"
    if items:
        result += f" ({'; '.join(str(item) for item in items)})"

    return result


def _html_text(value) -> str:
    return html.escape(str(value), quote=False)


def _html_value(value) -> str:
    return _html_text(value if value not in (None, "") else "—")


def build_preview_message(receipts: list) -> str:
    lines = ["📋 <b>Результат розпізнавання:</b>", ""]

    n = len(receipts)
    if n > 1:
        lines.append(f"🧾 Знайдено <b>{n} {receipt_count_word(n)}</b> на фото\n")

    for i, data in enumerate(receipts, 1):
        total = data.get("total")
        total_str = str(total) if total is not None else "—"

        if n > 1:
            lines.append(f"<b>Чек {i}:</b>")

        doc_type = data.get("doc_type", "к/чек")
        doc_number = data.get("doc_number", "б/н")
        date_ua = format_date_ua(data.get("date", ""))
        vendor = data.get("vendor", "") or "—"
        items = data.get("items", [])

        lines.append("<b>Документ:</b>")
        lines.append(f"Тип: <code>{_html_value(doc_type)}</code>")
        lines.append(f"№ <code>{_html_value(doc_number)}</code> від <code>{_html_value(date_ua)}</code>")
        lines.append(f"Постачальник: <code>{_html_value(vendor)}</code>")
        if items:
            lines.append("\n<b>Товари:</b>")
            for j, item in enumerate(items, 1):
                lines.append(f"  {j}. <code>{_html_value(item)}</code>")
        lines.append(f"\n<b>Сума:</b> {_html_text(total_str)} грн")
        lines.append(f"\n<b>Об'єкт:</b> {_html_value(data.get('object_name'))}")
        lines.append(f"\n<b>Підстава:</b> {_html_value(data.get('basis'))}")
        lines.append(f"\n<b>Пояснення:</b> {_html_value(data.get('description'))}")
        lines.append(f"\n<b>Розділ:</b> {_html_value(data.get('section'))}")
        lines.append(f"\n<b>Прораб:</b> {_html_value(data.get('foreman'))}")
        if i < n:
            lines.append("")

    return "\n".join(lines)
