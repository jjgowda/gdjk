# bot.py ── Google-Drive Uploader Telegram Bot
#
# Build locally:
#   pip install -r requirements.txt
#   python bot.py
#
# Requirements (put in requirements.txt):
#   pyrogram>=2.0.106
#   tgcrypto>=1.2.5
#   google-api-python-client>=2.118.0
#   google-auth>=2.29.0
#   google-auth-httplib2>=0.2.0
#   google-auth-oauthlib>=1.2.0
#   python-dotenv>=1.0.1
#
# Environment variables (Railway/Render dashboard or .env file):
#   APP_ID=12345678
#   API_HASH=0123456789abcdef0123456789abcdef
#   BOT_TOKEN=123456:ABCdefGhIJK-LMNOPqrsTuVwxyZ
#   SA_JSON={"type":"service_account",...}
#   SERVICE_ACCOUNT_FILE=sa.json
#   DRIVE_FOLDER_ID=1AbCdEfGhIJkLmNoPQ (optional)

import os
import asyncio
import tempfile
import mimetypes
import textwrap
from pathlib import Path

from pyrogram import Client, filters
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ───────────────────────────── CONFIG / ENV ─────────────────────────────
# Auto-load .env for local development
if Path(".env").exists():
    from dotenv import load_dotenv
    load_dotenv(override=True)

def env(name: str, default: str | None = None):
    """Get environment variable and strip whitespace."""
    val = os.getenv(name, default)
    return val.strip() if isinstance(val, str) else val

# Telegram credentials
APP_ID = int(env("APP_ID", "0"))
API_HASH = env("API_HASH", "")
BOT_TOKEN = env("BOT_TOKEN", "")

# Google Drive service account
SA_JSON_ENV = env("SA_JSON")
SERVICE_ACCOUNT_FILE = env("SERVICE_ACCOUNT_FILE", "sa.json")
DRIVE_FOLDER_ID = env("DRIVE_FOLDER_ID") or None

# Validate required credentials
missing = [k for k, v in {"APP_ID": APP_ID, "API_HASH": API_HASH, "BOT_TOKEN": BOT_TOKEN}.items() if not v]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

# ───────────────── Ensure service-account key exists on disk ────────────
key_path = Path(SERVICE_ACCOUNT_FILE)
if not key_path.exists():
    if not SA_JSON_ENV:
        raise RuntimeError(
            "Service-account file not found and SA_JSON environment variable is empty.\n"
            "Provide one of them so the bot can authenticate with Google Drive."
        )
    # Write the JSON key to disk
    key_path.write_text(textwrap.dedent(SA_JSON_ENV), encoding="utf-8")
    print(f"[init] Wrote service-account key to {key_path.resolve()}")

# ─────────────────────── Google Drive client ────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
try:
    creds = Credentials.from_service_account_file(key_path, scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    print("[init] Google Drive service created")
except Exception as e:
    raise RuntimeError(f"Failed to initialize Google Drive service: {e}")

# ─────────────────────── Telegram client ────────────────────────────────
bot = Client(
    "gdrive-uploader-session",
    api_id=APP_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ───────────────────── Helper function: upload to Drive ──────────────────
def upload_to_drive(local_path: str, remote_name: str) -> str:
    """Upload a local file to Google Drive and return the shareable link."""
    mime_type = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
    
    with open(local_path, "rb") as f:
        media = MediaIoBaseUpload(f, mimetype=mime_type, resumable=True)
        
    body = {"name": remote_name}
    if DRIVE_FOLDER_ID:
        body["parents"] = [DRIVE_FOLDER_ID]

    file = drive.files().create(
        body=body, 
        media_body=media, 
        fields="id, webViewLink"
    ).execute()
    
    return file["webViewLink"]

# ───────────────────── Telegram message handlers ────────────────────────
@bot.on_message(filters.command("start"))
async def cmd_start(_, message):
    await message.reply_text(
        "👋 **Hi there!**\n\n"
        "Send me any **document**, **photo**, **audio**, or **video** file "
        "and I'll upload it to Google Drive and send you a shareable link back.\n\n"
        "Just drop your file and wait for the magic! ✨"
    )

@bot.on_message(filters.document | filters.audio | filters.video | filters.photo)
async def handle_file(client, message):
    """Handle incoming files and upload them to Google Drive."""
    media = message.document or message.video or message.audio or message.photo

    # Generate appropriate filename
    if hasattr(media, "file_name") and media.file_name:
        file_name = media.file_name
    else:
        # Photos don't have file_name attribute
        if isinstance(media, list):  # Photo array
            uid = media[-1].file_unique_id
        else:
            uid = media.file_unique_id
        file_name = f"photo_{uid}.jpg"

    # Show progress to user
    progress_msg = await message.reply_text("⬇️ **Downloading file...**")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Download the media
            local_path = await client.download_media(media, temp_dir)
            
            # Handle photos: Pyrogram returns a directory, we need to pick a file
            if os.path.isdir(local_path):
                files = [os.path.join(local_path, f) for f in os.listdir(local_path)]
                if not files:
                    raise RuntimeError("Downloaded photo directory is empty")
                # Pick the largest file (highest quality)
                local_path = max(files, key=os.path.getsize)

            await progress_msg.edit_text("⬆️ **Uploading to Google Drive...**")
            
            # Upload to Google Drive in a separate thread
            drive_link = await asyncio.to_thread(upload_to_drive, local_path, file_name)

        # Success message
        await progress_msg.edit_text(
            f"✅ **Upload successful!**\n\n"
            f"📁 **File:** `{file_name}`\n"
            f"🔗 **Link:** {drive_link}"
        )

    except Exception as e:
        error_msg = f"❌ **Upload failed:** {str(e)}"
        await progress_msg.edit_text(error_msg)
        print(f"[ERROR] Upload failed for user {message.from_user.id}: {e}")
        raise  # Keep full traceback in logs

# ───────────────────────────── Main execution ──────────────────────────
if __name__ == "__main__":
    print("[init] Starting Google Drive Uploader Bot...")
    print(f"[init] Target folder: {'My Drive (root)' if not DRIVE_FOLDER_ID else DRIVE_FOLDER_ID}")
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("[init] Bot stopped by user")
    except Exception as e:
        print(f"[ERROR] Bot crashed: {e}")
        raise
