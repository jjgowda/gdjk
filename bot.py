# bot.py  ── Google-Drive Uploader Telegram Bot
#
# Build / run locally
#   pip install -r requirements.txt
#   python bot.py
#
# Required packages (already listed in requirements.txt):
#   pyrogram tgcrypto google-api-python-client google-auth
#   google-auth-httplib2 google-auth-oauthlib pymongo (optional)
#   python-dotenv            # only for local .env loading
#
# Environment variables (Railway / Render dashboard or .env):
#   APP_ID                12345678
#   API_HASH              0123456789abcdef0123456789abcdef
#   BOT_TOKEN             123456:ABCdefGhIJK-LMNOPqrsTuVwxyZ
#   SA_JSON               { "type":"service_account", ... }   # paste entire JSON (Railway)
#   SERVICE_ACCOUNT_FILE  sa.json                             # path to write/read key
#   DRIVE_FOLDER_ID       1AbCdEfGhIJkLmNoPQ                 # optional target folder
# ------------------------------------------------------------

import os, io, asyncio, tempfile, mimetypes, json, textwrap, pathlib
from pathlib import Path
from datetime import datetime

from pyrogram import Client, filters, errors
from google.oauth2.service_account import Credentials
from googleapiclient.discovery     import build
from googleapiclient.http          import MediaIoBaseUpload

# ────────────────────────── 1. CONFIG / ENV ──────────────────────────
# Autoload .env during local development
if Path(".env").exists():
    from dotenv import load_dotenv
    load_dotenv(override=True)

def env(name: str, default: str | None = None):
    """Tiny helper that strips whitespace/newlines from env-vars."""
    val = os.getenv(name, default)
    return val.strip() if isinstance(val, str) else val

# Core Telegram credentials
APP_ID     = int(env("APP_ID", "0"))
API_HASH   = env("API_HASH", "")
BOT_TOKEN  = env("BOT_TOKEN", "")

# Google service account key
SA_JSON_ENV           = env("SA_JSON")                    # entire JSON pasted in env-var
SERVICE_ACCOUNT_FILE  = env("SERVICE_ACCOUNT_FILE", "sa.json")

# Optional: Drive destination folder
DRIVE_FOLDER_ID = env("DRIVE_FOLDER_ID") or None

# Basic validation
missing = [k for k, v in {"APP_ID": APP_ID, "API_HASH": API_HASH, "BOT_TOKEN": BOT_TOKEN}.items() if not v]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

# ───── 2. Ensure service-account key exists on disk ─────
key_path = Path(SERVICE_ACCOUNT_FILE)
if not key_path.exists():
    if not SA_JSON_ENV:
        raise RuntimeError(
            "Service-account file not found AND SA_JSON env var is empty.\n"
            "Provide one of them so the bot can authenticate with Google Drive."
        )
    key_path.write_text(textwrap.dedent(SA_JSON_ENV), encoding="utf-8")
    print(f"[init] Wrote service-account key to {key_path.resolve()}")

# ───── 3. Google Drive client ─────
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
creds  = Credentials.from_service_account_file(key_path, scopes=SCOPES)
drive  = build("drive", "v3", credentials=creds, cache_discovery=False)
print("[init] Google Drive service created")

# ───── 4. Telegram client ─────
bot = Client(
    "gdrive-uploader-session",
    api_id=APP_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ───── 5. Utility: upload file to Drive ─────
def upload_to_drive(local_path: str, remote_name: str) -> str:
    mime_type = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
    media     = MediaIoBaseUpload(open(local_path, "rb"), mimetype=mime_type, resumable=True)
    body      = {"name": remote_name}
    if DRIVE_FOLDER_ID:
        body["parents"] = [DRIVE_FOLDER_ID]

    file = drive.files().create(body=body, media_body=media, fields="id, webViewLink").execute()
    return file["webViewLink"]

# ────────────────────────── 6. Handlers ──────────────────────────
@bot.on_message(filters.command("start"))
async def cmd_start(_, m):
    await m.reply_text(
        "👋 Hi!\n"
        "Send me any document / photo / audio / video and "
        "I’ll upload it to Google Drive and send you a link back."
    )

@bot.on_message(filters.document | filters.audio | filters.video | filters.photo)
async def handle_file(client, msg):
    media = msg.document or msg.video or msg.audio or msg.photo
    file_name = (
        (media.file_name or f"tg_file_{media.file_id}")
        if not isinstance(media, list)
        else f"photo_{media[-1].file_unique_id}.jpg"
    )

    progress = await msg.reply_text("⬇️ Downloading…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = await client.download_media(media, tmpdir)
            await progress.edit_text("⬆️ Uploading to Google Drive…")
            link = await asyncio.to_thread(upload_to_drive, local_path, file_name)

        await progress.edit_text(f"✅ Uploaded!\n{link}")
    except Exception as e:
        await progress.edit_text(f"❌ Error: {e}")
        raise                                 # so the log still shows the traceback

# ────────────────────────── 7. Run ──────────────────────────
if __name__ == "__main__":
    print("[init] Bot starting …")
    bot.run()
