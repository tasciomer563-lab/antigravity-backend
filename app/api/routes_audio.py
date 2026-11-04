import asyncio
import os
import re
import tempfile
from fastapi import APIRouter, UploadFile, File, HTTPException
from groq import Groq
import aiofiles
from app.core.config import get_supabase_client

router = APIRouter()


def _get_groq_client() -> Groq:
    # API anahtarını Render'daki Environment Variables'dan otomatik çeker
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="GROQ_API_KEY ortam değişkeni tanımlanmamış.",
        )
    return Groq(api_key=api_key)


def _extract_bullet_summary(transcript: str, max_bullets: int = 6) -> list[str]:
    """Transkript metninden önemli cümleleri maddeler halinde çıkarır."""
    cleaned = re.sub(r"\s+", " ", transcript).strip()
    if not cleaned:
        return []
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", cleaned)
        if len(s.strip()) > 15
    ]
    return sentences[:max_bullets] if sentences else [cleaned]


def _log_transcription_to_supabase_sync(
    file_name: str,
    transcript_text: str,
    summary_bullets: list[str],
) -> None:
    """Başarılı ses deşifresini Supabase audio_transcriptions tablosuna kaydeder."""
    supabase = get_supabase_client()
    if supabase is None:
        return

    try:
        supabase.table("audio_transcriptions").insert(
            {
                "file_name": file_name,
                "transcript_text": transcript_text,
                "summary_bullets": summary_bullets,
                "model_used": "whisper-large-v3",
            }
        ).execute()
    except Exception as exc:
        print(f"[Supabase] audio_transcriptions kayıt hatası: {exc}")


@router.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    temp_dir = tempfile.gettempdir()
    safe_name = os.path.basename(file.filename or "audio.m4a")
    file_location = os.path.join(temp_dir, safe_name)

    async with aiofiles.open(file_location, "wb") as out_file:
        content = await file.read()
        await out_file.write(content)

    try:
        client = _get_groq_client()
        with open(file_location, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                file=(safe_name, audio_file.read()),
                model="whisper-large-v3",
                prompt=(
                    "Bu metin akıcı bir Türkçe ile, noktalama işaretlerine, "
                    "büyük/küçük harf kurallarına ve özel isimlere dikkat edilerek yazılmıştır."
                ),
            )

        if os.path.exists(file_location):
            os.remove(file_location)

        transcript_text = (transcription.text or "").strip()
        summary_bullets = _extract_bullet_summary(transcript_text)

        # Başarılı deşifre işlemini Supabase audio_transcriptions tablosuna kaydet
        await asyncio.to_thread(
            _log_transcription_to_supabase_sync,
            safe_name,
            transcript_text,
            summary_bullets,
        )

        return {
            "text": transcript_text,
            "summary_bullets": summary_bullets,
        }

    except Exception as e:
        if os.path.exists(file_location):
            os.remove(file_location)
        raise HTTPException(status_code=500, detail=str(e))
