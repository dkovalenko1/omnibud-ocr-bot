import asyncio
import json
import base64
import tempfile
import os
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from openai import OpenAI

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
from config import OPENAI_API_KEY, TELEGRAM_BOT_TOKEN
from ledger_db import init_db
from ledger_service import (
    LedgerError,
    bind_chat_account,
    unbind_chat_account,
    create_adjustment_transaction,
    create_opening_balance,
    create_receipt_transactions,
    create_topup_transaction,
    format_amount_kopecks,
    format_balance_message,
    get_balance_kopecks,
    parse_amount_to_kopecks,
    parse_topup_message,
)
from sheets_service import append_receipt

load_dotenv()
client = OpenAI(api_key=OPENAI_API_KEY)

# In-memory store: user_id -> confirmed receipt session
pending = {}

# Message buffer: user_id -> {image_paths, text, message_date, timer_task}
message_buffer: dict[int, dict] = {}

# Seconds to wait for more messages before processing
BUFFER_TIMEOUT = 2.0

# Max images per OpenAI request
MAX_IMAGES = 10

# Ukraine timezone offset (UTC+2 standard, UTC+3 summer — use fixed +2 for simplicity,
# or switch to zoneinfo if pytz/tzdata is available)
KYIV_TZ = timezone(timedelta(hours=2))

# Ignore all messages sent before this moment (bot was offline)
BOT_START_TIME = datetime.now(timezone.utc)

UKRAINIAN_MONTHS = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
    5: "травня", 6: "червня", 7: "липня", 8: "серпня",
    9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}

# ─────────────────────────── PROMPTS ───────────────────────────

SYSTEM_PROMPT = """Ти — асистент бухгалтера будівельної компанії. Твоя задача — розпізнати дані з фото чека або іншого документа та повернути ТІЛЬКИ валідний JSON без будь-якого додаткового тексту.

ВАЖЛИВО: на фото може бути ОДИН або ДЕКІЛЬКА окремих чеків. Визнач скільки чеків на фото і поверни масив об'єктів — по одному об'єкту на кожен чек.

Формат відповіді — завжди масив, навіть якщо чек один:
{
  "receipts": [
    {
      "doc_type": "к/чек" або "т/чек" або "Рахунок-фактура" або "Замовлення" або логічна назва з документа,
      "doc_number": "номер документу або б/н якщо відсутній",
      "date": "YYYY-MM-DD",
      "vendor": "назва магазину або постачальника",
      "items": ["Товар 1 (кількість x ціна, штрих-код: ...)", ...],
      "total": число (тільки цифри, без валюти),
      "object_name": "офіційна назва об'єкту або null",
      "basis": "Касовий чек" або "Рахунок-фактура" або "Акт виконаних робіт" або null,
      "description": "Формальне речення що пояснює на що витрачені кошти або null",
      "section": "Розділ будівельних робіт або null",
      "foreman": "Прізвище прораба або null"
    }
  ]
}

═══ ПРАВИЛА ═══
Перевіряй свою відповідь, будь максимально точним. Не вигадуй нічого — усю інформацію бери лише з того, що бачиш на фото або в підписі.
doc_type: касовий чек = "к/чек", товарний чек = "т/чек", рахунок-фактура = "Рахунок-фактура", інтернет-замовлення = "Замовлення".
doc_number: номер чека/рахунку. Якщо немає — "б/н".
date: дата з документа у форматі YYYY-MM-DD.
vendor: назва магазину або постачальника (саме постачальник, а не покупець).
object_name: офіційна назва об'єкту з підпису, така як вона зазначена у відомості. Може бути вказана як "Назва об'єкту: ...", або адресою, назвою ЖК тощо. Якщо не вказана — null.

Якщо на фото кілька чеків — кожен чек це окремий об'єкт у масиві receipts. Не змішуй дані різних чеків.

═══ ПІДПИС — ГОЛОВНЕ ДЖЕРЕЛО МЕТАДАНИХ ═══

Підпис від прораба містить структуровані поля. Завжди читай його ПЕРШИМ. Формат підпису:
  Сума: ...
  Назва об'єкту: ...
  Підстава: ...
  Пояснення: ...
  Розділ: ...
  Прораб: ...

Правила читання підпису:
- Сума (total): фактична сума закупки з урахуванням знижок чи додаткових витрат. Якщо вказана — використовуй замість суми з чека. Якщо кілька чеків — сума в підписі зазвичай загальна; для кожного чека бери суму з фото, перевір що вони в сумі збігаються.
- Назва об'єкту (object_name): офіційна назва як у відомості — бери точно як написано.
- Підстава (basis): ЗАВЖДИ вибери одне з трьох значень на основі типу документа: "Касовий чек" (касовий або товарний чек, фіскальний чек), "Рахунок-фактура" (рахунок, накладна, інвойс), "Акт виконаних робіт" (акт, послуги). Якщо в підписі вказано — використовуй підказку, але все одно обери одне з трьох. Ніколи не повертай null.
- Пояснення (description): загальне пояснення на що витрачені кошти. ЗАВЖДИ заповнюй. Якщо є підпис — бери звідти. Якщо немає — склади сам на основі товарів і контексту. ОБОВ'ЯЗКОВО відформатуй: з великої літери, повні слова без скорочень, формальний діловий стиль, БЕЗ крапки на кінці. Наприклад: "аванс за м-ли" → "Аванс за матеріали", "розх. мат. для ел-ки" → "Розхідні матеріали для електромонтажних робіт".
- Розділ (section): розділ будівельних робіт. Відформатуй: з великої літери, повна назва без скорочень. Наприклад: "сантех." → "Сантехнічні роботи", "ел-ка" → "Електромонтажні роботи".
- Прораб (foreman): прізвище прораба, з великої літери.

═══ ITEMS ═══
- Бери назви товарів з чека (конкретні назви, кількість, ціна).
- Якщо є штрих-код — додай.
- Якщо підпис уточнює що куплено — використовуй.
- Назви товарів пиши з великої літери, повністю без скорочень.
- ВАЖЛИВО: якщо в чеку бренд/модель/маркування надруковано ВЕЛИКИМИ ЛІТЕРАМИ — ЗБЕРІГАЙ його великими літерами. Наприклад: "MIDEA", "RAFTEC", "PEX-B", "ELCOR", "PLANK". Не переводь такі слова в звичайний регістр.
- Якщо нічого не розібрати — порожній масив [].

═══ TOTAL ═══
Пріоритет суми:
1. Якщо в підписі явно вказана сума — використовуй її.
2. Якщо підпис пояснює формулу ("1250 + 375") — порахуй і використовуй результат.
3. Якщо в підписі немає суми — бери з чека.
4. Якщо не видно ніде — null.

═══ DOC_NUMBER ═══
Шукай номер документа за ключовими словами: "Чек №", "Номер чеку", "Товарний чек №", "Накладна №" тощо.

═══ ФОРМАТУВАННЯ ТЕКСТУ ═══
Усі текстові поля (vendor, items, description, section, object_name, foreman) повинні бути:
- З великої літери
- Без скорочень (розкривай усі абревіатури та скорочення)
- Формальним діловим стилем
- Граматично правильно

ПЕРЕВІРЯЙ СЕБЕ УВАЖНО ПЕРЕД ВІДПОВІДДЮ.
Якщо поле неможливо визначити — використовуй null.
Відповідай ТІЛЬКИ JSON, без markdown, без пояснень."""

RETRY_SYSTEM_PROMPT = """Ти — асистент бухгалтера будівельної компанії. Попередня спроба розпізнавання дала неточний результат.
Уважно проаналізуй ще раз:
- Скільки окремих чеків на фото? Кожен чек — окремий об'єкт у масиві receipts
- Підпис прораба — чи є там Сума, Назва об'єкту, Підстава, Пояснення, Розділ, Прораб?
- Номер чека/рахунку
- Дату (може бути написана від руки або надрукована)
- Назву магазину або постачальника (не покупця)
- Перелік товарів (перевір назву та штрих-код)
- Підсумкову суму
- Чи правильно відформатовані текстові поля (з великої літери, без скорочень, діловий стиль)?
ПЕРЕВІРЯЙ СЕБЕ УВАЖНО ПЕРЕД ВІДПОВІДДЮ
""" + SYSTEM_PROMPT

# ─────────────────────────── HELPERS ───────────────────────────

def _delete_files(paths: list[str]):
    """Silently delete a list of temp files."""
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _to_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware regardless of source tzinfo state."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _receipt_count_word(n: int) -> str:
    if n == 1:
        return "чек"
    if 2 <= n <= 4:
        return "чеки"
    return "чеків"


# ─────────────────────────── OCR ───────────────────────────

def encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def pdf_to_images(pdf_path: str) -> list[str]:
    """Convert PDF pages to JPEG temp files. Returns list of file paths."""
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError("Для обробки PDF встановіть pymupdf: pip install pymupdf")

    image_paths = []
    try:
        with fitz.open(pdf_path) as doc:
            for page in doc:
                mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for better OCR quality
                pix = page.get_pixmap(matrix=mat)
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                pix.save(tmp.name)
                tmp.close()
                image_paths.append(tmp.name)
    except Exception:
        _delete_files(image_paths)
        raise
    return image_paths


def extract_receipt_data(image_paths: list[str], caption: str = "", retry: bool = False) -> list:
    """
    image_paths: one or more images (multiple = parts of one receipt or separate receipts).
    caption: user text accompanying the images.
    """
    if not image_paths:
        raise ValueError("Немає зображень для розпізнавання.")

    if len(image_paths) > MAX_IMAGES:
        raise ValueError(
            f"Забагато зображень: {len(image_paths)}. Максимум {MAX_IMAGES} за один раз."
        )

    system = RETRY_SYSTEM_PROMPT if retry else SYSTEM_PROMPT

    user_text = "Розпізнай дані з цього чека. Слідкуй правилам."
    if len(image_paths) > 1:
        user_text = (
            f"Тобі надіслано {len(image_paths)} зображень. "
            "Це може бути один чек розбитий на кілька фото, або кілька різних чеків. "
            "Проаналізуй всі зображення разом і поверни масив receipts.\n\n"
            "Розпізнай дані. Слідкуй правилам."
        )
    if caption:
        user_text += f"\n\nПідпис від користувача:\n{caption}"

    # Build content: text + all images
    content: list = [{"type": "text", "text": user_text}]
    for path in image_paths:
        base64_image = encode_image(path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
        })

    messages: list = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]

    model = "gpt-5.4"
    max_tokens = 3300 if not retry else 4000

    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},  # type: ignore
        messages=messages,
        max_completion_tokens=max_tokens,
    )

    usage = response.usage
    if usage:
        PRICE = {
            "gpt-5.2": {"input": 1.75, "output": 14.00},
            "gpt-5":   {"input": 1.25, "output": 10.00},
            "gpt-5.4": {"input": 2.50, "output": 15.00}
        }
        p = PRICE[model]
        cost_in  = usage.prompt_tokens     / 1_000_000 * p["input"]
        cost_out = usage.completion_tokens / 1_000_000 * p["output"]
        cost     = cost_in + cost_out
        print(
            f"[tokens] model={model} | "
            f"in={usage.prompt_tokens} out={usage.completion_tokens} "
            f"total={usage.total_tokens} | "
            f"cost=${cost:.5f} (in=${cost_in:.5f} out=${cost_out:.5f})"
        )

    content_str = response.choices[0].message.content
    if not content_str:
        raise ValueError("AI повернув порожню відповідь. Спробуй ще раз або зроби чіткіше фото.")
    parsed = json.loads(content_str)
    receipts = parsed.get("receipts", [])
    if not receipts:
        raise ValueError("AI не знайшов чеків на фото. Спробуй ще раз або зроби чіткіше фото.")
    return receipts


def parse_text_receipt(text: str) -> list:
    """Parse a text-only message (foreman signature without a photo) into receipts."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Користувач надіслав текстовий запис витрати без фото документу. "
                "Розпізнай поля з тексту. Для doc_type використай 'витрата', doc_number — 'б/н', "
                "date — сьогоднішня дата якщо не вказано. "
                "Слідкуй всім правилам.\n\n"
                f"Текст:\n{text}"
            ),
        },
    ]
    response = client.chat.completions.create(
        model="gpt-5.4",
        response_format={"type": "json_object"},  # type: ignore
        messages=messages,
        max_completion_tokens=2000,
    )
    content_str = response.choices[0].message.content
    if not content_str:
        raise ValueError("AI повернув порожню відповідь.")
    parsed = json.loads(content_str)
    receipts = parsed.get("receipts", [])
    if not receipts:
        raise ValueError("Не вдалося розпізнати дані з тексту.")
    return receipts


# ─────────────────────────── FORMATTER ───────────────────────────

def format_date_ua(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.day} {UKRAINIAN_MONTHS[d.month]} {d.year} р"
    except Exception:
        return date_str or "—"


def build_excel_string(data: dict) -> str:
    doc_type = data.get("doc_type", "к/чек")
    doc_number = data.get("doc_number", "б/н")
    date_ua = format_date_ua(data.get("date", ""))
    vendor = data.get("vendor", "")
    items = data.get("items", [])

    result = f"{doc_type} № {doc_number} від {date_ua} {vendor}"
    if items:
        result += f" ({'; '.join(items)})"

    return result


def build_preview_message(receipts: list) -> str:
    lines = ["📋 *Результат розпізнавання:*", ""]

    n = len(receipts)
    if n > 1:
        lines.append(f"🧾 Знайдено *{n} {_receipt_count_word(n)}* на фото\n")

    for i, data in enumerate(receipts, 1):
        excel_str = build_excel_string(data)
        total = data.get("total")
        total_str = str(total) if total is not None else "—"

        if n > 1:
            lines.append(f"*Чек {i}:*")

        doc_type = data.get("doc_type", "к/чек")
        doc_number = data.get("doc_number", "б/н")
        date_ua = format_date_ua(data.get("date", ""))
        vendor = data.get("vendor", "") or "—"
        items = data.get("items", [])

        lines.append("*Документ:*")
        lines.append(f"Тип: `{doc_type}`")
        lines.append(f"№ `{doc_number}` від `{date_ua}`")
        lines.append(f"Постачальник: `{vendor}`")
        if items:
            lines.append("\n*Товари:*")
            for j, item in enumerate(items, 1):
                lines.append(f"  {j}. `{item}`")
        lines.append(f"\n*Сума:* {total_str} грн")
        lines.append(f"\n*Об'єкт:* {data.get('object_name') or '—'}")
        lines.append(f"\n*Підстава:* {data.get('basis') or '—'}")
        lines.append(f"\n*Пояснення:* {data.get('description') or '—'}")
        lines.append(f"\n*Розділ:* {data.get('section') or '—'}")
        lines.append(f"\n*Прораб:* {data.get('foreman') or '—'}")
        if i < n:
            lines.append("")

    return "\n".join(lines)


def _actor_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    full_name = " ".join(part for part in parts if part).strip()
    return full_name or user.username or f"user-{user.id}"


def _kyiv_message_date(message_date: datetime) -> datetime:
    return _to_utc(message_date).astimezone(KYIV_TZ)


async def _reply_balance(chat_id: int, context: ContextTypes.DEFAULT_TYPE, as_of: datetime):
    balance = get_balance_kopecks(chat_id)
    await context.bot.send_message(chat_id=chat_id, text=format_balance_message(balance, as_of))


async def bind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    foreman_name = " ".join(context.args).strip()
    try:
        bind_chat_account(update.effective_chat.id, foreman_name, _kyiv_message_date(message.date))
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
        created = create_opening_balance(
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
        foreman_name = unbind_chat_account(update.effective_chat.id)
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
        created = create_adjustment_transaction(
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

    try:
        receipts = parse_text_receipt(caption) if is_text_only else extract_receipt_data(image_paths, caption=caption)

        pending[user_id] = {
            "image_paths": image_paths,
            "caption": caption,
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
            parse_mode="Markdown",
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
        image_paths = pdf_to_images(tmp.name)
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
                created = create_topup_transaction(
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
    await query.answer()
    user_id = update.effective_user.id

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
            sheet_url = None
            saved_lines = []
            balance_note = ""
            for data in receipts:
                excel_str = build_excel_string(data)
                total = data.get("total") or 0
                object_name = data.get("object_name") or "—"
                basis = data.get("basis") or "—"
                description = data.get("description") or "—"
                section = data.get("section") or "—"
                foreman = data.get("foreman") or "—"
                sheet_url = append_receipt(
                    excel_str, total, message_date_kyiv, object_name,
                    basis, description, section, foreman,
                )
                saved_lines.append(
                    f"• {excel_str[:60]}{'...' if len(excel_str) > 60 else ''}"
                    f" — *{total} грн* ({object_name})"
                )

            try:
                inserted = create_receipt_transactions(
                    chat_id=update.effective_chat.id,
                    message_id=query.message.message_id,
                    receipts=receipts,
                    description=state["caption"],
                    created_by_user_id=query.from_user.id,
                    created_by_name=_actor_name(query.from_user),
                    created_at=message_date_kyiv,
                )
                if inserted:
                    balance_note = "\n\n" + format_balance_message(
                        get_balance_kopecks(update.effective_chat.id),
                        message_date_kyiv,
                    )
                else:
                    balance_note = "\n\n⚠️ Баланс не оновлено: ця витрата вже врахована."
            except LedgerError as exc:
                balance_note = f"\n\n⚠️ Баланс не оновлено: {exc}"

            saved_text = "\n".join(saved_lines)
            count = len(receipts)
            text = (
                f"✅ *Збережено {count} {_receipt_count_word(count)}!*\n\n"
                f"📅 Сторінка: `{tab_name}`\n\n"
                f"{saved_text}\n\n"
                f"[Відкрити таблицю]({sheet_url})"
                f"{balance_note}"
            )
            await query.edit_message_text(text, parse_mode="Markdown", disable_web_page_preview=True)
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
            new_data = extract_receipt_data(
                state["image_paths"],
                caption=state["caption"],
                retry=True,
            )
            state["receipts"] = new_data
            state["retry_count"] += 1

            preview = build_preview_message(new_data)
            await query.edit_message_text(preview, parse_mode="Markdown", reply_markup=_confirm_keyboard())

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
