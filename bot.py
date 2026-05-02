import asyncio
import tempfile
import os
import logging
import html
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from config import TELEGRAM_BOT_TOKEN
from ledger_db import init_db
from ledger_service import (
    LedgerError,
    bind_chat_account,
    unbind_chat_account,
    create_adjustment_transaction,
    create_opening_balance,
    create_topup_transaction,
    format_amount_kopecks,
    format_balance_message,
    get_balance_kopecks,
    parse_amount_to_kopecks,
    parse_topup_message,
    undo_saved_receipt,
)
from receipt_formatting import (
    build_preview_message,
    receipt_count_word as _receipt_count_word,
)
from receipt_persistence import save_confirmed_receipts
from receipt_recognition import (
    MAX_IMAGES,
    extract_receipt_data,
    parse_text_receipt,
    pdf_to_images,
)
from sheets_service import mark_receipt_canceled
from time_utils import kyiv_message_date as _kyiv_message_date, to_utc as _to_utc

# In-memory store: user_id -> confirmed receipt session
pending = {}

# Message buffer: user_id -> {image_paths, text, message_date, timer_task}
message_buffer: dict[int, dict] = {}

# Seconds to wait for more messages before processing
BUFFER_TIMEOUT = 2.0

# Ignore all messages sent before this moment (bot was offline)
BOT_START_TIME = datetime.now(timezone.utc)

# ─────────────────────────── HELPERS ───────────────────────────

def _delete_files(paths: list[str]):
    """Silently delete a list of temp files."""
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _recognize_pending_state(state: dict, retry: bool = False) -> list:
    if state.get("source_kind") == "text":
        return parse_text_receipt(state.get("caption", ""))
    return extract_receipt_data(
        state.get("image_paths", []),
        caption=state.get("caption", ""),
        retry=retry,
    )


def _actor_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    full_name = " ".join(part for part in parts if part).strip()
    return full_name or user.username or f"user-{user.id}"

async def _reply_balance(chat_id: int, context: ContextTypes.DEFAULT_TYPE, as_of: datetime):
    balance = await asyncio.to_thread(get_balance_kopecks, chat_id)
    await context.bot.send_message(chat_id=chat_id, text=format_balance_message(balance, as_of))


async def bind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    foreman_name = " ".join(context.args).strip()
    try:
        await asyncio.to_thread(
            bind_chat_account,
            update.effective_chat.id,
            foreman_name,
            _kyiv_message_date(message.date),
        )
    except LedgerError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return

    await message.reply_text(f"✅ Чат прив'язано до прораба: {foreman_name}")


async def opening_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    if not context.args:
        await message.reply_text("⚠️ Використання: /opening 25000")
        return

    try:
        amount_kopecks = parse_amount_to_kopecks(" ".join(context.args))
        created = await asyncio.to_thread(
            create_opening_balance,
            chat_id=update.effective_chat.id,
            amount_kopecks=amount_kopecks,
            created_by_user_id=update.effective_user.id,
            created_by_name=_actor_name(update.effective_user),
            created_at=_kyiv_message_date(message.date),
        )
    except LedgerError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return

    if not created:
        await message.reply_text("⚠️ Початковий залишок уже задано. Для змін використовуй /adjust.")
        return

    await message.reply_text(f"✅ Початковий залишок встановлено: {format_amount_kopecks(amount_kopecks)}")
    await _reply_balance(update.effective_chat.id, context, _kyiv_message_date(message.date))


async def unbind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    try:
        foreman_name = await asyncio.to_thread(unbind_chat_account, update.effective_chat.id)
    except LedgerError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return

    await message.reply_text(f"✅ Облік для чату вимкнено. Було прив'язано: {foreman_name}")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    try:
        await _reply_balance(update.effective_chat.id, context, _kyiv_message_date(message.date))
    except LedgerError as exc:
        await message.reply_text(f"⚠️ {exc}")


async def adjust_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        await message.reply_text("⚠️ Використання: /adjust -500 Помилка у попередньому записі")
        return

    raw = body[1].strip()
    parts = raw.split(maxsplit=1)
    amount_token = parts[0]
    note = parts[1].strip() if len(parts) > 1 else "Ручне коригування"

    try:
        amount_kopecks = parse_amount_to_kopecks(amount_token)
        created = await asyncio.to_thread(
            create_adjustment_transaction,
            chat_id=update.effective_chat.id,
            message_id=message.message_id,
            amount_kopecks=amount_kopecks,
            description=note,
            created_by_user_id=update.effective_user.id,
            created_by_name=_actor_name(update.effective_user),
            created_at=_kyiv_message_date(message.date),
        )
    except LedgerError as exc:
        await message.reply_text(f"⚠️ {exc}")
        return

    if not created:
        await message.reply_text("⚠️ Це коригування вже враховано.")
        return

    await message.reply_text(f"✅ Коригування записано: {format_amount_kopecks(amount_kopecks)}")
    await _reply_balance(update.effective_chat.id, context, _kyiv_message_date(message.date))


# ─────────────────────────── BUFFER ───────────────────────────

async def _process_buffer(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Called when the buffer timer fires. Process all accumulated messages."""
    buf = message_buffer.pop(user_id, None)
    if not buf:
        return

    image_paths: list[str] = buf["image_paths"]
    caption: str = buf["text"].strip()
    message_date: datetime = buf["message_date"]
    status_msg = buf["status_msg"]

    TEXT_ONLY_KEYWORDS = ("сума:", "назва об'єкту:", "пояснення:", "підстава:", "розділ:")
    is_text_only = not image_paths and any(kw in caption.lower() for kw in TEXT_ONLY_KEYWORDS)

    if not image_paths and not is_text_only:
        await status_msg.edit_text("⚠️ Надішли фото або PDF чека.")
        return

    if is_text_only:
        await status_msg.edit_text("⏳ Розпізнаю текстовий запис...")
    else:
        img_count = len(image_paths)
        await status_msg.edit_text(
            f"⏳ Розпізнаю {f'{img_count} {_receipt_count_word(img_count)}'}..."
        )

    source_kind = "text" if is_text_only else "image"
    recognition_state = {
        "image_paths": image_paths,
        "caption": caption,
        "source_kind": source_kind,
    }

    try:
        receipts = await asyncio.to_thread(_recognize_pending_state, recognition_state, False)

        pending[user_id] = {
            **recognition_state,
            "receipts": receipts,
            "retry_count": 0,
            "message_date": message_date,
        }

        preview = build_preview_message(receipts)
        keyboard = _confirm_keyboard()

        await status_msg.delete()
        await context.bot.send_message(
            chat_id=status_msg.chat_id,
            text=preview,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception as e:
        _delete_files(image_paths)
        await status_msg.edit_text(f"❌ Помилка розпізнавання: {e}")


async def _schedule_processing(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Wait BUFFER_TIMEOUT seconds then process the buffer."""
    await asyncio.sleep(BUFFER_TIMEOUT)
    await _process_buffer(user_id, context)


def _reset_timer(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Cancel existing timer and start a new one."""
    buf = message_buffer[user_id]
    old_task = buf.get("timer_task")
    if old_task and not old_task.done():
        old_task.cancel()
    buf["timer_task"] = asyncio.create_task(_schedule_processing(user_id, context))


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Зберегти в таблицю", callback_data="confirm"),
            InlineKeyboardButton("🔄 Повторити", callback_data="retry"),
        ]
    ])


def _undo_keyboard(saved_receipts: list[dict]) -> InlineKeyboardMarkup | None:
    if not saved_receipts:
        return None
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"↩️ Скасувати чек {record['receipt_index']}",
                callback_data=f"undo_receipt:{record['saved_receipt_id']}",
            )
        ]
        for record in saved_receipts
    ])


def _remove_undo_button(
    keyboard: InlineKeyboardMarkup | None,
    callback_data: str,
) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None

    rows = []
    for row in keyboard.inline_keyboard:
        buttons = []
        for button in row:
            if button.callback_data != callback_data:
                buttons.append(button)
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(rows) if rows else None


def _saved_message_html(message) -> str:
    text_html = getattr(message, "text_html", None)
    if text_html:
        return text_html
    return html.escape(getattr(message, "text", "") or "", quote=False)


def _remove_saved_status_line(text: str) -> str:
    lines = text.splitlines()
    filtered = [line for line in lines if "Збережено" not in line]
    while filtered and not filtered[0].strip():
        filtered.pop(0)
    return "\n".join(filtered)


def _build_undone_message_text(message, receipt_index: int) -> str:
    notice = f"❌ <b>СКАСОВАНО: Чек {receipt_index}</b>"
    current_text = _remove_saved_status_line(_saved_message_html(message))
    if f"СКАСОВАНО: Чек {receipt_index}" in current_text:
        return current_text
    return f"{notice}\n\n{current_text}"


# ─────────────────────────── HANDLERS ───────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привіт! Я допомагаю автоматично розпізнавати чеки.\n\n"
        "Надішли фото або PDF чека — я розпізнаю дані та сформую рядок для таблиці Excel.\n\n"
        "Можна надіслати кілька фото одного чека — я зберу їх разом.\n"
        "Можна надіслати підпис окремим повідомленням одразу після фото.\n\n"
        "Формат рядка:\n"
        "`к/чек № 162681 від 26 лютого 2025 р ЕПІЦЕНТР (Товар 1; Товар 2)`\n\n"
        "Команди:\n"
        "/start — це повідомлення\n"
        "/help — довідка\n"
        "/bind Ім'я Прізвище — прив'язати чат до прораба\n"
        "/unbind — вимкнути облік для цього чату\n"
        "/opening 25000 — початковий залишок\n"
        "/balance — поточний залишок\n"
        "/adjust -500 Корекція — ручне коригування"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Як користуватись:*\n\n"
        "1. Надішли фото або PDF чека (касового, товарного, рахунку-фактури)\n"
        "2. Якщо чек великий — надішли кілька фото підряд, я об'єднаю\n"
        "3. Підпис можна написати окремим повідомленням одразу після фото\n"
        "4. Я розпізнаю дані та покажу рядок для Excel\n"
        "5. Перевір результат — натисни ✅ *Зберегти* або 🔄 *Повторити розпізнавання*\n\n"
        "*Облік підзвіту:*\n"
        "/bind Ім'я Прізвище — налаштувати чат\n"
        "/unbind — вимкнути облік для цього чату\n"
        "/opening 25000 — встановити початковий залишок\n"
        "/balance — показати поточний залишок\n"
        "/adjust -500 Корекція — ручне коригування\n"
        "Текст `Получил 30000` — поповнення балансу\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def _ensure_buffer(user_id: int, message_date: datetime, update: Update) -> None:
    """Initialize buffer for user if not exists."""
    if user_id not in message_buffer:
        status_msg = await update.message.reply_text("📨 Збираю повідомлення...")
        message_buffer[user_id] = {
            "image_paths": [],
            "text": "",
            "message_date": message_date,
            "status_msg": status_msg,
            "timer_task": None,
        }


def _is_old_message(message_date: datetime) -> bool:
    return _to_utc(message_date) < BOT_START_TIME


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message_date = update.message.date

    if _is_old_message(message_date):
        return

    caption = update.message.caption or ""

    await _ensure_buffer(user_id, message_date, update)
    buf = message_buffer[user_id]

    if len(buf["image_paths"]) >= MAX_IMAGES:
        await buf["status_msg"].edit_text(
            f"⚠️ Максимум {MAX_IMAGES} зображень за один раз. Надішли менше фото."
        )
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    await file.download_to_drive(tmp.name)
    tmp.close()

    buf["image_paths"].append(tmp.name)
    if caption:
        buf["text"] += ("\n" if buf["text"] else "") + caption

    img_count = len(buf["image_paths"])
    await buf["status_msg"].edit_text(
        f"📨 Отримано {img_count} фото"
        + (", підпис є" if buf["text"] else "")
        + f" — чекаю ще {BUFFER_TIMEOUT:.0f} сек..."
    )

    _reset_timer(user_id, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message_date = update.message.date

    if _is_old_message(message_date):
        return

    doc = update.message.document
    caption = update.message.caption or ""

    await _ensure_buffer(user_id, message_date, update)
    buf = message_buffer[user_id]

    await buf["status_msg"].edit_text("📥 Завантажую файл...")

    file = await doc.get_file()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    await file.download_to_drive(tmp.name)
    tmp.close()

    try:
        image_paths = await asyncio.to_thread(pdf_to_images, tmp.name)
    except Exception as e:
        await buf["status_msg"].edit_text(f"❌ {e}")
        message_buffer.pop(user_id, None)
        return
    finally:
        os.unlink(tmp.name)

    total_after = len(buf["image_paths"]) + len(image_paths)
    if total_after > MAX_IMAGES:
        _delete_files(image_paths)
        await buf["status_msg"].edit_text(
            f"⚠️ Забагато зображень: PDF дає {len(image_paths)} стор., "
            f"разом {total_after} > {MAX_IMAGES}. Надішли менший файл."
        )
        return

    buf["image_paths"].extend(image_paths)
    if caption:
        buf["text"] += ("\n" if buf["text"] else "") + caption

    pages = len(image_paths)
    await buf["status_msg"].edit_text(
        f"📨 PDF отримано ({pages} стор.) — чекаю ще {BUFFER_TIMEOUT:.0f} сек..."
    )

    _reset_timer(user_id, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect text messages. If no buffer exists and text looks like a signature — start a text-only buffer."""
    user_id = update.effective_user.id
    message_date = update.message.date

    if _is_old_message(message_date):
        return

    text = update.message.text or ""
    if not text:
        return

    if user_id not in message_buffer:
        try:
            topup_kopecks = parse_topup_message(text)
        except LedgerError as exc:
            await update.message.reply_text(f"⚠️ {exc}")
            return
        if topup_kopecks is not None:
            try:
                created = await asyncio.to_thread(
                    create_topup_transaction,
                    chat_id=update.effective_chat.id,
                    message_id=update.message.message_id,
                    amount_kopecks=topup_kopecks,
                    description=text,
                    created_by_user_id=user_id,
                    created_by_name=_actor_name(update.effective_user),
                    created_at=_kyiv_message_date(message_date),
                )
            except LedgerError as exc:
                await update.message.reply_text(f"⚠️ {exc}")
                return

            if not created:
                await update.message.reply_text("⚠️ Це поповнення вже враховано.")
                return

            await update.message.reply_text(f"✅ Поповнення записано: {format_amount_kopecks(topup_kopecks)}")
            await _reply_balance(update.effective_chat.id, context, _kyiv_message_date(message_date))
            return

    TEXT_ONLY_KEYWORDS = ("сума:", "назва об'єкту:", "пояснення:", "підстава:", "розділ:")
    is_signature = any(kw in text.lower() for kw in TEXT_ONLY_KEYWORDS)

    if user_id not in message_buffer:
        if not is_signature:
            return
        # Start a text-only buffer
        await _ensure_buffer(user_id, message_date, update)

    buf = message_buffer[user_id]
    buf["text"] += ("\n" if buf["text"] else "") + text

    if buf["image_paths"]:
        await buf["status_msg"].edit_text(
            f"📨 Отримано {len(buf['image_paths'])} фото + підпис"
            f" — чекаю ще {BUFFER_TIMEOUT:.0f} сек..."
        )
    else:
        await buf["status_msg"].edit_text(
            f"📨 Отримано текстовий запис — чекаю ще {BUFFER_TIMEOUT:.0f} сек..."
        )

    _reset_timer(user_id, context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id

    if query.data == "noop":
        await query.answer()
        return

    if query.data and query.data.startswith("undo_receipt:"):
        try:
            saved_receipt_id = int(query.data.split(":", 1)[1])
            undone_at = _kyiv_message_date(datetime.now(timezone.utc))
            undone = await asyncio.to_thread(
                undo_saved_receipt,
                saved_receipt_id=saved_receipt_id,
                undone_by_user_id=query.from_user.id,
                undone_by_name=_actor_name(query.from_user),
                undone_at=undone_at,
            )
            await asyncio.to_thread(mark_receipt_canceled, undone["sheet_tab"], undone["sheet_row"])

            if not undone["already_undone"]:
                balance_text = format_balance_message(
                    await asyncio.to_thread(get_balance_kopecks, undone["chat_id"]),
                    undone_at,
                )
                await context.bot.send_message(chat_id=undone["chat_id"], text=balance_text)

            new_keyboard = _remove_undo_button(
                query.message.reply_markup,
                query.data,
            )
            await query.edit_message_text(
                text=_build_undone_message_text(query.message, undone["receipt_index"]),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=new_keyboard,
            )
            await query.answer("Чек скасовано")
        except Exception as exc:
            await query.answer(f"Помилка скасування: {exc}", show_alert=True)
        return

    await query.answer()

    if user_id not in pending:
        await query.edit_message_text("⚠️ Сесія закінчилась. Надішли фото ще раз.")
        return

    state = pending[user_id]

    if query.data == "confirm":
        receipts = state["receipts"]
        # Use Kyiv local time for the sheet tab name
        message_date_kyiv = _kyiv_message_date(state["message_date"])
        tab_name = message_date_kyiv.strftime("%d.%m.%Y")

        await query.edit_message_text("💾 Зберігаю в Google Sheets...")

        try:
            result = await asyncio.to_thread(
                save_confirmed_receipts,
                chat_id=update.effective_chat.id,
                message_id=query.message.message_id,
                receipts=receipts,
                caption=state["caption"],
                created_by_user_id=query.from_user.id,
                created_by_name=_actor_name(query.from_user),
                created_at=message_date_kyiv,
            )

            saved_text = "\n".join(result.saved_lines)
            count = len(receipts)
            sheet_link = f'<a href="{html.escape(result.sheet_url, quote=True)}">Відкрити таблицю</a>'
            text = (
                f"✅ <b>Збережено {count} {_receipt_count_word(count)}!</b>\n\n"
                f"📅 Сторінка: <code>{html.escape(tab_name)}</code>\n\n"
                f"{saved_text}\n\n"
                f"{sheet_link}"
                f"\n\n{html.escape(result.balance_text)}"
            )
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_undo_keyboard(result.saved_receipts),
            )
            del pending[user_id]
            _delete_files(state["image_paths"])

        except Exception as e:
            await query.edit_message_text(f"❌ Помилка збереження: {e}")
            # Keep pending so user can retry saving

    elif query.data == "retry":
        if state["retry_count"] >= 2:
            await query.edit_message_text(
                "⚠️ Не вдалось розпізнати чек після кількох спроб.\n"
                "Спробуй зробити чіткіше фото або введи дані вручну."
            )
            _delete_files(state["image_paths"])
            del pending[user_id]
            return

        await query.edit_message_text("🔄 Повторюю розпізнавання...")

        try:
            new_data = await asyncio.to_thread(_recognize_pending_state, state, True)
            state["receipts"] = new_data
            state["retry_count"] += 1

            preview = build_preview_message(new_data)
            await query.edit_message_text(preview, parse_mode="HTML", reply_markup=_confirm_keyboard())

        except Exception as e:
            await query.edit_message_text(f"❌ Помилка: {e}")
            _delete_files(state["image_paths"])
            del pending[user_id]


# ─────────────────────────── MAIN ───────────────────────────

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Suppress transient network errors; log everything else."""
    if isinstance(context.error, (NetworkError, TimedOut)):
        logging.debug("Network glitch (ignored): %s", context.error)
        return
    logging.error("Unhandled exception", exc_info=context.error)


def main():
    logging.basicConfig(level=logging.WARNING)
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("bind", bind_cmd))
    app.add_handler(CommandHandler("unbind", unbind_cmd))
    app.add_handler(CommandHandler("opening", opening_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("adjust", adjust_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(handle_error)

    print("✅ Бот запущено...")
    app.run_polling()


if __name__ == "__main__":
    main()
