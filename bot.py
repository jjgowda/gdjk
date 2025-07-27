# gdrive_uploader_bot.py
# pip install pyrogram tgcrypto google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib python-dotenv

import os, io, asyncio, tempfile, mimetypes
from pathlib import Path
from datetime import datetime

from pyrogram import Client, filters
from google.oauth2.service_account import Credentials
from googleapiclient.discovery    import build
from googleapiclient.http         import MediaIoBaseUpload

# ───────────────────── CONFIG SECTION ─────────────────────
APP_ID       = int(os.getenv("APP_ID", 0))                      # my.telegram.org
API_HASH     = os.getenv("API_HASH", "YOUR_API_HASH")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
SA_FILE      = os.getenv("SERVICE_ACCOUNT_FILE", "sa.json")    # uploaded to Render “Secret Files” or kept locally
DRIVE_FOLDER = os.getenv("DRIVE_FOLDER_ID", None)              # leave blank to use root (“My Drive”)
# ───────────────────────────────────────────────────────────

# Auto-load .env if you’re running on your laptop
if Path(".env").exists():
    from dotenv import load_dotenv; load_dotenv(override=True)

# Google Drive service
creds    = Credentials.from_service_account_file(SA_FILE, scopes=["https://www.googleapis.com/auth/drive.file"])
drive    = build("drive", "v3", credentials=creds, cache_discovery=False)

# Telegram client
bot = Client("gdrive-uploader", api_id=APP_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def upload_to_drive(local_path: str, file_name: str) -> str:
    mime   = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    media  = MediaIoBaseUpload(open(local_path, "rb"), mimetype=mime, resumable=True)
    body   = {"name": file_name}
    if DRIVE_FOLDER:
        body["parents"] = [DRIVE_FOLDER]
    file   = drive.files().create(body=body, media_body=media, fields="id, webViewLink").execute()
    return file["webViewLink"]

@bot.on_message(filters.command("start"))
async def on_start(_, m):
    await m.reply_text("Send me any file and I‘ll put it straight into Google Drive!")

@bot.on_message(filters.document | filters.video | filters.audio | filters.photo)
async def on_file(_, m):
    media = m.document or m.video or m.audio or m.photo
    file_name = (media.file_name or f"tg_file_{media.file_id}") if not isinstance(media, list) else f"photo_{media[-1].file_unique_id}.jpg"

    msg = await m.reply_text("Downloading…")
    with tempfile.TemporaryDirectory() as tmp:
        local_path = await bot.download_media(media, tmp)
        await msg.edit_text("Uploading to Drive…")
        link = await asyncio.to_thread(upload_to_drive, local_path, file_name)

    await msg.edit_text(f"✅ Uploaded!\n{link}")

if __name__ == "__main__":
    print("Bot starting…"); bot.run()
