"""
Watchdog — monitors services and internet, sends Telegram alerts on events:
- PC started
- PC shutting down
- Internet lost / restored
- Service crashed / recovered
Run as NSSM service: OmibudWatchdog
"""

import signal
import subprocess
import sys
import os
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from notify import send as notify

SERVICES = ["OmnibudOCR", "OmibudDeploy", "NgrokTunnel"]
CHECK_INTERVAL = 30  # seconds
INTERNET_CHECK_URL = "https://www.google.com"
INTERNET_TIMEOUT = 5


def is_internet_up() -> bool:
    try:
        urllib.request.urlopen(INTERNET_CHECK_URL, timeout=INTERNET_TIMEOUT)
        return True
    except Exception:
        return False


def get_service_status(name: str) -> bool:
    result = subprocess.run(
        ["nssm", "status", name],
        capture_output=True, text=True
    )
    return "SERVICE_RUNNING" in result.stdout


def on_shutdown(signum, frame):
    notify("PC is shutting down — OmibudOCR going offline")
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, on_shutdown)
    signal.signal(signal.SIGBREAK, on_shutdown)

    # Wait for internet before sending startup message
    while not is_internet_up():
        time.sleep(5)

    notify("PC started — OmibudOCR system is online")

    internet_was_up = True
    service_states = {s: get_service_status(s) for s in SERVICES}

    while True:
        time.sleep(CHECK_INTERVAL)

        # Check internet
        internet_now = is_internet_up()
        if internet_was_up and not internet_now:
            notify("Internet connection LOST on TANYA-PC")
        elif not internet_was_up and internet_now:
            notify("Internet connection RESTORED on TANYA-PC")
        internet_was_up = internet_now

        # Check services
        for service in SERVICES:
            is_running = get_service_status(service)
            was_running = service_states[service]

            if was_running and not is_running:
                notify(f"SERVICE CRASHED: {service} is down on TANYA-PC")
            elif not was_running and is_running:
                notify(f"Service recovered: {service} is back up")

            service_states[service] = is_running


if __name__ == "__main__":
    main()
