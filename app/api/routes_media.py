import asyncio
import os
import re
import shutil
from typing import Literal, Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.core.config import get_supabase_client
from app.services.downloader import download_media_async

router = APIRouter()


class MediaDownloadRequest(BaseModel):
    # HttpUrl yerine esnek str kullanarak mobil paylaşım metinlerini veya query parametrelerini de kabul eder
    url: Optional[str] = Field(default=None, description="İndirilecek YouTube veya Instagram URL'si")
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
    return "other"


def _log_media_to_supabase_sync(
    source_url: str,
    title: str,
    media_mode: str,
) -> None:
    """Başarıyla indirilen medyayı Supabase media_history tablosuna kaydeder."""
    supabase = get_supabase_client()
    if supabase is None:
        return

    try:
        supabase.table("media_history").insert(
            {
                "source_url": source_url,
                "title": title,
                "media_mode": media_mode,
                "platform": _detect_platform(source_url),
            }
        ).execute()
    except Exception as exc:
        print(f"[Supabase] media_history kayıt hatası: {exc}")


def _friendly_download_error(exc: Exception) -> str:
    """yt-dlp'nin teknik hata metnini anlaşılır Türkçe mesaja çevirir; teknik nedeni de ekler."""
    raw = str(exc)
    low = raw.lower()
    short = raw.replace("ERROR: ", "").strip()[:220]
    if "sign in to confirm" in low or "not a bot" in low:
        hint = "YouTube sunucu IP'sini bot sanıp engelledi. Sunucuya YT_COOKIES (çerez) eklemek gerekir."
    elif "empty media response" in low or "login required" in low or "rate-limit" in low or "rate limit" in low:
        hint = "Instagram sunucu IP'sini engelledi veya giriş istiyor. Sunucuya YT_COOKIES (çerez) eklemek gerekir."
    elif "private" in low:
        hint = "Bu video gizli (private); indirilemez."
    elif "video unavailable" in low or "has been removed" in low:
        hint = "Bu video kullanılamıyor veya kaldırılmış."
    elif "unsupported url" in low:
        hint = "Desteklenmeyen bağlantı. Lütfen YouTube veya Instagram linki gir."
    else:
        hint = "Medya indirilemedi."
    return f"{hint} (Teknik: {short})"


def _cleanup_temp_dir(temp_dir: str) -> None:
    """Yanıt istemciye ulaştıktan sonra sunucudaki geçici medya klasörünü temizler."""
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


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
            # Content-Type belirtilmemişse yine de JSON parse dene
            raw_bytes = await request.body()
            if raw_bytes:
                import json

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
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        },
        background=BackgroundTask(_cleanup_temp_dir, result.temp_dir),
    )
