import os
import tempfile
from fastapi import APIRouter, UploadFile, File, HTTPException
from groq import Groq
import aiofiles

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

        return {"text": transcription.text}

    except Exception as e:
        if os.path.exists(file_location):
            os.remove(file_location)
        raise HTTPException(status_code=500, detail=str(e))
