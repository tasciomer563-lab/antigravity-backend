import asyncio
import io
import os
import re
import unicodedata
from typing import Optional
from urllib.parse import quote
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import Response, JSONResponse
from PIL import Image, ImageOps
from app.core.config import get_supabase_client, STORAGE_BUCKET

router = APIRouter()


def _sanitize_storage_filename(name: str) -> str:
    """Supabase Storage path için Türkçe/özel karakterleri güvenli ASCII'ye dönüştürür."""
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", normalized).strip("_")
    return cleaned or "Odev_Belgesi"


def _sync_upload_to_supabase(
    file_bytes: bytes,
    file_name: str,
    file_type: str,
    content_type: str,
) -> Optional[dict]:
    """Oluşturulan PDF/DOCX dosyasını Supabase Storage'a yükler ve document_history tablosuna kaydeder."""
    supabase = get_supabase_client()
    if supabase is None:
        return None

    file_size_mb = round(len(file_bytes) / (1024 * 1024), 2)
    safe_base = _sanitize_storage_filename(file_name)
    storage_path = f"{safe_base}_{os.urandom(4).hex()}.{file_type}"

    # 1. Supabase Storage'a Yükleme
    supabase.storage.from_(STORAGE_BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={"content-type": content_type},
    )

    # 2. Public URL Alma
    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)

    # 3. Veritabanına Log Düşme (document_history)
    supabase.table("document_history").insert(
        {
            "file_name": file_name,
            "file_type": file_type,
            "file_size_mb": file_size_mb,
            "file_url": public_url,
        }
    ).execute()

    return {
        "status": "success",
        "url": public_url,
        "size_mb": file_size_mb,
    }


def _prepare_optimized_images(
    raw_images: list[bytes],
    max_width: int = 1240,
) -> list[Image.Image]:
    """iOS EXIF yönelimini düzeltir, RGB'ye çevirir ve A4 okunabilirliğini koruyarak boyutlandırır."""
    optimized: list[Image.Image] = []
    for content in raw_images:
        img = Image.open(io.BytesIO(content))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")

        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize(
                (max_width, int(img.height * ratio)),
                Image.Resampling.LANCZOS,
            )
        optimized.append(img)
    return optimized


@router.post("/convert-pdf")
async def convert_to_pdf_and_save(
    files: list[UploadFile] = File(...),
    file_name: str = Form("Belge"),
    quality: int = Form(85),
    return_json: bool = Form(False),
):
    raw_contents = [await f.read() for f in files if f.size and f.size > 0]
    if not raw_contents:
        raise HTTPException(status_code=400, detail="Görsel bulunamadı.")

    image_list = _prepare_optimized_images(raw_contents, max_width=1240)
    clamped_quality = max(50, min(95, quality))

    compressed_images: list[Image.Image] = []
    for img in image_list:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=clamped_quality, optimize=True)
        buf.seek(0)
        compressed_images.append(Image.open(buf).convert("RGB"))

    pdf_bytes = io.BytesIO()
    compressed_images[0].save(
        pdf_bytes,
        format="PDF",
        resolution=150.0,
        save_all=True,
        append_images=compressed_images[1:],
        optimize=True,
        quality=clamped_quality,
    )
    raw_pdf = pdf_bytes.getvalue()
    file_size_mb = round(len(raw_pdf) / (1024 * 1024), 2)

    # Supabase Storage + document_history kaydı (hata olsa bile yerel indirmeyi kesmez)
    cloud_info: Optional[dict] = None
    try:
        cloud_info = await asyncio.to_thread(
            _sync_upload_to_supabase,
            raw_pdf,
            file_name,
            "pdf",
            "application/pdf",
        )
    except Exception as exc:
        print(f"[Supabase] PDF bulut kayıt hatası: {exc}")

    if return_json:
        return JSONResponse(
            content={
                "status": "success",
                "url": cloud_info["url"] if cloud_info else None,
                "size_mb": file_size_mb,
            }
        )

    safe_filename = quote(f"{file_name}.pdf")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}",
        "X-File-Size-Mb": str(file_size_mb),
    }
    if cloud_info and cloud_info.get("url"):
        headers["X-Supabase-Url"] = cloud_info["url"]

    return Response(
        content=raw_pdf,
        media_type="application/pdf",
        headers=headers,
    )


@router.post("/convert-docx")
async def convert_to_docx_and_save(
    files: list[UploadFile] = File(...),
    file_name: str = Form("Belge"),
    quality: int = Form(85),
    return_json: bool = Form(False),
):
    """Taranan A4 sayfalarını Word (.docx) belgesine dönüştürür ve Supabase'e yedekler."""
    try:
        from docx import Document
        from docx.shared import Inches
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="Sunucuda python-docx kurulu değil.",
        ) from exc

    raw_contents = [await f.read() for f in files if f.size and f.size > 0]
    if not raw_contents:
        raise HTTPException(status_code=400, detail="Görsel bulunamadı.")

    image_list = _prepare_optimized_images(raw_contents, max_width=1240)
    clamped_quality = max(50, min(95, quality))

    doc = Document()
    for section in doc.sections:
        section.top_margin = Inches(0.4)
        section.bottom_margin = Inches(0.4)
        section.left_margin = Inches(0.4)
        section.right_margin = Inches(0.4)

    for idx, img in enumerate(image_list):
        img_stream = io.BytesIO()
        img.save(img_stream, format="JPEG", quality=clamped_quality, optimize=True)
        img_stream.seek(0)
        doc.add_picture(img_stream, width=Inches(7.4))
        if idx < len(image_list) - 1:
            doc.add_page_break()

    docx_bytes = io.BytesIO()
    doc.save(docx_bytes)
    raw_docx = docx_bytes.getvalue()
    file_size_mb = round(len(raw_docx) / (1024 * 1024), 2)

    docx_mime = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    cloud_info: Optional[dict] = None
    try:
        cloud_info = await asyncio.to_thread(
            _sync_upload_to_supabase,
            raw_docx,
            file_name,
            "docx",
            docx_mime,
        )
    except Exception as exc:
        print(f"[Supabase] DOCX bulut kayıt hatası: {exc}")

    if return_json:
        return JSONResponse(
            content={
                "status": "success",
                "url": cloud_info["url"] if cloud_info else None,
                "size_mb": file_size_mb,
            }
        )

    safe_filename = quote(f"{file_name}.docx")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}",
        "X-File-Size-Mb": str(file_size_mb),
    }
    if cloud_info and cloud_info.get("url"):
        headers["X-Supabase-Url"] = cloud_info["url"]

    return Response(
        content=raw_docx,
        media_type=docx_mime,
        headers=headers,
    )
