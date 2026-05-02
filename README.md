# OmnibudOCR

Telegram bot for OCR receipt capture, Google Sheets export, and per-chat ledger balances.

## Runtime Layout

- `bot.py` wires Telegram handlers and async workflow orchestration.
- `receipt_recognition.py` owns OpenAI prompts, image/PDF preparation, and OCR parsing.
- `receipt_formatting.py` owns receipt preview and sheet-row text formatting.
- `receipt_persistence.py` coordinates Google Sheets writes with ledger validation and rollback marking.
- `ledger_service.py` and `ledger_db.py` own SQLite accounting state.
- `sheets_service.py` owns Google Sheets API calls.
- `deploy_server.py`, `watchdog.py`, and `notify.py` support the Windows/NSSM deployment.

## Configuration

Copy `.env.example` to `.env` and fill in the values. Keep the Google service-account JSON outside the repository directory, then point `GOOGLE_CREDENTIALS_JSON` at that absolute path. The repo ignores `*.json`, but storing live credentials outside the project root reduces accidental sharing and backup exposure.

`WEBHOOK_SECRET` is required for `deploy_server.py`; unsigned deploy webhooks are rejected.

## Tests

Run the suite with:

```bash
.venv/bin/python -m unittest discover -v
```
