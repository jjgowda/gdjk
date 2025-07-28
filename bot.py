# bot.py ── Google-Drive Uploader Telegram Bot (OAuth with Environment Variables)
#
# Setup Instructions:
# 1. Google Cloud Console → APIs & Services → Credentials → Create OAuth 2.0 Client ID (Web Application)
# 2. Set authorized redirect URI: http://localhost:8080/callback
# 3. Copy Client ID and Client Secret to Railway environment variables
# 4. Deploy to Railway - authentication happens automatically
#
# Requirements (put in requirements.txt):
#   pyrogram>=2.0.106
#   tgcrypto>=1.2.5
#   google-api-python-client>=2.118.0
#   google-auth>=2.29.0
#   google-auth-httplib2>=0.2.0
#   google-auth-oauthlib>=1.2.0
#   python-dotenv>=1.0.1

import os
import asyncio
import tempfile
import mimetypes
from pathlib import Path

from pyrogram import Client, filters
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
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

# Google OAuth credentials (from Google Cloud Console)
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", "")

# Optional: target folder
DRIVE_FOLDER_ID = env("DRIVE_FOLDER_ID") or None

# OAuth settings
SCOPES = ['https://www.googleapis.com/auth/drive.file']
REDIRECT_URI = 'urn:ietf:wg:oauth:2.0:oob'  # For installed/server apps

# Pre-generated OAuth token (you'll set this after first run)
OAUTH_TOKEN = env("OAUTH_TOKEN", "")
OAUTH_REFRESH_TOKEN = env("OAUTH_REFRESH_TOKEN", "")

# Validate required credentials
missing = [k for k, v in {
    "APP_ID": APP_ID, 
    "API_HASH": API_HASH, 
    "BOT_TOKEN": BOT_TOKEN,
    "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
    "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET
}.items() if not v]

if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

# ─────────────────────── Google Drive OAuth Setup ────────────────────────
def get_drive_service():
    """Initialize Google Drive service using OAuth credentials from env vars."""
    
    if not OAUTH_TOKEN or not OAUTH_REFRESH_TOKEN:
        # First time setup - generate auth URL
        client_config = {
            "installed": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"]
            }
        }
        
        flow = Flow.from_client_config(client_config, SCOPES)
        flow.redirect_uri = REDIRECT_URI
        
        auth_url, _ = flow.authorization_url(prompt='consent')
        
        print("\n" + "="*60)
        print("🔐 FIRST TIME OAUTH SETUP REQUIRED")
        print("="*60)
        print("1. Visit this URL in your browser:")
        print(f"   {auth_url}")
        print("\n2. After authorization, you'll get a code")
        print("3. Set these Railway environment variables:")
        print("   OAUTH_TOKEN=<access_token>")
        print("   OAUTH_REFRESH_TOKEN=<refresh_token>")
        print("\n4. Redeploy the bot")
        print("="*60)
        
        raise RuntimeError("OAuth setup required - see instructions above")
    
    # Create credentials from environment variables
    creds = Credentials(
        token=OAUTH_TOKEN,
        refresh_token=OAUTH_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES
    )
    
    # Refresh token if expired
    if creds.expired:
        print("[oauth] Refreshing expired token...")
        creds.refresh(Request())
        print(f"[oauth] New access token: {creds.token}")
        print("💡 Update OAUTH_TOKEN environment variable with the new token above")
    
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

# Initialize Drive service
try:
    drive = get_drive_service()
    print("[init] Google Drive OAuth service created")
except Exception as e:
    print(f"[ERROR] Failed to initialize Google Drive: {e}")
    exit(1)

# ─────────────────────── Telegram Client ────────────────────────────────
bot = Client(
    "gdrive-uploader-session",
    api_id=APP_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ───────────────────── Helper: Upload to Drive ──────────────────────────
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

# ───────────────────── Telegram Handlers ────────────────────────────────
@bot.on_message(filters.command("start"))
async def cmd_start(_, message):
    await message.reply_text(
        "👋 **Hi there!**\n\n"
        "Send me any **document**, **photo**, **audio**, or **video** file "
        "and I'll upload it to Google Drive using OAuth authentication.\n\n"
        "✨ **No permission issues!** ✨"
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
            
            # Upload to Google Drive
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
        raise

# ───────────────────────────── Main ──────────────────────────────────────
if __name__ == "__main__":
    print("[init] Starting Google Drive Uploader Bot (OAuth with Env Vars)...")
    print(f"[init] Target folder: {'My Drive (root)' if not DRIVE_FOLDER_ID else DRIVE_FOLDER_ID}")
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("[init] Bot stopped by user")
    except Exception as e:
        print(f"[ERROR] Bot crashed: {e}")
        raise
