"""Shared Telegram notification helper."""

import os
import urllib.request
import urllib.parse
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")


def send(message: str):
    if not TELEGRAM_TOKEN or not ADMIN_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": ADMIN_CHAT_ID, "text": message}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception:
        pass
