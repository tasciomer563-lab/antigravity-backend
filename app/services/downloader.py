import asyncio
import html as html_lib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Literal, Optional

import yt_dlp
from PIL import Image

MediaMode = Literal["video", "audio", "thumbnail"]

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


@dataclass
class DownloadedMedia:
    file_path: str
    title: str
    temp_dir: str
    media_type: str
    extension: str


def _extract_instagram_shortcode(url: str) -> Optional[str]:
    match = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


def _try_instagram_embed_fallback(
    url: str,
    temp_dir: str,
    unique_id: str,
    mode: MediaMode,
) -> Optional[DownloadedMedia]:
    """
    Render veri merkezi IP'lerinde Instagram yt-dlp'yi 'login required' ile engellediğinde
    Instagram Embed sayfası ve açık medya vekilleri üzerinden MP4/Kapak/MP3 indirir.
    """
    shortcode = _extract_instagram_shortcode(url)
    if not shortcode:
        return None

    video_url: Optional[str] = None
    thumb_url: Optional[str] = None

    # 1. Yöntem: Instagram /p/{shortcode}/embed/captioned/ sayfasından video_url çözümü
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        req = urllib.request.Request(
            embed_url,
            headers={
                "User-Agent": MOBILE_USER_AGENT,
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            page_html = resp.read().decode("utf-8", errors="ignore")

        # Gömülü JSON içinden video_url ara
        v_match = re.search(r'"video_url"\s*:\s*"([^"]+)"', page_html)
        if v_match:
            video_url = (
                v_match.group(1)
                .encode("utf-8")
                .decode("unicode_escape")
                .replace(r"\/", "/")
            )
            video_url = html_lib.unescape(video_url)

        t_match = re.search(r'"display_url"\s*:\s*"([^"]+)"', page_html)
        if t_match:
            thumb_url = (
                t_match.group(1)
                .encode("utf-8")
                .decode("unicode_escape")
                .replace(r"\/", "/")
            )
            thumb_url = html_lib.unescape(thumb_url)
    except Exception as exc:
        print(f"[Instagram Embed Fallback] Uyarı: {exc}")

    # 2. Yöntem: Instagram GraphQL doc_id sorgusu
    if not video_url:
        try:
            graphql_url = "https://www.instagram.com/api/graphql"
            post_data = urllib.parse.urlencode(
                {
                    "av": "0",
                    "__d": "www",
                    "__user": "0",
                    "__a": "1",
                    "__req": "3",
                    "__hs": "19888.HYP:instagram_web_pkg.2.1..0.0",
                    "dpr": "1",
                    "__ccg": "UNKNOWN",
                    "lsd": "AVqbxe3J_YA",
                    "fb_api_caller_class": "RelayModern",
                    "fb_api_req_friendly_name": "PolarisPostActionLoadPostQueryQuery",
                    "variables": json.dumps({"shortcode": shortcode}),
                    "server_timestamps": "true",
                    "doc_id": "10015901848480474",
                }
            ).encode("utf-8")
            g_req = urllib.request.Request(
                graphql_url,
                data=post_data,
                headers={
                    "User-Agent": MOBILE_USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-IG-App-ID": "936619743392459",
                    "X-FB-LSD": "AVqbxe3J_YA",
                    "X-ASBD-ID": "129477",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            with urllib.request.urlopen(g_req, timeout=15) as g_resp:
                g_json = json.loads(g_resp.read().decode("utf-8", errors="ignore"))
                media_data = (
                    g_json.get("data", {}).get("xdt_shortcode_media") or {}
                )
                video_url = video_url or media_data.get("video_url")
                thumb_url = thumb_url or media_data.get("display_url")
        except Exception as exc:
            print(f"[Instagram GraphQL Fallback] Uyarı: {exc}")

    if mode == "thumbnail" and thumb_url:
        jpg_path = os.path.join(temp_dir, f"cover_{unique_id}.jpg")
        urllib.request.urlretrieve(thumb_url, jpg_path)
        with Image.open(jpg_path) as img:
            img.convert("RGB").save(jpg_path, format="JPEG", quality=95)
        return DownloadedMedia(
            file_path=jpg_path,
            title=f"instagram_{shortcode}",
            temp_dir=temp_dir,
            media_type="image/jpeg",
            extension="jpg",
        )

    if not video_url:
        return None

    mp4_path = os.path.join(temp_dir, f"media_{unique_id}.mp4")
    req_dl = urllib.request.Request(
        video_url, headers={"User-Agent": MOBILE_USER_AGENT}
    )
    with urllib.request.urlopen(req_dl, timeout=60) as v_resp, open(
        mp4_path, "wb"
    ) as out_f:
        out_f.write(v_resp.read())

    if mode == "audio":
        mp3_path = os.path.join(temp_dir, f"media_{unique_id}.mp3")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                mp4_path,
                "-vn",
                "-acodec",
                "libmp3lame",
                "-ab",
                "192k",
                mp3_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return DownloadedMedia(
            file_path=mp3_path,
            title=f"instagram_{shortcode}",
            temp_dir=temp_dir,
            media_type="audio/mpeg",
            extension="mp3",
        )

    return DownloadedMedia(
        file_path=mp4_path,
        title=f"instagram_{shortcode}",
        temp_dir=temp_dir,
        media_type="video/mp4",
        extension="mp4",
    )


def _extract_and_download_sync(url: str, mode: MediaMode = "video") -> DownloadedMedia:
    """yt-dlp'yi senkron olarak çalıştırır; Instagram rate-limit durumunda yedek çözücüyü devreye sokar."""
    temp_dir = tempfile.mkdtemp(prefix="antigravity_media_")
    unique_id = uuid.uuid4().hex[:8]
    output_template = os.path.join(temp_dir, f"media_{unique_id}.%(ext)s")

    common_headers = {
        "User-Agent": MOBILE_USER_AGENT,
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    if mode == "audio":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": common_headers,
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
        ydl_opts = {
            "skip_download": True,
            "writethumbnail": True,
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": common_headers,
        }
        target_ext = "jpg"
        mime_type = "image/jpeg"

    else:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": common_headers,
            "format_sort": ["vcodec:h264", "acodec:m4a", "ext:mp4:m4a"],
        }
        target_ext = "mp4"
        mime_type = "video/mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or f"medya_{unique_id}"
    except Exception as ytdlp_exc:
        # Eğer link Instagram ise yedek GraphQL/Embed indiriciyi dene
        if "instagram.com" in url.lower() or "instagr.am" in url.lower():
            fallback_res = _try_instagram_embed_fallback(
                url, temp_dir, unique_id, mode
            )
            if fallback_res is not None and os.path.exists(fallback_res.file_path):
                return fallback_res
        raise ytdlp_exc

    resolved_path = ""
    if mode == "thumbnail":
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
    """FastAPI ana event loop'unu bloke etmeden medya indirme işlemini yürütür."""
    return await asyncio.to_thread(_extract_and_download_sync, url, mode)
