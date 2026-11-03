import asyncio
import os
import re
import tempfile
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException

router = APIRouter()

_whisper_model = None


def _get_whisper_model(model_size: str = "base"):
    """Whisper modelini ilk çağrıda belleğe yükler (Lazy Loading)."""
    global _whisper_model
    if _whisper_model is None:
        import whisper

        _whisper_model = whisper.load_model(model_size)
    return _whisper_model


def _extract_bullet_summary(transcript: str, max_bullets: int = 6) -> list[str]:
    """Transkript metninden amfi dersi ve teknik toplantılar için anahtar maddeleri çıkarır."""
    cleaned = re.sub(r"\s+", " ", transcript).strip()
    if not cleaned:
        return ["Ses kaydında konuşma tespit edilemedi."]

    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", cleaned)
        if len(s.strip()) > 20
    ]

    if not sentences:
        return [cleaned]

    # Önemli bağlaç veya vurgu içeren cümlelere öncelik ver
    priority_keywords = (
        "önemli",
        "sonuç",
        "özetle",
        "karar",
        "aksiyon",
        "ödev",
        "sınav",
        "haftaya",
        "projede",
        "lojistik",
        "maliyet",
        "teslim",
        "gerekiyor",
        "yapılacak",
    )

    scored: list[tuple[int, int, str]] = []
    for idx, sentence in enumerate(sentences):
        lower_s = sentence.lower()
        score = sum(2 for kw in priority_keywords if kw in lower_s)
        # İlk ve son cümleler genelde bağlam/sonuç içerir
        if idx == 0 or idx == len(sentences) - 1:
            score += 1
        scored.append((score, idx, sentence))

    # En yüksek skorlu cümleleri seçip konuşma sırasına göre diz
    top_selected = sorted(scored, key=lambda x: (-x[0], x[1]))[:max_bullets]
    ordered_bullets = [item[2] for item in sorted(top_selected, key=lambda x: x[1])]
    return ordered_bullets


def _transcribe_audio_sync(file_path: str, language: Optional[str] = "tr") -> dict:
    model = _get_whisper_model("base")
    result = model.transcribe(
        file_path,
        language=language if language else None,
        fp16=False,
    )
    text = (result.get("text") or "").strip()
    bullets = _extract_bullet_summary(text)
    return {
        "transcript": text,
        "summary_bullets": bullets,
        "language": result.get("language", language or "tr"),
    }


@router.post("/transcribe")
async def transcribe_and_summarize_audio(
    file: UploadFile = File(...),
    language: str = Form("tr"),
):
    """Ses kaydını Whisper AI ile metne dönüştürür ve maddeler halinde ders/toplantı özeti çıkarır."""
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Boş ses dosyası gönderildi.")

    suffix = os.path.splitext(file.filename or "audio.m4a")[1] or ".m4a"
    fd, temp_audio_path = tempfile.mkstemp(prefix="whisper_audio_", suffix=suffix)
    os.close(fd)

    try:
        with open(temp_audio_path, "wb") as f:
            f.write(raw_bytes)

        data = await asyncio.to_thread(_transcribe_audio_sync, temp_audio_path, language)
        return {
            "status": "success",
            "transcript": data["transcript"],
            "summary_bullets": data["summary_bullets"],
            "language": data["language"],
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Whisper ses işleme hatası: {exc}",
        ) from exc
    finally:
        if os.path.exists(temp_audio_path):
            try:
                os.remove(temp_audio_path)
            except OSError:
                pass
