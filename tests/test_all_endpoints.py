import io
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image, ImageDraw
from pptx import Presentation
from pypdf import PdfReader

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert data["ffmpeg"] is True


def test_universal_document_conversions():
    sample_txt = (
        "Lojistik Yönetimi Ders Notları\n"
        "Tedarik zinciri optimizasyonu önemlidir.\n"
        "Stok devir hızı;Maliyet;Hizmet Seviyesi\n"
        "Yüksek;Düşük;%98\n"
    ).encode("utf-8")

    for fmt in ("pdf", "docx", "xlsx", "pptx", "txt", "csv", "md"):
        r = client.post(
            "/api/document/convert",
            files=[("files", ("ders_notu.txt", sample_txt, "text/plain"))],
            data={
                "target_format": fmt,
                "file_name": "Lojistik_Odev",
                "quality": "85",
                "include_cover": "true" if fmt in ("pdf", "docx", "txt", "md") else "false",
                "cover_university": "İSTANBUL ÜNİVERSİTESİ",
                "cover_department": "Ulaştırma ve Lojistik Fakültesi",
                "cover_course": "Tedarik Zinciri Yönetimi",
                "cover_title": "2026 Dönem Projesi",
                "cover_student": "Ömer Taşçı",
                "cover_student_id": "20261001",
                "cover_instructor": "Prof. Dr. Ahmet Yılmaz",
            },
        )
        assert r.status_code == 200, f"Failed for {fmt}: {r.text}"
        assert len(r.content) > 20, f"Empty output for {fmt}"

        if fmt == "pdf":
            reader = PdfReader(io.BytesIO(r.content))
            assert len(reader.pages) >= 2

            r_pdf_to_docx = client.post(
                "/api/document/convert",
                files=[("files", ("test.pdf", r.content, "application/pdf"))],
                data={"target_format": "docx", "file_name": "PdfToWord"},
            )
            assert r_pdf_to_docx.status_code == 200
            doc = Document(io.BytesIO(r_pdf_to_docx.content))
            assert len(doc.paragraphs) > 0

            r_pdf_merge = client.post(
                "/api/document/convert",
                files=[
                    ("files", ("p1.pdf", r.content, "application/pdf")),
                    ("files", ("p2.pdf", r.content, "application/pdf")),
                ],
                data={
                    "target_format": "pdf",
                    "file_name": "Birlestirilmis",
                    "page_range": "1",
                },
            )
            assert r_pdf_merge.status_code == 200
            merged_reader = PdfReader(io.BytesIO(r_pdf_merge.content))
            assert len(merged_reader.pages) == 2

        elif fmt == "xlsx":
            wb = load_workbook(io.BytesIO(r.content))
            assert len(wb.worksheets) >= 1
        elif fmt == "pptx":
            prs = Presentation(io.BytesIO(r.content))
            assert len(prs.slides) >= 1


def test_image_to_documents_and_ocr():
    img = Image.new("RGB", (600, 300), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((40, 100), "ANTIGRAVITY ODEV TEST 12345", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    for fmt in ("pdf", "docx", "pptx"):
        r = client.post(
            "/api/document/convert",
            files=[("files", ("sayfa1.png", png_bytes, "image/png"))],
            data={"target_format": fmt, "file_name": "Gorsel_Odev"},
        )
        assert r.status_code == 200
        assert len(r.content) > 100

    r_ocr = client.post(
        "/api/document/ocr",
        files={"file": ("sayfa1.png", png_bytes, "image/png")},
        data={"lang": "tr"},
    )
    assert r_ocr.status_code == 200, r_ocr.text
    assert r_ocr.json()["status"] == "success"


def test_standalone_cover_page():
    for fmt in ("pdf", "docx"):
        r = client.post(
            "/api/document/cover-page",
            data={
                "university": "ORTA DOĞU TEKNİK ÜNİVERSİTESİ",
                "department": "Makine Mühendisliği Bölümü",
                "course": "ME 301 İmalat Yöntemleri",
                "title": "Bambu Lab 3D Baskı Mukavemet Analizi",
                "student_name": "Ömer Taşçı",
                "student_id": "2401992",
                "instructor": "Doç. Dr. Selim Kaya",
                "target_format": fmt,
            },
        )
        assert r.status_code == 200, r.text
        assert len(r.content) > 500


def test_video_compression_ffmpeg():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        sample_mp4 = tmp.name

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1.2:size=640x360:rate=24",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                sample_mp4,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(sample_mp4, "rb") as fh:
            raw_video = fh.read()

        r = client.post(
            "/api/media/compress",
            files={"file": ("ornek_ders.mp4", raw_video, "video/mp4")},
            data={"quality": "low"},
        )
        assert r.status_code == 200, r.text
        assert len(r.content) > 500
    finally:
        if os.path.exists(sample_mp4):
            os.remove(sample_mp4)


def test_audio_and_text_summarizer():
    lecture_text = (
        "Bugünkü dersimizde tedarik zinciri yönetiminde kamçı etkisi yani bullwhip effect kavramını inceliyoruz. "
        "Önemli bir kural olarak, talep tahminlerindeki küçük sapmalar tedarik zincirinin üst basamaklarına doğru katlanarak büyür. "
        "Sınav için dikkat edilmesi gereken birinci husus bilgi paylaşımının kamçı etkisini azaltmasıdır. "
        "İkincisi ise sipariş partisi büyüklüklerinin küçültülmesi ve fiyat dalgalanmalarının önlenmesidir. "
        "Sonuç olarak gerçek zamanlı stok takibi yapan işletmelerde lojistik maliyetler yüzde 18 oranında düşmektedir."
    )
    r = client.post(
        "/api/audio/summarize-text",
        json={"text": lecture_text},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["summary_bullets"]) >= 2
    assert len(data["keywords"]) >= 3
    assert len(data["flashcards"]) >= 2

    r_tr = client.post(
        "/api/audio/transcribe",
        files={"file": ("ders_kaydi.m4a", b"\x00\x01\x02\x03" * 256, "audio/mp4")},
        data={"language": "tr"},
    )
    assert r_tr.status_code == 200, r_tr.text
    tr_data = r_tr.json()
    assert "text" in tr_data and len(tr_data["summary_bullets"]) > 0


def test_histories():
    r_doc_hist = client.get("/api/document/history")
    assert r_doc_hist.status_code == 200
    items = r_doc_hist.json()["items"]
    assert len(items) > 0

    first_local = items[0].get("local_url")
    if first_local:
        r_file = client.get(first_local)
        assert r_file.status_code == 200
        assert len(r_file.content) > 0

    r_aud_hist = client.get("/api/audio/history")
    assert r_aud_hist.status_code == 200


if __name__ == "__main__":
    test_health()
    test_universal_document_conversions()
    test_image_to_documents_and_ocr()
    test_standalone_cover_page()
    test_video_compression_ffmpeg()
    test_audio_and_text_summarizer()
    test_histories()
    print("ALL TESTS PASSED!")
