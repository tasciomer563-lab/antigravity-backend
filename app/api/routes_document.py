import asyncio
import csv
import io
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps

from app.core.config import STORAGE_BUCKET, get_supabase_client

# iPhone fotoğrafları (HEIC/HEIF) için isteğe bağlı destek
try:  # pragma: no cover
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIF_OK = True
except Exception:  # pillow-heif kurulu değilse sessizce devam et
    _HEIF_OK = False

IMAGE_EXTS = ("jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif", "gif", "heic", "heif")

_PDF_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _register_pdf_unicode_font() -> Optional[str]:
    """Türkçe karakterleri koruyan bir TTF font bulup ReportLab'e kaydeder."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        for path in _PDF_FONT_CANDIDATES:
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont("UnicodeSans", path))
                return "UnicodeSans"
    except Exception as exc:
        print(f"[PDF] Unicode font yüklenemedi: {exc}")
    return None

router = APIRouter()

TargetFormat = Literal["pdf", "docx", "xlsx", "pptx", "txt"]

MIME_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain; charset=utf-8",
}


@dataclass
class ParsedContent:
    """Kaynak dosyalardan çıkarılan ortak metin, tablo ve görsel verisi."""

    paragraphs: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    images: list[Image.Image] = field(default_factory=list)


def _sanitize_storage_filename(name: str) -> str:
    normalized = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", normalized).strip("_")
    return cleaned or "Donusturulen_Belge"


def _sync_upload_to_supabase(
    file_bytes: bytes,
    file_name: str,
    file_type: str,
    content_type: str,
) -> Optional[dict]:
    """Dönüştürülen herhangi bir formatı (pdf, docx, xlsx, pptx, txt) Supabase'e yedekler ve loglar."""
    supabase = get_supabase_client()
    if supabase is None:
        return None

    file_size_mb = round(len(file_bytes) / (1024 * 1024), 3)
    safe_base = _sanitize_storage_filename(file_name)
    storage_path = f"{safe_base}_{os.urandom(4).hex()}.{file_type}"

    supabase.storage.from_(STORAGE_BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={"content-type": content_type},
    )

    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)

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


def _parse_input_files(
    items: list[tuple[str, bytes]],
    max_width: int = 1240,
) -> ParsedContent:
    """Yüklenen dosyaların mevcut uzantısını algılayıp içeriklerini (metin, tablo, görsel) ayrıştırır."""
    parsed = ParsedContent()

    for filename, raw_bytes in items:
        ext = os.path.splitext(filename.lower())[1].lstrip(".")

        # 1. Görsel Dosyaları (JPG, PNG, WEBP, HEIC vb.)
        if ext in IMAGE_EXTS:
            img = Image.open(io.BytesIO(raw_bytes))
            img = ImageOps.exif_transpose(img).convert("RGB")
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize(
                    (max_width, int(img.height * ratio)),
                    Image.Resampling.LANCZOS,
                )
            parsed.images.append(img)

        # 2. PDF Dosyası
        elif ext == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw_bytes))
            for idx, page in enumerate(reader.pages):
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    for line in page_text.splitlines():
                        cleaned_line = line.strip()
                        if cleaned_line:
                            parsed.paragraphs.append(cleaned_line)
                            parsed.rows.append([f"Sayfa {idx + 1}", cleaned_line])

        # 3. Word (.docx) Dosyası
        elif ext == "docx":
            from docx import Document

            doc = Document(io.BytesIO(raw_bytes))
            for p in doc.paragraphs:
                txt = p.text.strip()
                if txt:
                    parsed.paragraphs.append(txt)
                    parsed.rows.append([txt])
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parsed.rows.append(cells)
                        parsed.paragraphs.append(" | ".join(cells))

        # 4. Excel (.xlsx) Dosyası
        elif ext in ("xlsx", "xlsm"):
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(raw_bytes), data_only=True)
            for sheet in wb.worksheets:
                parsed.paragraphs.append(f"--- Tablo: {sheet.title} ---")
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(cell) if cell is not None else "" for cell in row]
                    if any(c.strip() for c in cells):
                        parsed.rows.append(cells)
                        parsed.paragraphs.append(" | ".join(c for c in cells if c))

        # 5. PowerPoint (.pptx) Dosyası
        elif ext == "pptx":
            from pptx import Presentation

            prs = Presentation(io.BytesIO(raw_bytes))
            for s_idx, slide in enumerate(prs.slides):
                slide_lines: list[str] = []
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        txt = shape.text_frame.text.strip()
                        if txt:
                            slide_lines.append(txt)
                if slide_lines:
                    combined = " — ".join(slide_lines)
                    parsed.paragraphs.append(f"[Slayt {s_idx + 1}] {combined}")
                    parsed.rows.append([f"Slayt {s_idx + 1}", *slide_lines])

        # 6. CSV Dosyası
        elif ext == "csv":
            decoded = raw_bytes.decode("utf-8", errors="replace")
            reader = csv.reader(io.StringIO(decoded))
            for r in reader:
                if any(r):
                    parsed.rows.append(r)
                    parsed.paragraphs.append(" | ".join(r))

        # 7. Düz Metin (.txt, .md, .json vb.)
        else:
            decoded = raw_bytes.decode("utf-8", errors="replace")
            for line in decoded.splitlines():
                stripped = line.strip()
                if stripped:
                    parsed.paragraphs.append(stripped)
                    # Tab veya noktalı virgül varsa sütunlara ayır
                    if "\t" in stripped:
                        parsed.rows.append(stripped.split("\t"))
                    elif ";" in stripped:
                        parsed.rows.append(stripped.split(";"))
                    else:
                        parsed.rows.append([stripped])

    return parsed


def _build_target_bytes(
    parsed: ParsedContent,
    target_format: TargetFormat,
    quality: int = 85,
) -> bytes:
    """Ayrıştırılmış içeriği istenen hedef formata (PDF, DOCX, XLSX, PPTX, TXT) dönüştürür."""
    clamped_quality = max(50, min(95, quality))

    # --- HEDEF 1: TXT ---
    if target_format == "txt":
        if parsed.paragraphs:
            text_out = "\n\n".join(parsed.paragraphs)
        elif parsed.rows:
            text_out = "\n".join("\t".join(r) for r in parsed.rows)
        else:
            text_out = (
                f"[{len(parsed.images)} adet görsel yüklendi. "
                "Görsellerdeki yazıları çıkarmak için Görselden Metin (OCR) aracını da kullanabilirsiniz.]"
            )
        return text_out.encode("utf-8")

    # --- HEDEF 2: DOCX (Word) ---
    if target_format == "docx":
        from docx import Document
        from docx.shared import Inches

        doc = Document()
        for section in doc.sections:
            section.top_margin = Inches(0.6)
            section.bottom_margin = Inches(0.6)
            section.left_margin = Inches(0.6)
            section.right_margin = Inches(0.6)

        for p_text in parsed.paragraphs:
            doc.add_paragraph(p_text)

        # Çok sütunlu tablo verisi varsa Word tablosu olarak ekle
        multi_col_rows = [r for r in parsed.rows if len(r) > 1]
        if multi_col_rows and not parsed.paragraphs:
            max_cols = max(len(r) for r in multi_col_rows)
            table = doc.add_table(rows=len(multi_col_rows), cols=max_cols)
            for r_idx, r_vals in enumerate(multi_col_rows):
                for c_idx, val in enumerate(r_vals):
                    table.cell(r_idx, c_idx).text = val

        for idx, img in enumerate(parsed.images):
            img_stream = io.BytesIO()
            img.save(img_stream, format="JPEG", quality=clamped_quality, optimize=True)
            img_stream.seek(0)
            doc.add_picture(img_stream, width=Inches(6.8))
            if idx < len(parsed.images) - 1:
                doc.add_page_break()

        out = io.BytesIO()
        doc.save(out)
        return out.getvalue()

    # --- HEDEF 3: XLSX (Excel) ---
    if target_format == "xlsx":
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as XLImage

        wb = Workbook()
        ws = wb.active
        ws.title = "Dönüştürülen Veri"

        if parsed.rows:
            for r in parsed.rows:
                ws.append(r)
        elif parsed.paragraphs:
            for idx, p in enumerate(parsed.paragraphs, start=1):
                ws.append([idx, p])
        else:
            ws.append(["Görsel Sayısı", len(parsed.images)])

        # Görseller varsa Excel çalışma sayfasına göm
        row_anchor = (ws.max_row or 1) + 2
        for img in parsed.images:
            img_stream = io.BytesIO()
            img.save(img_stream, format="PNG")
            img_stream.seek(0)
            xl_img = XLImage(img_stream)
            ws.add_image(xl_img, f"A{row_anchor}")
            row_anchor += 25

        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    # --- HEDEF 4: PPTX (PowerPoint) ---
    if target_format == "pptx":
        from pptx import Presentation
        from pptx.util import Inches, Pt

        prs = Presentation()
        blank_layout = prs.slide_layouts[6]

        # Önce görselleri tam slayt olarak ekle
        for img in parsed.images:
            slide = prs.slides.add_slide(blank_layout)
            img_stream = io.BytesIO()
            img.save(img_stream, format="JPEG", quality=clamped_quality, optimize=True)
            img_stream.seek(0)
            # 10x7.5 inç slayta sığdır (geniş görseller taşmasın)
            max_w, max_h = 9.0, 6.5
            ratio = img.width / img.height if img.height else 1
            pic_w = min(max_w, max_h * ratio)
            pic_h = pic_w / ratio
            slide.shapes.add_picture(
                img_stream,
                Inches((10 - pic_w) / 2),
                Inches((7.5 - pic_h) / 2),
                width=Inches(pic_w),
                height=Inches(pic_h),
            )

        # Metin paragraflarını 6'şar satırlık slaytlar halinde ekle
        chunk_size = 6
        for i in range(0, len(parsed.paragraphs), chunk_size):
            chunk = parsed.paragraphs[i : i + chunk_size]
            slide = prs.slides.add_slide(blank_layout)
            tx_box = slide.shapes.add_textbox(
                Inches(0.7), Inches(0.7), Inches(8.6), Inches(6.0)
            )
            tf = tx_box.text_frame
            tf.word_wrap = True
            for p_idx, text_line in enumerate(chunk):
                p = tf.paragraphs[0] if p_idx == 0 else tf.add_paragraph()
                p.text = f"• {text_line}" if not text_line.startswith("[") else text_line
                p.font.size = Pt(18)

        if not prs.slides:
            prs.slides.add_slide(blank_layout)

        out = io.BytesIO()
        prs.save(out)
        return out.getvalue()

    # --- HEDEF 5: PDF ---
    # Eğer sadece görseller varsa doğrudan Pillow ile çok sayfalı optimize PDF üret
    if parsed.images and not parsed.paragraphs:
        compressed_images: list[Image.Image] = []
        for img in parsed.images:
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
        return pdf_bytes.getvalue()

    # Metin / Tablo içeren dosyalar (DOCX, XLSX, PPTX, TXT -> PDF) için ReportLab ile PDF üret
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader, simpleSplit
    from reportlab.pdfgen import canvas

    pdf_out = io.BytesIO()
    c = canvas.Canvas(pdf_out, pagesize=A4)
    width, height = A4
    y_pos = height - 50

    pdf_font = _register_pdf_unicode_font()
    font_name = pdf_font or "Helvetica"
    c.setFont(font_name, 11)
    lines_to_write = parsed.paragraphs or [" | ".join(r) for r in parsed.rows]

    for paragraph in lines_to_write:
        # ReportLab standart Helvetica'da olmayan Türkçe karakterleri okunaklı eşdeğerleriyle koru
        safe_line = paragraph
        if not pdf_font:
            safe_line = (
                paragraph.replace("ğ", "g")
                .replace("Ğ", "G")
                .replace("ş", "s")
                .replace("Ş", "S")
                .replace("ı", "i")
                .replace("İ", "I")
            )
        wrapped = simpleSplit(safe_line, font_name, 11, width - 80)
        for w_line in wrapped:
            if y_pos < 50:
                c.showPage()
                c.setFont(font_name, 11)
                y_pos = height - 50
            c.drawString(40, y_pos, w_line)
            y_pos -= 16
        y_pos -= 6

    for img in parsed.images:
        c.showPage()
        img_buf = io.BytesIO()
        img.save(img_buf, format="JPEG", quality=clamped_quality)
        img_buf.seek(0)
        c.drawImage(
            ImageReader(img_buf),
            30,
            30,
            width=width - 60,
            height=height - 60,
            preserveAspectRatio=True,
        )

    c.save()
    return pdf_out.getvalue()


@router.post("/convert")
async def convert_universal_document(
    files: list[UploadFile] = File(...),
    target_format: str = Form("pdf"),
    file_name: str = Form("Belge"),
    quality: int = Form(85),
    return_json: bool = Form(False),
):
    """
    Evrensel Dosya Dönüştürücü:
    Yüklenen dosyaların mevcut uzantısını (PDF, DOCX, XLSX, PPTX, TXT, JPG, PNG) algılar
    ve seçilen target_format (pdf, docx, xlsx, pptx, txt) formatına çevirip Supabase'e loglar.
    """
    fmt = target_format.lower().strip().lstrip(".")
    if fmt not in MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Desteklenmeyen hedef format: {fmt}. Desteklenenler: pdf, docx, xlsx, pptx, txt",
        )

    input_items: list[tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        if content:
            input_items.append((f.filename or "dosya.txt", content))

    if not input_items:
        raise HTTPException(status_code=400, detail="Dönüştürülecek dosya bulunamadı.")

    try:
        parsed = await asyncio.to_thread(_parse_input_files, input_items)
        output_bytes = await asyncio.to_thread(
            _build_target_bytes,
            parsed,
            fmt,  # type: ignore[arg-type]
            quality,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Dosya dönüştürme hatası: {exc}",
        ) from exc

    file_size_mb = round(len(output_bytes) / (1024 * 1024), 3)
    mime_type = MIME_TYPES[fmt]

    # Supabase Storage ve document_history tablosuna kaydet
    cloud_info: Optional[dict] = None
    try:
        cloud_info = await asyncio.to_thread(
            _sync_upload_to_supabase,
            output_bytes,
            file_name,
            fmt,
            mime_type,
        )
    except Exception as exc:
        print(f"[Supabase] Evrensel belge bulut kayıt hatası: {exc}")

    if return_json:
        return JSONResponse(
            content={
                "status": "success",
                "target_format": fmt,
                "url": cloud_info["url"] if cloud_info else None,
                "size_mb": file_size_mb,
            }
        )

    safe_filename = quote(f"{file_name}.{fmt}")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}",
        "X-File-Size-Mb": str(file_size_mb),
        "Access-Control-Expose-Headers": "X-Supabase-Url, X-File-Size-Mb, Content-Disposition",
    }
    if cloud_info and cloud_info.get("url"):
        headers["X-Supabase-Url"] = cloud_info["url"]

    return Response(
        content=output_bytes,
        media_type=mime_type,
        headers=headers,
    )
