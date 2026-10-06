# Telegram Folder Backup

Watches local folders on Windows and automatically uploads any new file to
Telegram via a bot, as soon as it's added. Supports **multiple bots**, each
with its own dedicated folder(s) — e.g. a "Music" bot backing up your music
folder and a separate "Documents" bot backing up another folder, running
from the same script.

## How it works

- Runs continuously in the background and watches **one or more folders**,
  each mapped to its own bot + chat. By default only the top level of each
  folder is watched; any route can opt in to also watching its subfolders
  (see `BOT_N_RECURSIVE` below).
- Config is entirely driven by `.env`. Each bot+folder pairing is called a
  **route**. Adding a new bot and folder is just adding a new route to
  `.env` — no code changes required, ever.
- When a new file is added, it waits until the file's size stops changing
  before uploading, so partially-copied or partially-downloaded files aren't
  sent prematurely.
- Uploads the file to that folder's bot/chat using the Bot API's
  `sendDocument`.
- Keeps a record of already-sent files (`sent_files.json`) so restarting the
  script won't re-send anything.
- Files over Telegram's 50MB Bot API limit are skipped and logged as a
  warning — nothing crashes.
- Files are left in place after upload; nothing is moved or deleted.

## Prerequisites

- Windows 10/11
- Python 3.9+
- One Telegram bot token per route (create via [@BotFather](https://t.me/BotFather))
  — you can reuse the same bot across multiple folders, or use a separate
  bot per folder, your choice
- One Telegram chat ID per route (the destination for that folder's
  uploads — can be your own DM with the bot, or a group/channel)

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
BOT_1_TOKEN=your_first_bot_token_here
BOT_1_CHAT_ID=your_first_chat_id_here
BOT_1_FOLDERS=D:\My Music
BOT_1_RECURSIVE=false
BOT_1_EXCLUDE=

BOT_2_TOKEN=your_second_bot_token_here
BOT_2_CHAT_ID=your_second_chat_id_here
BOT_2_FOLDERS=D:\Podcasts,D:\Audiobooks
BOT_2_RECURSIVE=true
BOT_2_EXCLUDE=Temp,Drafts\WIP
```

Each `BOT_N_*` group is a **route**: a bot token, its destination chat ID,
a comma-separated list of folder(s) it watches, whether to include
subfolders, and any subfolders to skip:

- `BOT_N_FOLDERS` — one bot can watch multiple folders; you decide whether
  different folders share a bot or get their own.
- `BOT_N_RECURSIVE` — optional, defaults to `false`. Set to `true` to also
  pick up files added inside any subfolder of that route's folders, not
  just the top level. Each route can set this independently — e.g. one
  bot watching flat and another watching recursively.
- `BOT_N_EXCLUDE` — optional, comma-separated list of subfolders to skip,
  even with `RECURSIVE=true`. Excluding a folder excludes everything
  inside it too. Only applies when `RECURSIVE=true` — irrelevant otherwise,
  since subfolders aren't watched at all in that case. Each entry can be
  either style, and you can mix both in the same list:
  - **Relative**, from that route's folder: `Temp` or `Drafts\WIP` (the
    nested form excludes only that specific subfolder, not every `WIP`
    folder anywhere)
  - **Full absolute path**: `D:\Podcasts\Temp`

`.env` is git-ignored and never committed — only `.env.example` (with
placeholder values) is tracked in the repo.

### Adding another bot + folder later

No code changes needed. Just add a new block to `.env`:

```env
BOT_3_TOKEN=your_third_bot_token_here
BOT_3_CHAT_ID=your_third_chat_id_here
BOT_3_FOLDERS=D:\Path\To\New\Folder
```

Restart the watcher and it's picked up automatically — the script scans
`.env` at startup for every `BOT_N_TOKEN` it can find, in any order, with no
gaps required (you could even skip from `BOT_2` to `BOT_5`).

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
| `watcher.py` | Main script — watches folders and uploads new files via the right bot |
| `.env` | Your real secrets (bot tokens, chat IDs, folder paths per route) — not committed |
| `.env.example` | Template showing the route format — safe to commit |
| `requirements.txt` | Python dependencies |
| `watcher.log` | Runtime log (created automatically, git-ignored) |
| `sent_files.json` | Tracks already-uploaded files (created automatically, git-ignored) |

## Notes / limitations

- Telegram's standard Bot API caps file uploads at **50MB**. Larger files
  require running a local Bot API server, which this project doesn't set up.
- By default only the top level of each folder is watched. Set
  `BOT_N_RECURSIVE=true` for a route to also include its subfolders.
- Use `BOT_N_EXCLUDE` to skip specific subfolders when running recursively
  (e.g. a `Temp` or `WIP` folder you don't want backed up).
- Keep your bot tokens private. Anyone with one can control that bot.
- If two routes accidentally point at the same folder, only one route will
  claim it (whichever is read last during startup) — keep each folder
  assigned to exactly one route.