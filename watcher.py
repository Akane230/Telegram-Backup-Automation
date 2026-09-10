"""
Telegram Backup Storage Automation
-----------------------------------
Watches a folder (top-level only, no subfolders) for newly added files
and automatically uploads them to a Telegram chat via the Bot API.

Files are left in place after upload (not moved or deleted).
Files over 50MB (Telegram Bot API limit) are skipped and logged as a warning.

Run with: pythonw.exe watcher.py   (no console window)
       or: python.exe watcher.py   (for debugging, shows console output)
"""

import os
import sys
import time
import json
import logging
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
WATCH_FOLDER = os.getenv("WATCH_FOLDER")

if not BOT_TOKEN or not CHAT_ID or not WATCH_FOLDER:
    print("ERROR: Missing BOT_TOKEN, CHAT_ID, or WATCH_FOLDER. Check your .env file.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "watcher.log")
SENT_LOG_FILE = os.path.join(SCRIPT_DIR, "sent_files.json")

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB Bot API limit
STABLE_CHECK_INTERVAL = 2   # seconds between size checks
STABLE_CHECK_REQUIRED = 3   # consecutive matching checks before considering file "done"
STABLE_CHECK_TIMEOUT = 300  # give up waiting for a file to finish copying after this long

logger = logging.getLogger("telegram_backup")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)
# Also print to console when run directly (helpful for testing with python.exe)
console = logging.StreamHandler(sys.stdout)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(console)


# ---------------------------------------------------------------------------
# Sent-files record (prevents re-sending on restart)
# ---------------------------------------------------------------------------

def load_sent_files():
    if os.path.exists(SENT_LOG_FILE):
        try:
            with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read sent_files.json, starting fresh: {e}")
    return set()


def save_sent_files(sent_set):
    try:
        with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_set), f, indent=2)
    except OSError as e:
        logger.error(f"Could not write sent_files.json: {e}")


sent_files = load_sent_files()


# ---------------------------------------------------------------------------
# Telegram upload
# ---------------------------------------------------------------------------

def send_to_telegram(filepath):
    filename = os.path.basename(filepath)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    try:
        with open(filepath, "rb") as f:
            files = {"document": (filename, f)}
            data = {"chat_id": CHAT_ID}
            response = requests.post(url, data=data, files=files, timeout=120)

        if response.status_code == 200 and response.json().get("ok"):
            logger.info(f"Sent: {filename}")
            return True
        else:
            logger.error(f"Telegram API error for {filename}: {response.status_code} {response.text}")
            return False

    except requests.RequestException as e:
        logger.error(f"Network error sending {filename}: {e}")
        return False
    except OSError as e:
        logger.error(f"File error reading {filename}: {e}")
        return False


def notify_telegram_text(message):
    """Optional: send a plain text status message to the same chat."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=30)
    except requests.RequestException:
        pass  # non-critical, don't crash the watcher over a notification failing


# ---------------------------------------------------------------------------
# Wait for a file to finish being written (important for copies/downloads)
# ---------------------------------------------------------------------------

def wait_until_stable(filepath):
    """
    Polls the file size until it stops changing for STABLE_CHECK_REQUIRED
    consecutive checks, or until STABLE_CHECK_TIMEOUT is hit.
    Returns True if the file appears stable, False if it timed out or vanished.
    """
    start_time = time.time()
    last_size = -1
    stable_count = 0

    while time.time() - start_time < STABLE_CHECK_TIMEOUT:
        if not os.path.exists(filepath):
            return False  # file was removed/renamed before it stabilized

        try:
            current_size = os.path.getsize(filepath)
        except OSError:
            return False

        if current_size == last_size:
            stable_count += 1
            if stable_count >= STABLE_CHECK_REQUIRED:
                return True
        else:
            stable_count = 0
            last_size = current_size

        time.sleep(STABLE_CHECK_INTERVAL)

    logger.warning(f"Timed out waiting for file to stabilize: {os.path.basename(filepath)}")
    return False


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def process_file(filepath):
    filename = os.path.basename(filepath)

    # Skip if we've already sent this exact path before
    if filepath in sent_files:
        return

    # Ignore temp/partial files some apps create while downloading/copying
    if filename.startswith("~") or filename.endswith((".tmp", ".crdownload", ".part")):
        return

    if not os.path.isfile(filepath):
        return

    logger.info(f"New file detected: {filename} — waiting for it to finish copying...")
    if not wait_until_stable(filepath):
        logger.warning(f"Skipped (never stabilized or was removed): {filename}")
        return

    try:
        size = os.path.getsize(filepath)
    except OSError as e:
        logger.error(f"Could not stat {filename}: {e}")
        return

    if size > TELEGRAM_MAX_BYTES:
        logger.warning(
            f"Skipped (too large: {size / (1024*1024):.1f}MB > 50MB limit): {filename}"
        )
        return

    if send_to_telegram(filepath):
        sent_files.add(filepath)
        save_sent_files(sent_files)


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------

class MusicFolderHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        process_file(event.src_path)

    def on_moved(self, event):
        # Handles the common case where an app writes a temp file then renames it
        if event.is_directory:
            return
        process_file(event.dest_path)


# ---------------------------------------------------------------------------
# Startup: catch up on any files already present but never sent
# (only runs once at launch, useful after the watcher was offline)
# ---------------------------------------------------------------------------

def scan_existing_files():
    if not os.path.isdir(WATCH_FOLDER):
        logger.error(f"Watch folder does not exist: {WATCH_FOLDER}")
        return

    for entry in os.scandir(WATCH_FOLDER):
        if entry.is_file():
            if entry.path not in sent_files:
                logger.info(f"Found unsent file from before startup: {entry.name}")
                process_file(entry.path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.isdir(WATCH_FOLDER):
        logger.error(f"Watch folder does not exist, exiting: {WATCH_FOLDER}")
        sys.exit(1)

    logger.info(f"Starting Telegram backup watcher on: {WATCH_FOLDER}")
    scan_existing_files()

    event_handler = MusicFolderHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_FOLDER, recursive=False)  # top-level only
    observer.start()
    logger.info("Watcher is running.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        logger.info("Watcher stopped by user.")
    observer.join()


if __name__ == "__main__":
    main()