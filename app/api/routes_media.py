import asyncio
import os
import shutil
from typing import Literal
from urllib.parse import quote, urlparse
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
from starlette.background import BackgroundTask
from app.core.config import get_supabase_client
from app.services.downloader import download_media_async

router = APIRouter()


class MediaDownloadRequest(BaseModel):
    url: HttpUrl
    mode: Literal["video", "audio", "thumbnail"] = "video"


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


def _cleanup_temp_dir(temp_dir: str) -> None:
    """Yanıt istemciye ulaştıktan sonra sunucudaki geçici medya klasörünü temizler."""
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.post("/download")
async def download_media(payload: MediaDownloadRequest):
    url_str = str(payload.url)

    try:
        result = await download_media_async(url_str, mode=payload.mode)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Medya indirilemedi veya bağlantı desteklenmiyor: {exc}",
        ) from exc

    if not result.file_path or not os.path.exists(result.file_path):
        _cleanup_temp_dir(result.temp_dir)
        raise HTTPException(status_code=500, detail="Medya dosyası oluşturulamadı.")

    # Başarılı indirme işlemini Supabase media_history tablosuna kaydet
    await asyncio.to_thread(
        _log_media_to_supabase_sync,
        url_str,
        result.title,
        payload.mode,
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
