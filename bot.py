# bot.py ── Google-Drive Uploader + YouTube Downloader Telegram Bot
#
# Requirements (put in requirements.txt):
#   pyrogram>=2.0.106
#   tgcrypto>=1.2.5
#   google-api-python-client>=2.118.0
#   google-auth>=2.29.0
#   google-auth-httplib2>=0.2.0
#   google-auth-oauthlib>=1.2.0
#   python-dotenv>=1.0.1
#   yt-dlp>=2024.1.7

import os
import asyncio
import tempfile
import mimetypes
import time
import re
from pathlib import Path

from pyrogram import Client, filters
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import yt_dlp

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

# Google OAuth credentials
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", "")

# OAuth tokens (get from Google OAuth Playground)
OAUTH_TOKEN = env("OAUTH_TOKEN", "")
OAUTH_REFRESH_TOKEN = env("OAUTH_REFRESH_TOKEN", "")

# Optional: target folder
DRIVE_FOLDER_ID = env("DRIVE_FOLDER_ID") or None

# OAuth settings
SCOPES = ['https://www.googleapis.com/auth/drive.file']

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
        print("\n" + "="*60)
        print("🔐 OAUTH SETUP REQUIRED")
        print("="*60)
        print("Use Google OAuth Playground to get tokens:")
        print("1. Go to: https://developers.google.com/oauthplayground/")
        print("2. Click gear icon → Use your own OAuth credentials")
        print(f"3. Client ID: {GOOGLE_CLIENT_ID}")
        print("4. Client Secret: [your secret]")
        print("5. Select Drive API v3 scope")
        print("6. Authorize and exchange for tokens")
        print("7. Set OAUTH_TOKEN and OAUTH_REFRESH_TOKEN in Railway")
        print("="*60)
        
        raise RuntimeError("OAuth tokens missing - use OAuth Playground")
    
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
    if creds.expired and creds.refresh_token:
        print("[oauth] Refreshing expired token...")
        try:
            creds.refresh(Request())
            print("[oauth] Token refreshed successfully")
        except Exception as e:
            print(f"[oauth] Token refresh failed: {e}")
            raise RuntimeError("Token refresh failed - get new tokens from OAuth Playground")
    
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

# ───────────────────── Progress Tracking Class ──────────────────────────
class ProgressTracker:
    def __init__(self, progress_msg, file_name, total_size=None):
        self.progress_msg = progress_msg
        self.file_name = file_name
        self.total_size = total_size
        self.uploaded_size = 0
        self.start_time = time.time()
        self.last_update = 0
        
    def format_size(self, size_bytes):
        """Convert bytes to human readable format."""
        if size_bytes == 0:
            return "0 B"
        size_names = ["B", "KB", "MB", "GB"]
        i = 0
        while size_bytes >= 1024 and i < len(size_names) - 1:
            size_bytes /= 1024.0
            i += 1
        return f"{size_bytes:.1f} {size_names[i]}"
    
    def format_speed(self, speed_bps):
        """Convert bytes per second to human readable format."""
        return self.format_size(speed_bps) + "/s"
    
    async def update_download(self, downloaded_bytes, total_bytes=None, speed=None):
        """Update download progress."""
        current_time = time.time()
        
        # Only update every 2 seconds to avoid rate limits
        if current_time - self.last_update < 2:
            return
            
        self.last_update = current_time
        
        if total_bytes:
            progress_percent = (downloaded_bytes / total_bytes) * 100
            bar_length = 10
            filled_length = int(bar_length * progress_percent / 100)
            bar = "█" * filled_length + "░" * (bar_length - filled_length)
            
            progress_text = (
                f"⬇️ **Downloading from YouTube...**\n\n"
                f"📁 **Video:** `{self.file_name}`\n"
                f"📊 **Progress:** {progress_percent:.1f}% `{bar}`\n"
                f"📈 **Speed:** {speed or 'calculating...'}\n"
                f"📦 **Size:** {self.format_size(downloaded_bytes)} / {self.format_size(total_bytes)}"
            )
        else:
            progress_text = (
                f"⬇️ **Downloading from YouTube...**\n\n"
                f"📁 **Video:** `{self.file_name}`\n"
                f"📦 **Downloaded:** {self.format_size(downloaded_bytes)}\n"
                f"📈 **Speed:** {speed or 'calculating...'}"
            )
        
        try:
            await self.progress_msg.edit_text(progress_text)
        except Exception:
            pass
    
    async def update_upload(self, uploaded_bytes):
        """Update upload progress."""
        if not self.total_size:
            return
            
        current_time = time.time()
        
        # Only update every 2 seconds to avoid rate limits
        if current_time - self.last_update < 2:
            return
            
        self.last_update = current_time
        
        # Calculate progress
        progress_percent = (uploaded_bytes / self.total_size) * 100
        elapsed_time = current_time - self.start_time
        
        # Calculate speed
        if elapsed_time > 0:
            speed_bps = uploaded_bytes / elapsed_time
            speed_text = self.format_speed(speed_bps)
            
            # Estimate remaining time
            if speed_bps > 0:
                remaining_bytes = self.total_size - uploaded_bytes
                eta_seconds = remaining_bytes / speed_bps
                eta_text = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"
            else:
                eta_text = "calculating..."
        else:
            speed_text = "calculating..."
            eta_text = "calculating..."
        
        # Create progress bar
        bar_length = 10
        filled_length = int(bar_length * progress_percent / 100)
        bar = "█" * filled_length + "░" * (bar_length - filled_length)
        
        # Update message
        progress_text = (
            f"⬆️ **Uploading to Google Drive...**\n\n"
            f"📁 **File:** `{self.file_name}`\n"
            f"📊 **Progress:** {progress_percent:.1f}% `{bar}`\n"
            f"📈 **Speed:** {speed_text}\n"
            f"📦 **Size:** {self.format_size(uploaded_bytes)} / {self.format_size(self.total_size)}\n"
            f"⏱ **ETA:** {eta_text}"
        )
        
        try:
            await self.progress_msg.edit_text(progress_text)
        except Exception:
            pass

# ───────────────────── YouTube Downloader ──────────────────────────────
class YouTubeDownloader:
    def __init__(self, progress_tracker):
        self.progress_tracker = progress_tracker
        
    def progress_hook(self, d):
        """Progress hook for yt-dlp."""
        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            speed = d.get('speed')
            
            if speed:
                speed_text = self.progress_tracker.format_speed(speed)
            else:
                speed_text = "calculating..."
            
            # Run update in async context
            asyncio.create_task(
                self.progress_tracker.update_download(downloaded, total, speed_text)
            )
    
    async def download_video(self, url, output_dir):
        """Download video from YouTube and return file path."""
        ydl_opts = {
            'format': 'best[height<=720]/best',  # Max 720p to save bandwidth
            'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [self.progress_hook],
            'no_warnings': True,
            'extractaudio': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info first
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            video_title = info.get('title', 'Unknown Video')
            
            # Update progress tracker with video title
            self.progress_tracker.file_name = f"{video_title}.{info.get('ext', 'mp4')}"
            
            # Download the video
            await asyncio.to_thread(ydl.download, [url])
            
            # Find the downloaded file
            for file in os.listdir(output_dir):
                if file.endswith(('.mp4', '.mkv', '.webm', '.avi')):
                    return os.path.join(output_dir, file)
            
            raise RuntimeError("Downloaded file not found")

# ───────────────────── Helper: Upload to Drive (WITH PROGRESS) ──────────
async def upload_to_drive_with_progress(local_path: str, remote_name: str, progress_tracker) -> str:
    """Upload a local file to Google Drive with real-time progress updates."""
    mime_type = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
    
    # Get file size and update progress tracker
    file_size = os.path.getsize(local_path)
    progress_tracker.total_size = file_size
    progress_tracker.start_time = time.time()  # Reset start time for upload
    
    # Create media upload with progress callback
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    
    body = {"name": remote_name}
    if DRIVE_FOLDER_ID:
        body["parents"] = [DRIVE_FOLDER_ID]
    
    try:
        # Create the file on Google Drive
        request = drive.files().create(body=body, media_body=media, fields="id, webViewLink")
        
        response = None
        while response is None:
            # Execute next chunk of the upload
            status, response = request.next_chunk()
            
            if status:
                # Update progress
                uploaded_bytes = int(status.resumable_progress)
                await progress_tracker.update_upload(uploaded_bytes)
        
        return response["webViewLink"]
        
    except Exception as e:
        print(f"[ERROR] Drive upload failed: {e}")
        raise

# ───────────────────── URL Detection ────────────────────────────────────
def is_youtube_url(url):
    """Check if URL is a valid YouTube URL."""
    youtube_patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=[\w-]+',
        r'(?:https?://)?(?:www\.)?youtu\.be/[\w-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/[\w-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/v/[\w-]+',
        r'(?:https?://)?(?:m\.)?youtube\.com/watch\?v=[\w-]+',
    ]
    
    for pattern in youtube_patterns:
        if re.match(pattern, url, re.IGNORECASE):
            return True
    return False

# ───────────────────── Telegram Handlers ────────────────────────────────
@bot.on_message(filters.command("start"))
async def cmd_start(_, message):
    await message.reply_text(
        "👋 **Welcome to Google Drive Uploader Bot!**\n\n"
        "🎯 **What I can do:**\n\n"
        "📁 **File Upload:** Send any document/photo/audio/video\n"
        "🎬 **YouTube Download:** Send any YouTube URL\n\n"
        "✨ **Features:**\n"
        "• Real-time progress tracking\n"
        "• Upload speed monitoring\n"
        "• ETA calculation\n"
        "• High-quality downloads (720p max)\n\n"
        "🚀 **Ready to upload!**\n\n"
        "💡 **Examples:**\n"
        "`https://youtube.com/watch?v=xyz123`\n"
        "`https://youtu.be/abc456`"
    )

@bot.on_message(filters.text & ~filters.command([]))
async def handle_youtube_url(client, message):
    """Handle YouTube URLs."""
    text = message.text.strip()
    
    if not is_youtube_url(text):
        await message.reply_text(
            "❌ **Invalid URL**\n\n"
            "Please send a valid YouTube URL like:\n"
            "• `https://youtube.com/watch?v=xyz123`\n"
            "• `https://youtu.be/abc456`\n\n"
            "Or send a file directly for upload."
        )
        return
    
    # Show initial progress
    progress_msg = await message.reply_text("🔍 **Extracting video info...**")
    
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create progress tracker
            progress_tracker = ProgressTracker(progress_msg, "Unknown Video")
            
            # Download video from YouTube
            downloader = YouTubeDownloader(progress_tracker)
            video_path = await downloader.download_video(text, temp_dir)
            
            if not video_path or not os.path.exists(video_path):
                raise RuntimeError("Download failed - file not found")
            
            # Get final filename
            video_filename = os.path.basename(video_path)
            progress_tracker.file_name = video_filename
            
            # Start upload to Google Drive
            await progress_msg.edit_text("⬆️ **Preparing upload to Google Drive...**")
            
            # Upload with progress tracking
            drive_link = await upload_to_drive_with_progress(video_path, video_filename, progress_tracker)

        # Success message
        await progress_msg.edit_text(
            f"✅ **YouTube video uploaded successfully!**\n\n"  
            f"🎬 **Video:** `{video_filename}`\n"
            f"🔗 **Google Drive Link:**\n{drive_link}\n\n"
            f"🎉 **Ready for next download!**"
        )

    except Exception as e:
        error_msg = f"❌ **YouTube download failed:** {str(e)}"
        await progress_msg.edit_text(error_msg)
        print(f"[ERROR] YouTube download failed for user {message.from_user.id}: {e}")

@bot.on_message(filters.document | filters.audio | filters.video | filters.photo)
async def handle_file(client, message):
    """Handle incoming files and upload them to Google Drive with progress."""
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

    # Show initial progress
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

            # Create progress tracker and start upload
            file_size = os.path.getsize(local_path)
            progress_tracker = ProgressTracker(progress_msg, file_name, file_size)
            
            # Upload to Google Drive with progress updates
            drive_link = await upload_to_drive_with_progress(local_path, file_name, progress_tracker)

        # Success message
        await progress_msg.edit_text(
            f"✅ **File uploaded successfully!**\n\n"  
            f"📁 **File:** `{file_name}`\n"
            f"🔗 **Google Drive Link:**\n{drive_link}\n\n"
            f"🎉 **Ready for next upload!**"
        )

    except Exception as e:
        error_msg = f"❌ **Upload failed:** {str(e)}"
        await progress_msg.edit_text(error_msg)
        print(f"[ERROR] Upload failed for user {message.from_user.id}: {e}")

# ───────────────────────────── Main ──────────────────────────────────────
if __name__ == "__main__":
    print("[init] Starting Google Drive Uploader + YouTube Downloader Bot...")
    print(f"[init] Target folder: {'My Drive (root)' if not DRIVE_FOLDER_ID else DRIVE_FOLDER_ID}")
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n[init] Bot stopped by user")
    except Exception as e:
        print(f"[ERROR] Bot crashed: {e}")
        raise
