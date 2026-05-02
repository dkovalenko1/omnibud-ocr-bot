import base64
import json
import os
import tempfile

from openai import OpenAI

from config import OPENAI_API_KEY


MAX_IMAGES = 10

client = OpenAI(api_key=OPENAI_API_KEY)

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


def _delete_files(paths: list[str]):
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


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
                mat = fitz.Matrix(2.0, 2.0)
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
        price = {
            "gpt-5.2": {"input": 1.75, "output": 14.00},
            "gpt-5": {"input": 1.25, "output": 10.00},
            "gpt-5.4": {"input": 2.50, "output": 15.00},
        }
        p = price[model]
        cost_in = usage.prompt_tokens / 1_000_000 * p["input"]
        cost_out = usage.completion_tokens / 1_000_000 * p["output"]
        cost = cost_in + cost_out
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
