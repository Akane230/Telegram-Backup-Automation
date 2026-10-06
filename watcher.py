"""
Telegram Backup Storage Automation
-----------------------------------
Watches one or more folders for newly added files and automatically
uploads each one to its own dedicated Telegram bot/chat.

Config is entirely driven by .env — to add a new bot + folder, add a new
BOT_N_TOKEN / BOT_N_CHAT_ID / BOT_N_FOLDERS group to .env. No code changes
needed; the script discovers routes automatically at startup.

Each route can optionally watch subfolders too, by setting
BOT_N_RECURSIVE=true (defaults to false — top-level only — if omitted).

Files are left in place after upload (not moved or deleted).
Files over 50MB (Telegram Bot API limit) are skipped and logged as a warning.

Run with: pythonw.exe watcher.py   (no console window)
       or: python.exe watcher.py   (for debugging, shows console output)
"""

import os
import re
import sys
import time
import json
import logging
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

load_dotenv()


# ---------------------------------------------------------------------------
# Route discovery
# ---------------------------------------------------------------------------
# A "route" = one bot (token + chat id) paired with the folder(s) it backs up.
# Defined in .env like:
#
#   BOT_1_TOKEN=111:AAA...
#   BOT_1_CHAT_ID=11111111
#   BOT_1_FOLDERS=D:\My Music
#
#   BOT_2_TOKEN=222:BBB...
#   BOT_2_CHAT_ID=22222222
#   BOT_2_FOLDERS=D:\Podcasts,D:\Audiobooks
#
# To add a new bot+folder, just add a new BOT_N_* group — the script scans
# for BOT_1, BOT_2, BOT_3... automatically, in any order, with no gaps required.

@dataclass
class Route:
    name: str          # e.g. "BOT_1" — used only in logs
    bot_token: str
    chat_id: str
    folders: list = field(default_factory=list)
    recursive: bool = False          # whether to include subfolders of this route's folders
    exclude_folders: list = field(default_factory=list)  # list of ("rel", parts_tuple) or ("abs", normalized_path) entries to exclude


def _parse_bool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _is_absolute_windows_path(path):
    # os.path.isabs() on Linux won't recognize "D:\Foo" as absolute, so check
    # for a drive letter explicitly too (relevant since this script targets Windows).
    return os.path.isabs(path) or re.match(r"^[A-Za-z]:[\\/]", path) is not None


def _parse_exclude_list(raw_value):
    """
    Accepts a comma-separated list of either:
      - relative subfolder paths, e.g. 'Temp,Drafts\\WIP'
      - full absolute paths, e.g. 'D:\\Podcasts\\Temp'
    and returns a list of ('rel', parts_tuple) or ('abs', normalized_path) entries.
    Mixing both styles in the same list is fine.
    """
    if not raw_value:
        return []

    excludes = []
    for entry in raw_value.split(","):
        entry = entry.strip().strip('"').strip("'")
        if not entry:
            continue
        entry = entry.replace("/", os.sep).replace("\\", os.sep)

        if _is_absolute_windows_path(entry):
            excludes.append(("abs", os.path.normcase(os.path.normpath(entry))))
        else:
            entry = entry.strip(os.sep)
            parts = tuple(p.lower() for p in entry.split(os.sep) if p)
            if parts:
                excludes.append(("rel", parts))
    return excludes


def discover_routes():
    # Find every distinct "BOT_<id>" prefix that has a _TOKEN set
    pattern = re.compile(r"^BOT_(.+)_TOKEN$")
    route_ids = []
    for key in os.environ:
        m = pattern.match(key)
        if m:
            route_ids.append(m.group(1))

    routes = []
    for route_id in route_ids:
        name = f"BOT_{route_id}"
        token = os.getenv(f"{name}_TOKEN")
        chat_id = os.getenv(f"{name}_CHAT_ID")
        raw_folders = os.getenv(f"{name}_FOLDERS")
        recursive = _parse_bool(os.getenv(f"{name}_RECURSIVE"), default=False)
        exclude_folders = _parse_exclude_list(os.getenv(f"{name}_EXCLUDE"))

        if not token or not chat_id or not raw_folders:
            print(f"ERROR: {name} is missing TOKEN, CHAT_ID, or FOLDERS in .env — skipping this route.")
            continue

        folders = [f.strip() for f in raw_folders.split(",") if f.strip()]
        routes.append(Route(
            name=name, bot_token=token, chat_id=chat_id,
            folders=folders, recursive=recursive, exclude_folders=exclude_folders,
        ))

    return routes


ROUTES = discover_routes()

if not ROUTES:
    print("ERROR: No valid bot routes found. Check your .env file — see .env.example.")
    sys.exit(1)

# List of (normalized root folder, Route) pairs, for dispatching file events
# to the right route. Kept as a list (not a dict) so recursive routes can be
# matched by "is this file inside that root folder", not just exact folder match.
ROUTE_FOLDERS = []
for route in ROUTES:
    for folder in route.folders:
        ROUTE_FOLDERS.append((os.path.normcase(os.path.normpath(folder)), route))


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

def send_to_telegram(filepath, route):
    filename = os.path.basename(filepath)
    url = f"https://api.telegram.org/bot{route.bot_token}/sendDocument"

    try:
        with open(filepath, "rb") as f:
            files = {"document": (filename, f)}
            data = {"chat_id": route.chat_id}
            response = requests.post(url, data=data, files=files, timeout=120)

        if response.status_code == 200 and response.json().get("ok"):
            logger.info(f"[{route.name}] Sent: {filename}")
            return True
        else:
            logger.error(f"[{route.name}] Telegram API error for {filename}: {response.status_code} {response.text}")
            return False

    except requests.RequestException as e:
        logger.error(f"[{route.name}] Network error sending {filename}: {e}")
        return False
    except OSError as e:
        logger.error(f"[{route.name}] File error reading {filename}: {e}")
        return False


def notify_telegram_text(route, message):
    """Optional: send a plain text status message to a route's chat."""
    url = f"https://api.telegram.org/bot{route.bot_token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": route.chat_id, "text": message}, timeout=30)
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
# Route lookup
# ---------------------------------------------------------------------------

def get_route_for_file(filepath):
    """
    Find which route owns this file, and the root folder that matched.
    - Exact match against a route's root folder always counts.
    - A match inside a subfolder only counts if that route has recursive=True.
    - If folders are nested across routes, the most specific (longest) root wins.
    Returns (route, root) or (None, None) if nothing matches.
    """
    file_dir = os.path.normcase(os.path.normpath(os.path.dirname(filepath)))
    best_route = None
    best_root = None
    best_len = -1

    for root, route in ROUTE_FOLDERS:
        if file_dir == root:
            match = True
        elif route.recursive and file_dir.startswith(root + os.sep):
            match = True
        else:
            match = False

        if match and len(root) > best_len:
            best_route = route
            best_root = root
            best_len = len(root)

    return best_route, best_root


def _dir_matches_exclude(route, root, abs_dir_path):
    """
    True if abs_dir_path (a folder) falls inside one of the route's excluded
    entries — whether that entry was given as a relative subfolder path or
    a full absolute path. Matching a folder also matches everything nested
    inside it.
    """
    if not route.exclude_folders:
        return False

    abs_norm = os.path.normcase(os.path.normpath(abs_dir_path))
    rel_dir = os.path.relpath(abs_dir_path, root)
    rel_parts = tuple(p.lower() for p in rel_dir.split(os.sep) if p) if rel_dir != "." else ()

    for kind, value in route.exclude_folders:
        if kind == "abs":
            if abs_norm == value or abs_norm.startswith(value + os.sep):
                return True
        else:  # "rel"
            if rel_parts and rel_parts[:len(value)] == value:
                return True
    return False


def is_excluded(route, root, filepath):
    """True if the file's containing folder is excluded for this route."""
    return _dir_matches_exclude(route, root, os.path.dirname(filepath))


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

    route, root = get_route_for_file(filepath)
    if route is None:
        logger.warning(f"No route found for file, skipping: {filepath}")
        return

    if is_excluded(route, root, filepath):
        return  # silently ignored — this is expected, not a warning-worthy event

    logger.info(f"[{route.name}] New file detected: {filename} — waiting for it to finish copying...")
    if not wait_until_stable(filepath):
        logger.warning(f"[{route.name}] Skipped (never stabilized or was removed): {filename}")
        return

    try:
        size = os.path.getsize(filepath)
    except OSError as e:
        logger.error(f"[{route.name}] Could not stat {filename}: {e}")
        return

    if size > TELEGRAM_MAX_BYTES:
        logger.warning(
            f"[{route.name}] Skipped (too large: {size / (1024*1024):.1f}MB > 50MB limit): {filename}"
        )
        return

    if send_to_telegram(filepath, route):
        sent_files.add(filepath)
        save_sent_files(sent_files)


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------

class BackupFolderHandler(FileSystemEventHandler):
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
    for route in ROUTES:
        for folder in route.folders:
            if not os.path.isdir(folder):
                continue

            if route.recursive:
                walker = []
                for dirpath, dirnames, filenames in os.walk(folder):
                    # Prune excluded subfolders in-place so os.walk doesn't descend into them
                    dirnames[:] = [
                        d for d in dirnames
                        if not _dir_matches_exclude(route, folder, os.path.join(dirpath, d))
                    ]
                    for fname in filenames:
                        walker.append(os.path.join(dirpath, fname))
            else:
                walker = (entry.path for entry in os.scandir(folder) if entry.is_file())

            for filepath in walker:
                if filepath not in sent_files:
                    logger.info(f"Found unsent file from before startup: {os.path.basename(filepath)}")
                    process_file(filepath)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    valid_folders = []  # list of (folder, recursive) tuples
    for route in ROUTES:
        for folder in route.folders:
            if os.path.isdir(folder):
                valid_folders.append((folder, route.recursive))
            else:
                logger.error(f"[{route.name}] Watch folder does not exist, skipping: {folder}")

    if not valid_folders:
        logger.error("No valid watch folders found across any route, exiting.")
        sys.exit(1)

    logger.info(f"Starting Telegram backup watcher — {len(ROUTES)} bot(s), {len(valid_folders)} folder(s):")
    for route in ROUTES:
        for folder in route.folders:
            status = "OK" if os.path.isdir(folder) else "MISSING"
            mode = "recursive" if route.recursive else "top-level only"
            logger.info(f"  [{route.name}] {folder} ({status}, {mode})")
        if route.exclude_folders:
            excluded_display = ", ".join(
                value if kind == "abs" else os.sep.join(value)
                for kind, value in route.exclude_folders
            )
            logger.info(f"  [{route.name}] excluding: {excluded_display}")

    scan_existing_files()

    event_handler = BackupFolderHandler()
    observer = Observer()
    for folder, recursive in valid_folders:
        observer.schedule(event_handler, folder, recursive=recursive)
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