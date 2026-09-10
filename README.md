# Telegram Folder Backup

Watches a local folder on Windows and automatically uploads any new file to a
Telegram chat via a bot, as soon as it's added. Built for backing up a music
folder, but works for any folder of files under Telegram's size limit.

## How it works

- Runs continuously in the background and watches a **single folder**
  (top-level only — subfolders are not monitored).
- When a new file is added, it waits until the file's size stops changing
  before uploading, so partially-copied or partially-downloaded files aren't
  sent prematurely.
- Uploads the file to a Telegram chat using the Bot API's `sendDocument`.
- Keeps a record of already-sent files (`sent_files.json`) so restarting the
  script won't re-send anything.
- Files over Telegram's 50MB Bot API limit are skipped and logged as a
  warning — nothing crashes.
- Files are left in place after upload; nothing is moved or deleted.

## Prerequisites

- Windows 10/11
- Python 3.9+
- A Telegram bot token (create one via [@BotFather](https://t.me/BotFather))
- A Telegram chat ID (the destination for uploads — can be your own DM with
  the bot, or a group/channel)

## Setup

**1. Clone the repo and enter the folder**

```powershell
git clone <your-repo-url>
cd telegram-folder-backup
```

**2. Create a virtual environment and install dependencies**

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**3. Configure your environment variables**

Copy `.env.example` to `.env` and fill in your real values:

```powershell
copy .env.example .env
```

```env
BOT_TOKEN=your_bot_token_here
CHAT_ID=your_chat_id_here
WATCH_FOLDER=D:\My Music
```

`.env` is git-ignored and never committed — only `.env.example` (with
placeholder values) is tracked in the repo.

**4. Test it**

```powershell
python watcher.py
```

Drop a file into your watched folder and confirm it arrives in the Telegram
chat. Check `watcher.log` if something doesn't work. Press `Ctrl+C` to stop.

## Running it automatically in the background

Use Windows Task Scheduler so it starts silently at login, with no console
window:

1. Open **Task Scheduler** → **Create Task** (not "Basic Task")
2. **General** tab: give it a name; check "Run whether user is logged on or
   not" if you want it active even while locked
3. **Triggers** tab: New → **At log on**
4. **Actions** tab: New →
   - Program/script: `C:\path\to\telegram-folder-backup\venv\Scripts\pythonw.exe`
   - Add arguments: `watcher.py`
   - Start in: `C:\path\to\telegram-folder-backup`
5. **Conditions** tab: uncheck "Start only if on AC power" if this runs on a
   laptop that should back up on battery too

Save it — it'll now run quietly on every login.

## Files

| File | Purpose |
|---|---|
| `watcher.py` | Main script — watches the folder and uploads new files |
| `.env` | Your real secrets (bot token, chat ID, folder path) — not committed |
| `.env.example` | Template showing required env vars — safe to commit |
| `requirements.txt` | Python dependencies |
| `watcher.log` | Runtime log (created automatically, git-ignored) |
| `sent_files.json` | Tracks already-uploaded files (created automatically, git-ignored) |

## Notes / limitations

- Telegram's standard Bot API caps file uploads at **50MB**. Larger files
  require running a local Bot API server, which this project doesn't set up.
- Only the top-level folder is watched — files added inside subfolders won't
  trigger an upload.
- Keep your `BOT_TOKEN` private. Anyone with it can control your bot.