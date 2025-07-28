# bot.py ── Enhanced YouTube Downloader with Multi-Quality Support
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
import threading
import subprocess
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
OAUTH_TOKEN = env("OAUTH_TOKEN", "")
OAUTH_REFRESH_TOKEN = env("OAUTH_REFRESH_TOKEN", "")
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

# ─────────────────────── FFmpeg Setup ──────────────────────────────────
async def ensure_ffmpeg():
    """Ensure FFmpeg is available for merging video+audio streams."""
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        print("[init] FFmpeg found")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("[init] FFmpeg not found, installing...")
        try:
            # Install FFmpeg on Railway/Ubuntu
            subprocess.run(['apt-get', 'update'], check=True)
            subprocess.run(['apt-get', 'install', '-y', 'ffmpeg'], check=True)
            print("[init] FFmpeg installed successfully")
            return True
        except subprocess.CalledProcessError:
            print("[init] Failed to install FFmpeg - 1080p downloads may fail")
            return False

# ─────────────────────── Google Drive OAuth Setup ────────────────────────
def get_drive_service():
    """Initialize Google Drive service using OAuth credentials from env vars."""
    
    if not OAUTH_TOKEN or not OAUTH_REFRESH_TOKEN:
        raise RuntimeError("OAuth tokens missing - use OAuth Playground")
    
    creds = Credentials(
        token=OAUTH_TOKEN,
        refresh_token=OAUTH_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES
    )
    
    if creds.expired and creds.refresh_token:
        print("[oauth] Refreshing expired token...")
        try:
            creds.refresh(Request())
            print("[oauth] Token refreshed successfully")
        except Exception as e:
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

# ───────────────────── Enhanced Progress Tracking ──────────────────────
class ProgressTracker:
    def __init__(self, progress_msg, file_name, total_size=None):
        self.progress_msg = progress_msg
        self.file_name = file_name
        self.total_size = total_size
        self.uploaded_size = 0
        self.start_time = time.time()
        self.last_update = 0
        
        # For YouTube download progress (thread-safe)
        self.download_progress = {
            'downloaded': 0,
            'total': 0,
            'speed': None,
            'quality': None,
            'last_update': 0
        }
        self.progress_lock = threading.Lock()
        
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
    
    def update_download_sync(self, downloaded_bytes, total_bytes=None, speed=None, quality=None):
        """Thread-safe update of download progress."""
        with self.progress_lock:
            self.download_progress['downloaded'] = downloaded_bytes
            if total_bytes:
                self.download_progress['total'] = total_bytes
            if speed:
                self.download_progress['speed'] = speed
            if quality:
                self.download_progress['quality'] = quality
    
    async def check_and_update_download(self):
        """Check for download progress updates and update message if needed."""
        current_time = time.time()
        
        if current_time - self.last_update < 3:
            return
            
        with self.progress_lock:
            downloaded = self.download_progress['downloaded']
            total = self.download_progress['total']
            speed = self.download_progress['speed']
            quality = self.download_progress['quality']
        
        if downloaded == 0:
            return
            
        self.last_update = current_time
        
        if total and total > 0:
            progress_percent = (downloaded / total) * 100
            bar_length = 10
            filled_length = int(bar_length * progress_percent / 100)
            bar = "█" * filled_length + "░" * (bar_length - filled_length)
            
            speed_text = self.format_speed(speed) if speed else "calculating..."
            quality_text = f" | **Quality:** {quality}" if quality else ""
            
            progress_text = (
                f"⬇️ **Downloading from YouTube...**\n\n"
                f"🎬 **Video:** `{self.file_name}`\n"
                f"📊 **Progress:** {progress_percent:.1f}% `{bar}`\n"
                f"📈 **Speed:** {speed_text}{quality_text}\n"
                f"📦 **Size:** {self.format_size(downloaded)} / {self.format_size(total)}"
            )
        else:
            speed_text = self.format_speed(speed) if speed else "calculating..."
            quality_text = f" | **Quality:** {quality}" if quality else ""
            progress_text = (
                f"⬇️ **Downloading from YouTube...**\n\n"
                f"🎬 **Video:** `{self.file_name}`\n"
                f"📦 **Downloaded:** {self.format_size(downloaded)}\n"
                f"📈 **Speed:** {speed_text}{quality_text}"
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
        
        if current_time - self.last_update < 2:
            return
            
        self.last_update = current_time
        
        progress_percent = (uploaded_bytes / self.total_size) * 100
        elapsed_time = current_time - self.start_time
        
        if elapsed_time > 0:
            speed_bps = uploaded_bytes / elapsed_time
            speed_text = self.format_speed(speed_bps)
            
            if speed_bps > 0:
                remaining_bytes = self.total_size - uploaded_bytes
                eta_seconds = remaining_bytes / speed_bps
                eta_text = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"
            else:
                eta_text = "calculating..."
        else:
            speed_text = "calculating..."
            eta_text = "calculating..."
        
        bar_length = 10
        filled_length = int(bar_length * progress_percent / 100)
        bar = "█" * filled_length + "░" * (bar_length - filled_length)
        
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

# ───────────────────── Multi-Quality YouTube Downloader ─────────────────
class YouTubeDownloader:
    def __init__(self, progress_tracker, quality_preference="best"):
        self.progress_tracker = progress_tracker
        self.quality_preference = quality_preference
        
    def progress_hook(self, d):
        """Progress hook for yt-dlp."""
        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            speed = d.get('speed')
            
            # Extract quality info if available
            filename = d.get('filename', '')
            quality = self._extract_quality_from_filename(filename)
            
            self.progress_tracker.update_download_sync(downloaded, total, speed, quality)
    
    def _extract_quality_from_filename(self, filename):
        """Extract quality info from filename."""
        if not filename:
            return None
            
        # Look for resolution indicators
        for res in ['2160p', '1440p', '1080p', '720p', '480p', '360p', '240p']:
            if res in filename:
                return res
        return None
    
    def _get_format_string(self, quality):
        """Get yt-dlp format string based on quality preference."""
        format_options = {
            "best": "bestvideo[height<=1080]+bestaudio/best",  # Auto-select up to 1080p
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best", 
            "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
            "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
            "audio": "bestaudio/best"
        }
        
        return format_options.get(quality, format_options["best"])
    
    async def get_video_info(self, url):
        """Get video information including available qualities."""
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            
            # Extract available qualities
            formats = info.get('formats', [])
            qualities = set()
            
            for fmt in formats:
                height = fmt.get('height')
                if height:
                    if height >= 2160:
                        qualities.add('4K')
                    elif height >= 1440:
                        qualities.add('1440p')
                    elif height >= 1080:
                        qualities.add('1080p')
                    elif height >= 720:
                        qualities.add('720p')
                    elif height >= 480:
                        qualities.add('480p')
                    else:
                        qualities.add('360p')
            
            return {
                'title': info.get('title', 'Unknown Video'),
                'duration': info.get('duration', 0),
                'uploader': info.get('uploader', 'Unknown'),
                'view_count': info.get('view_count', 0),
                'qualities': sorted(qualities, key=lambda x: {'4K': 4, '1440p': 3, '1080p': 2, '720p': 1, '480p': 0, '360p': -1}.get(x, -2), reverse=True)
            }
    
    async def download_video(self, url, output_dir):
        """Download video with the best available quality."""
        format_string = self._get_format_string(self.quality_preference)
        
        ydl_opts = {
            'format': format_string,
            'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [self.progress_hook],
            'no_warnings': True,
            'extractaudio': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'merge_output_format': 'mp4',  # Ensure MP4 output
        }
        
        # Start progress monitoring
        progress_task = asyncio.create_task(self._monitor_progress())
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info and update filename
                info = await asyncio.to_thread(ydl.extract_info, url, download=False)
                video_title = info.get('title', 'Unknown Video')
                
                # Truncate long titles
                safe_title = video_title[:50] + ('...' if len(video_title) > 50 else '')
                self.progress_tracker.file_name = f"{safe_title}.mp4"
                
                # Download the video
                await asyncio.to_thread(ydl.download, [url])
                
                # Find the downloaded file
                for file in os.listdir(output_dir):
                    if file.endswith(('.mp4', '.mkv', '.webm', '.avi')):
                        return os.path.join(output_dir, file)
                
                raise RuntimeError("Downloaded file not found")
        finally:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
    
    async def _monitor_progress(self):
        """Monitor download progress and update UI."""
        try:
            while True:
                await self.progress_tracker.check_and_update_download()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

# ───────────────────── Helper: Upload to Drive ──────────────────────────
async def upload_to_drive_with_progress(local_path: str, remote_name: str, progress_tracker) -> str:
    """Upload a local file to Google Drive with progress tracking."""
    mime_type = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
    
    file_size = os.path.getsize(local_path)
    progress_tracker.total_size = file_size
    progress_tracker.start_time = time.time()
    
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    
    body = {"name": remote_name}
    if DRIVE_FOLDER_ID:
        body["parents"] = [DRIVE_FOLDER_ID]
    
    try:
        request = drive.files().create(body=body, media_body=media, fields="id, webViewLink")
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            
            if status:
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
    ]
    
    for pattern in youtube_patterns:
        if re.match(pattern, url, re.IGNORECASE):
            return True
    return False

def parse_quality_command(text):
    """Parse quality commands like '/yt 1080p URL' or '/ytaudio URL'."""
    # Check for quality commands
    patterns = [
        r'/yt\s+(1080p?|720p?|480p?|360p?|best)\s+(.+)',
        r'/ytaudio\s+(.+)',
        r'/yt\s+(.+)'
    ]
    
    for pattern in patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            if 'ytaudio' in pattern:
                return 'audio', match.group(1)
            elif len(match.groups()) == 2:
                return match.group(1).lower(), match.group(2)
            else:
                return 'best', match.group(1)
    
    return None, None

# ───────────────────── Telegram Handlers ────────────────────────────────
@bot.on_message(filters.command("start"))
async def cmd_start(_, message):
    await message.reply_text(
        "👋 **Welcome to Advanced YouTube Downloader + Drive Uploader!**\n\n"
        "🎯 **Multi-Quality YouTube Downloads:**\n"
        "• **Auto Quality:** Send any YouTube URL\n"
        "• **1080p:** `/yt 1080p <URL>`\n"
        "• **720p:** `/yt 720p <URL>`\n"
        "• **Audio Only:** `/ytaudio <URL>`\n\n"
        "📁 **Direct File Upload:** Send any file\n\n"
        "✨ **Features:**\n"
        "• **Smart Quality Detection** - Automatically selects best available\n"
        "• **1080p Support** - Full HD downloads with FFmpeg\n"
        "• **Real-time Progress** - Speed, ETA, quality indicators\n"
        "• **Multiple Formats** - MP4, Audio, any resolution\n\n"
        "🚀 **Examples:**\n"
        "`https://youtube.com/watch?v=xyz123` - Auto quality\n"
        "`/yt 1080p https://youtu.be/abc456` - Force 1080p\n"
        "`/ytaudio https://youtu.be/def789` - Audio only"
    )

@bot.on_message(filters.text & ~filters.command(["start"]))
async def handle_youtube_url(client, message):
    """Handle YouTube URLs and quality commands."""
    text = message.text.strip()
    
    # Parse quality commands
    quality, url = parse_quality_command(text)
    
    # If not a command, check if it's a direct URL
    if not quality and is_youtube_url(text):
        quality, url = "best", text
    
    if not quality or not is_youtube_url(url):
        await message.reply_text(
            "❌ **Invalid URL or Command**\n\n"
            "**Valid formats:**\n"
            "• Direct URL: `https://youtube.com/watch?v=xyz123`\n"
            "• Quality command: `/yt 1080p <URL>`\n"
            "• Audio only: `/ytaudio <URL>`\n\n"
            "**Supported qualities:** best, 1080p, 720p, 480p, 360p"
        )
        return
    
    # Show initial progress
    progress_msg = await message.reply_text("🔍 **Analyzing video...**")
    
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create downloader with quality preference
            progress_tracker = ProgressTracker(progress_msg, "Unknown Video")
            downloader = YouTubeDownloader(progress_tracker, quality)
            
            # Get video info first
            video_info = await downloader.get_video_info(url)
            
            # Show video info
            duration_min = video_info['duration'] // 60 if video_info['duration'] else 0
            duration_sec = video_info['duration'] % 60 if video_info['duration'] else 0
            available_qualities = ", ".join(video_info['qualities']) or "Unknown"
            
            await progress_msg.edit_text(
                f"📺 **Video Found!**\n\n"
                f"🎬 **Title:** {video_info['title'][:50]}{'...' if len(video_info['title']) > 50 else ''}\n"
                f"⏱ **Duration:** {duration_min}m {duration_sec}s\n"
                f"📺 **Available Qualities:** {available_qualities}\n"
                f"🎯 **Downloading:** {quality.upper()}\n\n"
                f"⬇️ **Starting download...**"
            )
            
            # Download video
            video_path = await downloader.download_video(url, temp_dir)
            
            if not video_path or not os.path.exists(video_path):
                raise RuntimeError("Download failed - file not found")
            
            # Get final filename and start upload
            video_filename = os.path.basename(video_path)
            progress_tracker.file_name = video_filename
            
            await progress_msg.edit_text("⬆️ **Preparing Google Drive upload...**")
            
            # Upload to Google Drive
            drive_link = await upload_to_drive_with_progress(video_path, video_filename, progress_tracker)

        # Success message
        await progress_msg.edit_text(
            f"✅ **YouTube Video Uploaded Successfully!**\n\n"  
            f"🎬 **Video:** `{video_filename}`\n"
            f"🎯 **Quality:** {quality.upper()}\n"
            f"📊 **Available:** {available_qualities}\n"
            f"🔗 **Google Drive:**\n{drive_link}\n\n"
            f"🎉 **Ready for next download!**"
        )

    except Exception as e:
        error_msg = f"❌ **Download failed:** {str(e)}"
        await progress_msg.edit_text(error_msg)
        print(f"[ERROR] YouTube download failed: {e}")

@bot.on_message(filters.document | filters.audio | filters.video | filters.photo)
async def handle_file(client, message):
    """Handle direct file uploads."""
    media = message.document or message.video or message.audio or message.photo

    if hasattr(media, "file_name") and media.file_name:
        file_name = media.file_name
    else:
        if isinstance(media, list):
            uid = media[-1].file_unique_id
        else:
            uid = media.file_unique_id
        file_name = f"photo_{uid}.jpg"

    progress_msg = await message.reply_text("⬇️ **Downloading file...**")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = await client.download_media(media, temp_dir)
            
            if os.path.isdir(local_path):
                files = [os.path.join(local_path, f) for f in os.listdir(local_path)]
                if not files:
                    raise RuntimeError("Downloaded photo directory is empty")
                local_path = max(files, key=os.path.getsize)

            file_size = os.path.getsize(local_path)
            progress_tracker = ProgressTracker(progress_msg, file_name, file_size)
            
            drive_link = await upload_to_drive_with_progress(local_path, file_name, progress_tracker)

        await progress_msg.edit_text(
            f"✅ **File uploaded successfully!**\n\n"  
            f"📁 **File:** `{file_name}`\n"
            f"🔗 **Google Drive:**\n{drive_link}\n\n"
            f"🎉 **Ready for next upload!**"
        )

    except Exception as e:
        error_msg = f"❌ **Upload failed:** {str(e)}"
        await progress_msg.edit_text(error_msg)
        print(f"[ERROR] Upload failed: {e}")

# ───────────────────────────── Main ──────────────────────────────────────
if __name__ == "__main__":
    print("[init] Starting Advanced YouTube + Google Drive Bot...")
    print(f"[init] Target folder: {'My Drive (root)' if not DRIVE_FOLDER_ID else DRIVE_FOLDER_ID}")
    
    # Ensure FFmpeg is available for 1080p downloads
    asyncio.run(ensure_ffmpeg())
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n[init] Bot stopped by user")
    except Exception as e:
        print(f"[ERROR] Bot crashed: {e}")
        raise
