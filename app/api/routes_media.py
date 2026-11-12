import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Literal, Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.core.config import get_supabase_client
from app.core.local_store import add_history_entry, get_history_entries
from app.services.downloader import download_media_async, fetch_media_info_async

router = APIRouter()


class MediaDownloadRequest(BaseModel):
    url: Optional[str] = Field(default=None, description="İndirilecek YouTube, Instagram veya TikTok URL'si")
    video_url: Optional[str] = Field(default=None, description="Alternatif URL alanı")
    mode: Literal["video", "audio", "thumbnail"] = "video"


def _extract_clean_url(raw_text: str) -> str:
    """Kullanıcının yapıştırdığı metin içinde başlık veya açıklama varsa sadece https://... bağlantısını ayıklar."""
    cleaned = raw_text.strip()
    match = re.search(r"(https?://[^\s\"'<>]+)", cleaned)
    if match:
        return match.group(1)
    if cleaned.startswith("www."):
        return f"https://{cleaned}"
    return cleaned


def _detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "instagram.com" in host or "instagr.am" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    if "twitter.com" in host or "x.com" in host:
        return "twitter"
    return "other"


def _log_media_to_supabase_sync(
    source_url: str,
    title: str,
    media_mode: str,
) -> None:
    """Başarıyla indirilen medyayı hem yerel geçmişe hem de Supabase media_history tablosuna kaydeder."""
    platform = _detect_platform(source_url)
    add_history_entry(
        "media_history",
        {
            "source_url": source_url,
            "title": title,
            "media_mode": media_mode,
            "platform": platform,
        },
    )

    supabase = get_supabase_client()
    if supabase is None:
        return

    try:
        supabase.table("media_history").insert(
            {
                "source_url": source_url,
                "title": title,
                "media_mode": media_mode,
                "platform": platform,
            }
        ).execute()
    except Exception as exc:
        print(f"[Supabase] media_history kayıt hatası: {exc}")


def _friendly_download_error(exc: Exception) -> str:
    """yt-dlp'nin teknik hata metnini anlaşılır Türkçe mesaja çevirir; teknik nedeni de ekler."""
    raw = str(exc)
    low = raw.lower()
    short = raw.replace("ERROR: ", "").strip()[-450:]
    if "sign in to confirm" in low or "not a bot" in low:
        hint = "YouTube sunucu IP'sini bot sanıp engelledi. Sunucuya YT_COOKIES (çerez) eklemek gerekir."
    elif "empty media response" in low or "login required" in low or "rate-limit" in low or "rate limit" in low:
        hint = "Instagram sunucu IP'sini engelledi veya giriş istiyor. Sunucuya YT_COOKIES (çerez) eklemek gerekir."
    elif "private" in low:
        hint = "Bu video gizli (private); indirilemez."
    elif "video unavailable" in low or "has been removed" in low:
        hint = "Bu video kullanılamıyor veya kaldırılmış."
    elif "unsupported url" in low:
        hint = "Desteklenmeyen bağlantı. Lütfen geçerli bir video linki gir."
    else:
        hint = "Medya indirilemedi."
    return f"{hint} (Teknik: {short})"


def _cleanup_temp_dir(temp_dir: str) -> None:
    """Yanıt istemciye ulaştıktan sonra sunucudaki geçici medya klasörünü temizler."""
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.post("/info")
async def get_media_info(
    request: Request,
    url_query: Optional[str] = Query(default=None, alias="url"),
):
    """Verilen URL için indirme öncesi başlık, kapak fotoğrafı, süre ve platform bilgisini döner."""
    raw_url: Optional[str] = url_query
    try:
        raw_bytes = await request.body()
        if raw_bytes:
            body = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
            if isinstance(body, dict):
                raw_url = raw_url or body.get("url") or body.get("video_url")
    except Exception:
        pass

    if not raw_url or not str(raw_url).strip():
        raise HTTPException(status_code=400, detail="Lütfen geçerli bir 'url' gönderin.")

    clean_url = _extract_clean_url(str(raw_url))
    info = await fetch_media_info_async(clean_url)
    info["detected_platform"] = _detect_platform(clean_url)
    return info


@router.post("/download")
async def download_media(
    request: Request,
    url_query: Optional[str] = Query(default=None, alias="url"),
    mode_query: Optional[str] = Query(default=None, alias="mode"),
):
    """
    Hem JSON Body ({"url": "...", "mode": "video"}),
    hem FormData, hem de Query Parametresi (?url=...&mode=...) formatlarını kabul eder.
    """
    raw_url: Optional[str] = url_query
    raw_mode: str = mode_query or "video"

    content_type = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict):
                raw_url = raw_url or body.get("url") or body.get("video_url") or body.get("link")
                raw_mode = body.get("mode") or raw_mode
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            raw_url = raw_url or str(form.get("url") or form.get("video_url") or "")
            raw_mode = str(form.get("mode") or raw_mode)
        else:
            raw_bytes = await request.body()
            if raw_bytes:
                body = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
                if isinstance(body, dict):
                    raw_url = raw_url or body.get("url") or body.get("video_url")
                    raw_mode = body.get("mode") or raw_mode
    except Exception:
        pass

    if not raw_url or not str(raw_url).strip():
        raise HTTPException(
            status_code=400,
            detail="Lütfen geçerli bir 'url' parametresi gönderin.",
        )

    clean_url = _extract_clean_url(str(raw_url))
    valid_mode: Literal["video", "audio", "thumbnail"] = (
        raw_mode if raw_mode in ("video", "audio", "thumbnail") else "video"  # type: ignore[assignment]
    )

    try:
        result = await download_media_async(clean_url, mode=valid_mode)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=_friendly_download_error(exc),
        ) from exc

    if not result.file_path or not os.path.exists(result.file_path):
        _cleanup_temp_dir(result.temp_dir)
        raise HTTPException(status_code=500, detail="Medya dosyası oluşturulamadı.")

    await asyncio.to_thread(
        _log_media_to_supabase_sync,
        clean_url,
        result.title,
        valid_mode,
    )

    safe_title = "".join(
        c if c.isalnum() or c in (" ", "-", "_") else "_" for c in result.title
    ).strip()[:60] or "medya"
    final_filename = f"{safe_title}.{result.extension}"
    encoded_filename = quote(final_filename)

    return FileResponse(
        path=result.file_path,
        media_type=result.media_type,
        filename=final_filename,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "X-Video-Title": quote(result.title[:100]),
        },
        background=BackgroundTask(_cleanup_temp_dir, result.temp_dir),
    )


def _compress_video_ffmpeg_sync(
    input_path: str, output_path: str, quality: str
) -> None:
    """FFmpeg ile videoyu seçilen kalite seviyesinde (low/medium/high) küçültür."""
    q = quality.lower().strip()
    if q == "low":
        crf = "30"
        max_h = 480
        audio_br = "96k"
    elif q == "high":
        crf = "21"
        max_h = 1080
        audio_br = "160k"
    else:
        crf = "26"
        max_h = 720
        audio_br = "128k"

    vf_scale = (
        f"scale='if(gt(ih,{max_h}),trunc(iw*{max_h}/ih/2)*2,trunc(iw/2)*2)'"
        f":'if(gt(ih,{max_h}),{max_h},trunc(ih/2)*2)'"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-vf",
        vf_scale,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        audio_br,
        "-movflags",
        "+faststart",
        output_path,
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=240,
    )
    if proc.returncode != 0 or not os.path.exists(output_path):
        err_msg = proc.stderr.decode("utf-8", errors="ignore")[-300:]
        raise RuntimeError(f"FFmpeg video küçültme hatası: {err_msg}")


@router.post("/compress")
async def compress_video_endpoint(
    file: UploadFile = File(...),
    quality: str = Form("medium"),
):
    """
    Web ve Mobil için Bulut / Yerel FFmpeg Video Küçültücü:
    Yüklenen videoyu H.264/AAC ile sıkıştırır ve MP4 olarak döner.
    """
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Boş video dosyası gönderildi.")

    original_size = len(raw_bytes)
    temp_dir = tempfile.mkdtemp(prefix="antigravity_vcomp_")
    uid = uuid.uuid4().hex[:8]
    orig_ext = os.path.splitext(file.filename or "video.mp4")[1].lower() or ".mp4"
    in_path = os.path.join(temp_dir, f"in_{uid}{orig_ext}")
    out_path = os.path.join(temp_dir, f"compressed_{uid}.mp4")

    with open(in_path, "wb") as fh:
        fh.write(raw_bytes)

    try:
        await asyncio.to_thread(_compress_video_ffmpeg_sync, in_path, out_path, quality)
    except Exception as exc:
        _cleanup_temp_dir(temp_dir)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    compressed_size = os.path.getsize(out_path)
    base_name = os.path.splitext(file.filename or "Video")[0]
    safe_out_name = f"{base_name}_Kucultulmus.mp4"
    encoded_name = quote(safe_out_name)

    return FileResponse(
        path=out_path,
        media_type="video/mp4",
        filename=safe_out_name,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "X-Original-Size-Bytes": str(original_size),
            "X-Compressed-Size-Bytes": str(compressed_size),
        },
        background=BackgroundTask(_cleanup_temp_dir, temp_dir),
    )


@router.get("/history")
async def get_media_history():
    """İndirilen medya geçmişini döner."""
    items: list[dict[str, Any]] = []
    supabase = get_supabase_client()
    if supabase is not None:
        try:
            resp = (
                supabase.table("media_history")
                .select("*")
                .order("created_at", desc=True)
                .limit(30)
                .execute()
            )
            if resp.data:
                items.extend(resp.data)
        except Exception:
            pass

    local_items = await asyncio.to_thread(get_history_entries, "media_history")
    items.extend(local_items)
    return {"items": items[:40]}
