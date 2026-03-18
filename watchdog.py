"""
Watchdog — checks services every 5 minutes and sends Telegram alert if anything is down.
Run as NSSM service: OmibudWatchdog
"""

import os
import subprocess
import time
import urllib.request
import urllib.parse
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
CHECK_INTERVAL = 300  # seconds

SERVICES = ["OmnibudOCR", "OmibudDeploy", "NgrokTunnel"]


def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not ADMIN_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": ADMIN_CHAT_ID, "text": message}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception:
        pass


def check_service(name: str) -> bool:
    result = subprocess.run(
        ["nssm", "status", name],
        capture_output=True, text=True
    )
    return "SERVICE_RUNNING" in result.stdout


def main():
    send_telegram("Watchdog started — monitoring OmibudOCR services")

    while True:
        for service in SERVICES:
            if not check_service(service):
                send_telegram(f"WARNING: {service} is NOT running on TANYA-PC")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
