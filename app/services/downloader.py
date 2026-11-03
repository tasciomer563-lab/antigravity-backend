import asyncio
import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Literal
import yt_dlp
from PIL import Image

MediaMode = Literal["video", "audio", "thumbnail"]


@dataclass
class DownloadedMedia:
    file_path: str
    title: str
    temp_dir: str
    media_type: str
    extension: str


def _extract_and_download_sync(url: str, mode: MediaMode = "video") -> DownloadedMedia:
    """yt-dlp'yi senkron olarak çalıştırır; video (mp4), ses (mp3) veya kapak (jpg) indirir."""
    temp_dir = tempfile.mkdtemp(prefix="antigravity_media_")
    unique_id = uuid.uuid4().hex[:8]
    output_template = os.path.join(temp_dir, f"media_{unique_id}.%(ext)s")

    if mode == "audio":
        # Sadece Ses (MP3 - Car Therapy / OddlyViral trend sesleri için)
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        target_ext = "mp3"
        mime_type = "audio/mpeg"

    elif mode == "thumbnail":
        # Sadece Yüksek Çözünürlüklü Kapak Fotoğrafı (Videoyu indirmeden)
        ydl_opts = {
            "skip_download": True,
            "writethumbnail": True,
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        target_ext = "jpg"
        mime_type = "image/jpeg"

    else:
        # Varsayılan: iOS Fotoğraflar ile %100 uyumlu Filigransız MP4 Video
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/mp4",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "format_sort": ["vcodec:h264", "acodec:m4a", "ext:mp4:m4a"],
        }
        target_ext = "mp4"
        mime_type = "video/mp4"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title") or f"medya_{unique_id}"

    # İndirilen dosyayı temp_dir içinde bul
    resolved_path = ""
    if mode == "thumbnail":
        # webp/png inmişse iOS Galeri uyumu için yüksek kaliteli JPEG'e çevir
        for fname in os.listdir(temp_dir):
            full_p = os.path.join(temp_dir, fname)
            if os.path.isfile(full_p):
                jpg_path = os.path.join(temp_dir, f"cover_{unique_id}.jpg")
                with Image.open(full_p) as img:
                    img.convert("RGB").save(jpg_path, format="JPEG", quality=95)
                resolved_path = jpg_path
                break
    else:
        for fname in os.listdir(temp_dir):
            if fname.endswith(f".{target_ext}"):
                resolved_path = os.path.join(temp_dir, fname)
                break
        if not resolved_path:
            # Herhangi bir oluşan medya dosyasını yedek olarak al
            files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir)]
            if files:
                resolved_path = files[0]

    return DownloadedMedia(
        file_path=resolved_path,
        title=title,
        temp_dir=temp_dir,
        media_type=mime_type,
        extension=target_ext,
    )


async def download_media_async(url: str, mode: MediaMode = "video") -> DownloadedMedia:
    """FastAPI ana event loop'unu bloke etmeden yt-dlp işlemini thread havuzunda yürütür."""
    return await asyncio.to_thread(_extract_and_download_sync, url, mode)
