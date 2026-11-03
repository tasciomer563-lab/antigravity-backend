import os
import shutil
from typing import Literal
from urllib.parse import quote
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
from starlette.background import BackgroundTask
from app.services.downloader import download_media_async

router = APIRouter()


class MediaDownloadRequest(BaseModel):
    url: HttpUrl
    mode: Literal["video", "audio", "thumbnail"] = "video"


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
