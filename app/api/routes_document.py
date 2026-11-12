import asyncio
import base64
import csv
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageEnhance, ImageOps

from app.core.config import STORAGE_BUCKET, get_supabase_client
from app.core.local_store import (
    get_history_entries,
    get_local_file_path,
    save_local_document,
)

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
    "C:/Windows/Fonts/segoeui.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]

_PDF_BOLD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _register_pdf_unicode_font() -> tuple[str, str]:
    """Türkçe karakterleri koruyan normal ve kalın TTF font bulup ReportLab'e kaydeder."""
    normal_name = "Helvetica"
    bold_name = "Helvetica-Bold"
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        for path in _PDF_FONT_CANDIDATES:
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont("UnicodeSans", path))
                normal_name = "UnicodeSans"
                bold_name = "UnicodeSans"
                break

        for bpath in _PDF_BOLD_FONT_CANDIDATES:
            if os.path.exists(bpath):
                pdfmetrics.registerFont(TTFont("UnicodeSansBold", bpath))
                bold_name = "UnicodeSansBold"
                break
    except Exception as exc:
        print(f"[PDF] Unicode font yüklenemedi: {exc}")
    return normal_name, bold_name


def _safe_pdf_text(text: str, font_name: str) -> str:
    if font_name.startswith("UnicodeSans"):
        return text
    return (
        text.replace("ğ", "g")
        .replace("Ğ", "G")
        .replace("ş", "s")
        .replace("Ş", "S")
        .replace("ı", "i")
        .replace("İ", "I")
    )


router = APIRouter()

TargetFormat = Literal["pdf", "docx", "xlsx", "pptx", "txt", "csv", "md"]

MIME_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}


@dataclass
class CoverPageSpec:
    university: str = ""
    department: str = ""
    course: str = ""
    title: str = ""
    student_name: str = ""
    student_id: str = ""
    instructor: str = ""
    date_str: str = ""

    @property
    def is_active(self) -> bool:
        return bool(
            self.title.strip()
            or self.course.strip()
            or self.student_name.strip()
            or self.university.strip()
        )


@dataclass
class ParsedContent:
    """Kaynak dosyalardan çıkarılan ortak metin, tablo, görsel ve ham PDF verisi."""

    paragraphs: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    images: list[Image.Image] = field(default_factory=list)
    raw_pdfs: list[bytes] = field(default_factory=list)
    has_non_pdf_text_source: bool = False


def _parse_page_range(page_range_str: str, total_pages: int) -> list[int]:
    """'1-3, 5' gibi sayfa aralığı dizgesini 0-tabanlı sayfa indekslerine çevirir."""
    cleaned = (page_range_str or "").strip()
    if not cleaned:
        return list(range(total_pages))

    selected: list[int] = []
    for part in cleaned.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = max(1, int(bounds[0].strip()))
                end = min(total_pages, int(bounds[1].strip()))
                for p in range(start, end + 1):
                    idx = p - 1
                    if 0 <= idx < total_pages and idx not in selected:
                        selected.append(idx)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                idx = p - 1
                if 0 <= idx < total_pages and idx not in selected:
                    selected.append(idx)
            except ValueError:
                continue

    return selected if selected else list(range(total_pages))


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
    """Dönüştürülen herhangi bir formatı Supabase'e yedekler ve loglar."""
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


def _build_cover_page_pdf_bytes(cover: CoverPageSpec) -> bytes:
    """Akademik standartta A4 PDF ödev kapak sayfası üretir."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    normal_font, bold_font = _register_pdf_unicode_font()

    # Dış dekoratif çerçeve
    c.setStrokeColorRGB(0.23, 0.36, 0.86)
    c.setLineWidth(2)
    c.rect(32, 32, width - 64, height - 64)
    c.setLineWidth(0.6)
    c.rect(37, 37, width - 74, height - 74)

    y = height - 105
    uni = (cover.university.strip() or "T.C. ÜNİVERSİTE / OKUL BAŞLIĞI").upper()
    c.setFont(bold_font, 15)
    for line in simpleSplit(_safe_pdf_text(uni, bold_font), bold_font, 15, width - 120):
        c.drawCentredString(width / 2, y, line)
        y -= 22

    if cover.department.strip():
        y -= 4
        c.setFont(normal_font, 13)
        for line in simpleSplit(
            _safe_pdf_text(cover.department.strip().upper(), normal_font),
            normal_font,
            13,
            width - 120,
        ):
            c.drawCentredString(width / 2, y, line)
            y -= 18

    if cover.course.strip():
        y -= 18
        c.setFont(bold_font, 13)
        c.setFillColorRGB(0.23, 0.36, 0.86)
        for line in simpleSplit(
            _safe_pdf_text(f"DERS: {cover.course.strip()}", bold_font),
            bold_font,
            13,
            width - 120,
        ):
            c.drawCentredString(width / 2, y, line)
            y -= 18
        c.setFillColorRGB(0, 0, 0)

    # Orta Ödev Başlığı
    title_y = height * 0.54
    c.setStrokeColorRGB(0.82, 0.85, 0.92)
    c.setLineWidth(1)
    c.line(80, title_y + 42, width - 80, title_y + 42)

    main_title = cover.title.strip() or "ÖDEV / PROJE RAPORU"
    c.setFont(bold_font, 20)
    title_lines = simpleSplit(
        _safe_pdf_text(main_title, bold_font), bold_font, 20, width - 130
    )
    for line in title_lines:
        c.drawCentredString(width / 2, title_y, line)
        title_y -= 28

    c.line(80, title_y - 8, width - 80, title_y - 8)

    # Alt Bilgiler (Öğrenci & Öğretim Üyesi & Tarih)
    info_y = 220
    c.setFont(normal_font, 12)
    details: list[tuple[str, str]] = []
    if cover.student_name.strip():
        details.append(("Hazırlayan Öğrenci", cover.student_name.strip()))
    if cover.student_id.strip():
        details.append(("Öğrenci Numarası", cover.student_id.strip()))
    if cover.instructor.strip():
        details.append(("Ders Sorumlusu / Danışman", cover.instructor.strip()))
    date_val = cover.date_str.strip() or time.strftime("%d.%m.%Y")
    details.append(("Teslim Tarihi", date_val))

    for label, val in details:
        c.setFont(bold_font, 11)
        c.drawString(95, info_y, _safe_pdf_text(f"{label}:", bold_font))
        c.setFont(normal_font, 11)
        c.drawString(265, info_y, _safe_pdf_text(val, normal_font))
        info_y -= 24

    c.showPage()
    c.save()
    return buf.getvalue()


def _parse_input_files(
    items: list[tuple[str, bytes]],
    max_width: int = 1240,
    page_range: str = "",
) -> ParsedContent:
    """Yüklenen dosyaların mevcut uzantısını algılayıp içeriklerini (metin, tablo, görsel, PDF) ayrıştırır."""
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
            from pypdf import PdfReader, PdfWriter

            reader = PdfReader(io.BytesIO(raw_bytes))
            total_pages = len(reader.pages)
            selected_indices = _parse_page_range(page_range, total_pages)

            # Seçilen sayfaları koruyan ham PDF belleği oluştur (PDF birleştirme/ayıklama için)
            writer = PdfWriter()
            for idx in selected_indices:
                page = reader.pages[idx]
                writer.add_page(page)
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    for line in page_text.splitlines():
                        cleaned_line = line.strip()
                        if cleaned_line:
                            parsed.paragraphs.append(cleaned_line)
                            parsed.rows.append([f"Sayfa {idx + 1}", cleaned_line])

            sub_pdf = io.BytesIO()
            writer.write(sub_pdf)
            parsed.raw_pdfs.append(sub_pdf.getvalue())

        # 3. Word (.docx) Dosyası
        elif ext == "docx":
            from docx import Document

            parsed.has_non_pdf_text_source = True
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

            parsed.has_non_pdf_text_source = True
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

            parsed.has_non_pdf_text_source = True
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
            parsed.has_non_pdf_text_source = True
            decoded = raw_bytes.decode("utf-8", errors="replace")
            reader = csv.reader(io.StringIO(decoded))
            for r in reader:
                if any(r):
                    parsed.rows.append(r)
                    parsed.paragraphs.append(" | ".join(r))

        # 7. Düz Metin (.txt, .md, .json vb.)
        else:
            parsed.has_non_pdf_text_source = True
            decoded = raw_bytes.decode("utf-8", errors="replace")
            for line in decoded.splitlines():
                stripped = line.strip()
                if stripped:
                    parsed.paragraphs.append(stripped)
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
    cover: Optional[CoverPageSpec] = None,
) -> bytes:
    """Ayrıştırılmış içeriği istenen hedef formata (PDF, DOCX, XLSX, PPTX, TXT, CSV, MD) dönüştürür."""
    clamped_quality = max(50, min(95, quality))
    has_cover = cover is not None and cover.is_active

    # --- HEDEF 1: TXT ---
    if target_format == "txt":
        header_lines: list[str] = []
        if has_cover and cover:
            header_lines.extend(
                [
                    "==========================================",
                    cover.university or "ÖDEV KAPAK SAYFASI",
                    cover.department,
                    f"Ders: {cover.course}" if cover.course else "",
                    f"Konu: {cover.title}" if cover.title else "",
                    f"Öğrenci: {cover.student_name} ({cover.student_id})".strip(" ()"),
                    f"Danışman: {cover.instructor}" if cover.instructor else "",
                    f"Tarih: {cover.date_str or time.strftime('%d.%m.%Y')}",
                    "==========================================\n",
                ]
            )
            header_lines = [line for line in header_lines if line]

        if parsed.paragraphs:
            body_out = "\n\n".join(parsed.paragraphs)
        elif parsed.rows:
            body_out = "\n".join("\t".join(r) for r in parsed.rows)
        else:
            body_out = (
                f"[{len(parsed.images)} adet görsel yüklendi. "
                "Görsellerdeki yazıları çıkarmak için Görselden Metin (OCR) aracını da kullanabilirsiniz.]"
            )
        prefix = ("\n".join(header_lines) + "\n\n") if header_lines else ""
        return (prefix + body_out).encode("utf-8")

    # --- HEDEF 1B: CSV ---
    if target_format == "csv":
        str_buf = io.StringIO()
        writer = csv.writer(str_buf)
        if parsed.rows:
            for r in parsed.rows:
                writer.writerow(r)
        elif parsed.paragraphs:
            writer.writerow(["Sira", "Icerik"])
            for idx, p in enumerate(parsed.paragraphs, start=1):
                writer.writerow([idx, p])
        else:
            writer.writerow(["Gorsel_Sayisi", len(parsed.images)])
        return ("\ufeff" + str_buf.getvalue()).encode("utf-8")

    # --- HEDEF 1C: MD (Markdown) ---
    if target_format == "md":
        md_lines: list[str] = []
        if has_cover and cover:
            md_lines.append(f"# {cover.title or 'Ödev Raporu'}")
            if cover.university:
                md_lines.append(f"**Kurum:** {cover.university}  ")
            if cover.department:
                md_lines.append(f"**Bölüm:** {cover.department}  ")
            if cover.course:
                md_lines.append(f"**Ders:** {cover.course}  ")
            if cover.student_name:
                md_lines.append(f"**Öğrenci:** {cover.student_name} {cover.student_id}  ")
            if cover.instructor:
                md_lines.append(f"**Danışman:** {cover.instructor}  ")
            md_lines.append("\n---\n")

        if parsed.paragraphs:
            for p in parsed.paragraphs:
                if p.startswith("---") or p.startswith("[Slayt"):
                    md_lines.append(f"## {p.strip('- ')}")
                else:
                    md_lines.append(p)
        elif parsed.rows:
            for idx, r in enumerate(parsed.rows):
                md_lines.append("| " + " | ".join(r) + " |")
                if idx == 0:
                    md_lines.append("| " + " | ".join(["---"] * len(r)) + " |")
        else:
            md_lines.append(f"*({len(parsed.images)} görsel içerir)*")

        return "\n\n".join(md_lines).encode("utf-8")

    # --- HEDEF 2: DOCX (Word) ---
    if target_format == "docx":
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches, Pt

        doc = Document()
        for section in doc.sections:
            section.top_margin = Inches(0.6)
            section.bottom_margin = Inches(0.6)
            section.left_margin = Inches(0.6)
            section.right_margin = Inches(0.6)

        if has_cover and cover:
            for line_txt, pt_size, is_bold in [
                (cover.university.upper() or "T.C. ÜNİVERSİTE / OKUL", 15, True),
                (cover.department.upper(), 12, False),
                (f"DERS: {cover.course}" if cover.course else "", 12, True),
                ("\n\n" + (cover.title or "ÖDEV RAPORU") + "\n\n", 20, True),
                (
                    f"Hazırlayan: {cover.student_name}"
                    if cover.student_name
                    else "",
                    12,
                    False,
                ),
                (
                    f"Öğrenci No: {cover.student_id}"
                    if cover.student_id
                    else "",
                    12,
                    False,
                ),
                (
                    f"Danışman: {cover.instructor}" if cover.instructor else "",
                    12,
                    False,
                ),
                (f"Tarih: {cover.date_str or time.strftime('%d.%m.%Y')}", 11, False),
            ]:
                if not line_txt.strip():
                    continue
                cp = doc.add_paragraph()
                cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = cp.add_run(line_txt)
                run.bold = is_bold
                run.font.size = Pt(pt_size)
            doc.add_page_break()

        for p_text in parsed.paragraphs:
            doc.add_paragraph(p_text)

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

        if has_cover and cover:
            c_slide = prs.slides.add_slide(blank_layout)
            c_box = c_slide.shapes.add_textbox(
                Inches(0.8), Inches(1.2), Inches(8.4), Inches(5.0)
            )
            ctf = c_box.text_frame
            ctf.word_wrap = True
            lines_cover = [
                cover.university,
                cover.department,
                cover.course,
                cover.title or "Sunum Başlığı",
                f"{cover.student_name} {cover.student_id}".strip(),
                cover.instructor,
            ]
            first = True
            for cl in lines_cover:
                if not cl.strip():
                    continue
                p = ctf.paragraphs[0] if first else ctf.add_paragraph()
                first = False
                p.text = cl.strip()
                p.font.size = Pt(24 if cl == cover.title else 16)

        for img in parsed.images:
            slide = prs.slides.add_slide(blank_layout)
            img_stream = io.BytesIO()
            img.save(img_stream, format="JPEG", quality=clamped_quality, optimize=True)
            img_stream.seek(0)
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
    # 5A: Kaynak dosyalar PDF (ve/veya görsel) ise ve metin tabanlı başka dosya yoksa,
    # orijinal PDF sayfalarını bozmadan kayıpsız birleştir!
    if parsed.raw_pdfs and not parsed.has_non_pdf_text_source:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        if has_cover and cover:
            cover_pdf = _build_cover_page_pdf_bytes(cover)
            for p in PdfReader(io.BytesIO(cover_pdf)).pages:
                writer.add_page(p)

        for raw_pdf_bytes in parsed.raw_pdfs:
            for p in PdfReader(io.BytesIO(raw_pdf_bytes)).pages:
                writer.add_page(p)

        if parsed.images:
            img_pdf_buf = io.BytesIO()
            rgb_imgs = [im.convert("RGB") for im in parsed.images]
            rgb_imgs[0].save(
                img_pdf_buf,
                format="PDF",
                resolution=150.0,
                save_all=True,
                append_images=rgb_imgs[1:],
                quality=clamped_quality,
            )
            img_pdf_buf.seek(0)
            for p in PdfReader(img_pdf_buf).pages:
                writer.add_page(p)

        out_pdf = io.BytesIO()
        writer.write(out_pdf)
        return out_pdf.getvalue()

    # 5B: Sadece görseller varsa (ve kapak sayfası yoksa) doğrudan Pillow ile optimize PDF üret
    if parsed.images and not parsed.paragraphs and not has_cover:
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

    # 5C: Metin / Tablo içeren dosyalar (DOCX, XLSX, PPTX, TXT -> PDF) için ReportLab ile PDF üret
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader, simpleSplit
    from reportlab.pdfgen import canvas

    pdf_out = io.BytesIO()
    c = canvas.Canvas(pdf_out, pagesize=A4)
    width, height = A4
    normal_font, _ = _register_pdf_unicode_font()
    font_name = normal_font

    lines_to_write = parsed.paragraphs or [" | ".join(r) for r in parsed.rows]
    if lines_to_write:
        y_pos = height - 50
        c.setFont(font_name, 11)
        for paragraph in lines_to_write:
            safe_line = _safe_pdf_text(paragraph, font_name)
            wrapped = simpleSplit(safe_line, font_name, 11, width - 80)
            for w_line in wrapped:
                if y_pos < 50:
                    c.showPage()
                    c.setFont(font_name, 11)
                    y_pos = height - 50
                c.drawString(40, y_pos, w_line)
                y_pos -= 16
            y_pos -= 6
        if parsed.images:
            c.showPage()

    for idx, img in enumerate(parsed.images):
        if idx > 0:
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
    body_pdf_bytes = pdf_out.getvalue()

    if has_cover and cover:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        cover_bytes = _build_cover_page_pdf_bytes(cover)
        for p in PdfReader(io.BytesIO(cover_bytes)).pages:
            writer.add_page(p)
        for p in PdfReader(io.BytesIO(body_pdf_bytes)).pages:
            writer.add_page(p)
        merged_buf = io.BytesIO()
        writer.write(merged_buf)
        return merged_buf.getvalue()

    return body_pdf_bytes


@router.post("/convert")
async def convert_universal_document(
    files: list[UploadFile] = File(...),
    target_format: str = Form("pdf"),
    file_name: str = Form("Belge"),
    quality: int = Form(85),
    page_range: str = Form(""),
    include_cover: bool = Form(False),
    cover_university: str = Form(""),
    cover_department: str = Form(""),
    cover_course: str = Form(""),
    cover_title: str = Form(""),
    cover_student: str = Form(""),
    cover_student_id: str = Form(""),
    cover_instructor: str = Form(""),
    cover_date: str = Form(""),
    return_json: bool = Form(False),
):
    """
    Evrensel Dosya Dönüştürücü & PDF Stüdyosu:
    Yüklenen dosyaların mevcut uzantısını (PDF, DOCX, XLSX, PPTX, TXT, CSV, JPG, PNG, HEIC) algılar
    ve seçilen target_format (pdf, docx, xlsx, pptx, txt, csv, md) formatına çevirir.
    """
    fmt = target_format.lower().strip().lstrip(".")
    if fmt not in MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Desteklenmeyen hedef format: {fmt}. Desteklenenler: {', '.join(MIME_TYPES.keys())}",
        )

    input_items: list[tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        if content:
            input_items.append((f.filename or "dosya.txt", content))

    if not input_items:
        raise HTTPException(status_code=400, detail="Dönüştürülecek dosya bulunamadı.")

    cover_spec = (
        CoverPageSpec(
            university=cover_university,
            department=cover_department,
            course=cover_course,
            title=cover_title,
            student_name=cover_student,
            student_id=cover_student_id,
            instructor=cover_instructor,
            date_str=cover_date,
        )
        if include_cover
        else None
    )

    try:
        parsed = await asyncio.to_thread(
            _parse_input_files, input_items, 1240, page_range
        )
        output_bytes = await asyncio.to_thread(
            _build_target_bytes,
            parsed,
            fmt,  # type: ignore[arg-type]
            quality,
            cover_spec,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Dosya dönüştürme hatası: {exc}",
        ) from exc

    file_size_mb = round(len(output_bytes) / (1024 * 1024), 3)
    mime_type = MIME_TYPES[fmt]

    # 1. Supabase Storage ve document_history tablosuna kaydet (yapılandırılmışsa)
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

    # 2. Yerel geçmişe ve dosya deposuna her zaman kaydet
    local_entry = await asyncio.to_thread(
        save_local_document,
        output_bytes,
        file_name,
        fmt,
        cloud_info["url"] if cloud_info else None,
    )

    effective_url = (cloud_info["url"] if cloud_info else None) or local_entry.get(
        "local_url"
    )

    if return_json:
        return JSONResponse(
            content={
                "status": "success",
                "target_format": fmt,
                "url": effective_url,
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


@router.post("/cover-page")
async def generate_homework_cover_page(
    university: str = Form("T.C. ÜNİVERSİTE / FAKÜLTE"),
    department: str = Form(""),
    course: str = Form(""),
    title: str = Form("ÖDEV / PROJE RAPORU"),
    student_name: str = Form(""),
    student_id: str = Form(""),
    instructor: str = Form(""),
    date_str: str = Form(""),
    target_format: str = Form("pdf"),
):
    """Öğrenciler için tek tıkla standart A4 Ödev Kapak Sayfası (PDF veya DOCX) üretir."""
    fmt = "docx" if target_format.lower().strip() == "docx" else "pdf"
    spec = CoverPageSpec(
        university=university,
        department=department,
        course=course,
        title=title or "ÖDEV KAPAK SAYFASI",
        student_name=student_name,
        student_id=student_id,
        instructor=instructor,
        date_str=date_str,
    )

    if fmt == "docx":
        out_bytes = await asyncio.to_thread(
            _build_target_bytes, ParsedContent(), "docx", 90, spec
        )
    else:
        out_bytes = await asyncio.to_thread(_build_cover_page_pdf_bytes, spec)

    mime_type = MIME_TYPES[fmt]
    await asyncio.to_thread(
        save_local_document, out_bytes, "Odev_Kapak_Sayfasi", fmt, None
    )
    safe_filename = quote(f"Odev_Kapak_Sayfasi.{fmt}")
    return Response(
        content=out_bytes,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}",
            "X-File-Size-Mb": str(round(len(out_bytes) / (1024 * 1024), 3)),
        },
    )


def _extract_ocr_sync(raw_bytes: bytes, filename: str, lang: str = "tr") -> dict[str, Any]:
    """Görsel veya PDF dosyasından metin çıkarır (Groq Vision -> PDF Text -> Tesseract -> Görüntü Analizi)."""
    ext = os.path.splitext(filename.lower())[1].lstrip(".")

    # 1. PDF ise doğrudan pypdf ile metin çıkar
    if ext == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw_bytes))
        pages_text = [
            (p.extract_text() or "").strip() for p in reader.pages
        ]
        joined = "\n\n".join(t for t in pages_text if t)
        return {
            "text": joined,
            "engine": "pypdf",
            "pages": len(reader.pages),
        }

    # Görseli aç ve normalize et
    img = Image.open(io.BytesIO(raw_bytes))
    info_meta_texts: list[str] = []
    for k, v in (img.info or {}).items():
        if isinstance(v, str) and len(v.strip()) > 3 and k.lower() in (
            "text",
            "description",
            "comment",
            "title",
            "ocr",
        ):
            info_meta_texts.append(v.strip())

    img = ImageOps.exif_transpose(img).convert("RGB")
    width, height = img.size

    # 2. GROQ_API_KEY tanımlıysa Groq Vision modeliyle yüksek doğruluklu OCR yap
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if api_key and not api_key.startswith("gsk_..."):
        try:
            from groq import Groq

            resized = img.copy()
            if resized.width > 1500:
                ratio = 1500 / resized.width
                resized = resized.resize(
                    (1500, int(resized.height * ratio)), Image.Resampling.LANCZOS
                )
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=88)
            b64_img = base64.b64encode(buf.getvalue()).decode("ascii")

            client = Groq(api_key=api_key)
            for model_id in (
                "meta-llama/llama-4-scout-17b-16e-instruct",
                "llama-3.2-11b-vision-preview",
            ):
                try:
                    chat_resp = client.chat.completions.create(
                        model=model_id,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "Bu görseldeki tüm metinleri, başlıkları, maddeleri ve tabloları "
                                            "hiçbir yorum eklemeden olduğu gibi, orijinal dilinde (Türkçe/İngilizce) "
                                            "düz metin olarak çıkar."
                                        ),
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/jpeg;base64,{b64_img}"
                                        },
                                    },
                                ],
                            }
                        ],
                        temperature=0.1,
                        max_tokens=2048,
                    )
                    out_txt = (
                        chat_resp.choices[0].message.content or ""
                    ).strip()
                    if out_txt:
                        return {
                            "text": out_txt,
                            "engine": model_id,
                            "width": width,
                            "height": height,
                        }
                except Exception:
                    continue
        except Exception as exc:
            print(f"[OCR Groq Vision] Uyarı: {exc}")

    # 3. Sistemde tesseract kuruluysa çalıştır
    tesseract_bin = shutil.which("tesseract") or (
        "C:/Program Files/Tesseract-OCR/tesseract.exe"
        if os.path.exists("C:/Program Files/Tesseract-OCR/tesseract.exe")
        else None
    )
    if tesseract_bin:
        try:
            gray = ImageOps.grayscale(img)
            enhanced = ImageEnhance.Contrast(gray).enhance(1.8)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
                enhanced.save(tmp_path, format="PNG")
            try:
                tess_lang = "tur+eng" if lang == "tr" else "eng"
                proc = subprocess.run(
                    [tesseract_bin, tmp_path, "stdout", "-l", tess_lang],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if proc.returncode != 0:
                    proc = subprocess.run(
                        [tesseract_bin, tmp_path, "stdout"],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                txt = (proc.stdout or "").strip()
                if txt:
                    return {
                        "text": txt,
                        "engine": "tesseract",
                        "width": width,
                        "height": height,
                    }
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        except Exception as exc:
            print(f"[OCR Tesseract] Uyarı: {exc}")

    # 4. Bulut OCR Yedek Motoru (OCR.space Engine 2 - Türkçe/İngilizce destekli)
    try:
        import json as _json
        import urllib.parse as _uparse
        import urllib.request as _ureq

        ocr_img = img.copy()
        if ocr_img.width > 1400:
            ratio = 1400 / ocr_img.width
            ocr_img = ocr_img.resize(
                (1400, int(ocr_img.height * ratio)), Image.Resampling.LANCZOS
            )
        obuf = io.BytesIO()
        ocr_img.save(obuf, format="JPEG", quality=85)
        b64_data = "data:image/jpeg;base64," + base64.b64encode(
            obuf.getvalue()
        ).decode("ascii")
        ocr_key = os.getenv("OCR_SPACE_API_KEY", "helloworld").strip() or "helloworld"
        form_payload = _uparse.urlencode(
            {
                "apikey": ocr_key,
                "base64Image": b64_data,
                "language": "tur" if lang == "tr" else "eng",
                "OCREngine": "2",
                "scale": "true",
            }
        ).encode("utf-8")
        req = _ureq.Request(
            "https://api.ocr.space/parse/image",
            data=form_payload,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with _ureq.urlopen(req, timeout=15) as resp:
            ocr_data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
        parsed_list = ocr_data.get("ParsedResults") or []
        if parsed_list:
            parsed_text = (parsed_list[0].get("ParsedText") or "").strip()
            if parsed_text:
                return {
                    "text": parsed_text,
                    "engine": "cloud-ocr-v2",
                    "width": width,
                    "height": height,
                }
    except Exception as exc:
        print(f"[OCR Cloud Fallback] Uyarı: {exc}")

    # 5. Gömülü PNG/EXIF metni varsa döndür
    if info_meta_texts:
        return {
            "text": "\n".join(info_meta_texts),
            "engine": "metadata",
            "width": width,
            "height": height,
        }

    return {
        "text": "",
        "engine": "fallback",
        "width": width,
        "height": height,
        "hint": (
            "Web/Masaüstü bulut OCR için sunucuda GROQ_API_KEY veya Tesseract tanımlanabilir; "
            "mobil uygulamada (iOS/Android) Google ML Kit çevrimdışı OCR otomatik devreye girer."
        ),
    }


@router.post("/ocr")
async def recognize_text_from_image(
    file: UploadFile = File(...),
    lang: str = Form("tr"),
):
    """Yüklenen görsel veya PDF dosyasından metin (OCR) çıkarır."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Boş görsel dosyası gönderildi.")

    t0 = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            _extract_ocr_sync, raw, file.filename or "image.jpg", lang
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Görsel okunamadı: {exc}"
        ) from exc

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    text_out = result.get("text", "")
    words = len(text_out.split()) if text_out else 0
    return {
        "status": "success",
        "text": text_out,
        "word_count": words,
        "char_count": len(text_out),
        "engine": result.get("engine", "ocr"),
        "execution_ms": elapsed_ms,
        "width": result.get("width"),
        "height": result.get("height"),
        "hint": result.get("hint"),
    }


@router.get("/history")
async def get_document_history():
    """Hem Supabase (varsa) hem de yerel dönüştürme geçmişini birleştirerek döner."""
    items: list[dict[str, Any]] = []
    supabase = get_supabase_client()
    if supabase is not None:
        try:
            resp = (
                supabase.table("document_history")
                .select("*")
                .order("created_at", desc=True)
                .limit(30)
                .execute()
            )
            if resp.data:
                items.extend(resp.data)
        except Exception as exc:
            print(f"[Supabase] document_history okuma uyarısı: {exc}")

    local_items = await asyncio.to_thread(get_history_entries, "document_history")
    existing_urls = {it.get("file_url") for it in items if it.get("file_url")}
    for loc in local_items:
        if loc.get("file_url") not in existing_urls:
            items.append(loc)

    return {"items": items[:40]}


@router.get("/files/{stored_filename}")
async def serve_converted_file(stored_filename: str):
    """Yerel depodaki dönüştürülmüş belgeyi indirir veya görüntüler."""
    path = get_local_file_path(stored_filename)
    if not path:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı veya süresi doldu.")
    ext = os.path.splitext(stored_filename)[1].lstrip(".").lower()
    mime = MIME_TYPES.get(ext, "application/octet-stream")
    return FileResponse(path=path, media_type=mime, filename=stored_filename)
