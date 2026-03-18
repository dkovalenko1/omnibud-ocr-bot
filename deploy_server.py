"""
Minimal webhook deploy server.
Listens on localhost:9000 for GitHub push webhooks,
verifies HMAC-SHA256 signature, runs git pull, restarts NSSM service.
"""

import hashlib
import hmac
import http.server
import logging
import os
import subprocess
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ---------------------------------------------------------------------------
# Configuration — set WEBHOOK_SECRET in environment or .env before running
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = 9000
SERVICE_NAME = "OmnibudOCR"
LOG_FILE = os.path.join(os.path.dirname(__file__), "deploy.log")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
from notify import send as notify


def _verify_signature(body: bytes, sig_header: str) -> bool:
    """Return True if the request signature matches the secret."""
    if not WEBHOOK_SECRET:
        log.warning("WEBHOOK_SECRET is not set — skipping signature check")
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def _run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=os.path.dirname(__file__)
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


class DeployHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence default access log
        pass

    def do_POST(self):
        if self.path != "/deploy":
            self._respond(404, "Not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        sig = self.headers.get("X-Hub-Signature-256", "")

        if not _verify_signature(body, sig):
            log.warning("Invalid webhook signature")
            self._respond(403, "Forbidden")
            return

        log.info("Webhook received — running git pull")
        code, out = _run(["git", "pull", "origin", "main"])
        log.info("git pull (exit %d): %s", code, out)

        if code != 0:
            log.error("git pull failed")
            notify(f"Deploy FAILED — git pull error:\n{out}")
            self._respond(500, "git pull failed")
            return

        if "Already up to date" in out:
            log.info("No changes — skipping service restart")
            self._respond(200, "No changes")
            return

        # Extract only changed file names from git output
        changed_files = "\n".join(
            line.strip().split("|")[0].strip()
            for line in out.splitlines()
            if "|" in line
        )

        log.info("Changes detected — restarting service %s", SERVICE_NAME)

        # Write deploy timestamp so watchdog ignores imminent restarts
        with open(os.path.join(os.path.dirname(__file__), ".last_deploy"), "w") as f:
            f.write(str(datetime.now().timestamp()))

        code, out = _run(["nssm", "restart", SERVICE_NAME])
        log.info("nssm restart (exit %d): %s", code, out)

        if code == 0:
            log.info("Deploy successful")
            notify(f"Deploy successful\n{changed_files}")
            self._respond(200, "Deployed")
            # Restart watchdog and self after response is sent
            subprocess.Popen(
                ["powershell", "-Command", "Start-Sleep 3; nssm restart OmibudWatchdog; nssm restart OmibudDeploy"],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            log.error("Service restart failed")
            notify(f"Deploy FAILED — could not restart {SERVICE_NAME}:\n{out}")
            self._respond(500, "Service restart failed")

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, "OK")
        else:
            self._respond(404, "Not found")

    def _respond(self, status: int, message: str):
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = http.server.HTTPServer(("localhost", PORT), DeployHandler)
    log.info("Deploy server started on localhost:%d", PORT)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Deploy server listening on localhost:{PORT}")
    server.serve_forever()
