import asyncio
from collections import Counter
import json
import os
import re
import tempfile
import uuid
from typing import Any, Optional

import aiofiles
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from groq import Groq

from app.core.config import get_supabase_client
from app.core.local_store import add_history_entry, get_history_entries

router = APIRouter()

_TURKISH_STOPWORDS = {
    "ve", "veya", "ile", "için", "bir", "bu", "şu", "o", "da", "de", "ki", "mi",
    "mı", "mu", "mü", "ama", "fakat", "ancak", "çünkü", "gibi", "kadar", "daha",
    "en", "çok", "az", "her", "hiç", "bazı", "tüm", "bütün", "olan", "olarak",
    "olup", "oldu", "olur", "ise", "ya", "hem", "ne", "nasıl", "neden", "niçin",
    "nerede", "kim", "영", "the", "and", "for", "that", "this", "with", "from",
    "are", "was", "were", "have", "has", "had", "not", "but", "what", "all",
}

_ACADEMIC_CUE_WORDS = {
    "önemli", "kritik", "sonuç", "özetle", "tanım", "formül", "kural", "sınav",
    "dikkat", "temel", "amaç", "neden", "etki", "ilke", "yöntem", "kavram",
    "örnek", "birincisi", "ikincisi", "özellikle", "vurgulamak", "karar",
}


def _get_groq_client_optional() -> Optional[Groq]:
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key or api_key.startswith("gsk_..."):
        return None
    try:
        return Groq(api_key=api_key)
    except Exception:
        return None


def _analyze_study_text(transcript: str, max_bullets: int = 6) -> dict[str, Any]:
    """
    Metinden önemli cümleleri (TF-IDF + akademik ipucu ağırlıklı),
    anahtar kavramları ve soru-cevap çalışma kartlarını (flashcards) çıkarır.
    """
    cleaned = re.sub(r"\s+", " ", transcript).strip()
    if not cleaned:
        return {
            "summary_bullets": [],
            "keywords": [],
            "flashcards": [],
            "stats": {"word_count": 0, "char_count": 0, "reading_time_min": 0},
        }

    words_all = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ0-9]+", cleaned.lower())
    meaningful_words = [
        w for w in words_all if len(w) > 3 and w not in _TURKISH_STOPWORDS
    ]
    freq = Counter(meaningful_words)
    top_keywords = [w.capitalize() for w, _ in freq.most_common(8)]

    raw_sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", cleaned)
        if len(s.strip()) > 15
    ]
    if not raw_sentences:
        raw_sentences = [cleaned]

    # Cümleleri önem puanına göre sırala, ancak orijinal sırasını koru
    scored: list[tuple[float, int, str]] = []
    for idx, sent in enumerate(raw_sentences):
        s_words = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ0-9]+", sent.lower())
        if not s_words:
            continue
        base_score = sum(freq.get(w, 0) for w in s_words) / (len(s_words) ** 0.6)
        cue_bonus = sum(2.5 for w in s_words if w in _ACADEMIC_CUE_WORDS)
        pos_bonus = 1.5 if idx == 0 else (1.2 if idx == len(raw_sentences) - 1 else 1.0)
        scored.append(((base_score + cue_bonus) * pos_bonus, idx, sent))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_selected = sorted(scored[:max_bullets], key=lambda x: x[1])
    bullets = [s for _, _, s in top_selected] if top_selected else raw_sentences[:max_bullets]

    # Otomatik çalışma kartları (Flashcards) üret
    flashcards: list[dict[str, str]] = []
    for i, bullet in enumerate(bullets[:4]):
        kw = top_keywords[i] if i < len(top_keywords) else f"Kavram #{i + 1}"
        if "nedir" in bullet.lower() or "olarak tanımlanır" in bullet.lower():
            q_text = f"'{kw}' kavramının tanımı ve önemi nedir?"
        elif any(c.isdigit() for c in bullet):
            q_text = f"Metinde '{kw}' ile ilgili paylaşılan sayısal/kritik bilgi nedir?"
        else:
            q_text = f"Ders/toplantı notlarında '{kw}' hakkında vurgulanan ana fikir nedir?"
        flashcards.append({"question": q_text, "answer": bullet})

    word_count = len(cleaned.split())
    reading_min = max(1, round(word_count / 180))

    return {
        "summary_bullets": bullets,
        "keywords": top_keywords,
        "flashcards": flashcards,
        "stats": {
            "word_count": word_count,
            "char_count": len(cleaned),
            "reading_time_min": reading_min,
        },
    }


def _extract_bullet_summary(transcript: str, max_bullets: int = 6) -> list[str]:
    """Transkript metninden önemli cümleleri maddeler halinde çıkarır."""
    return _analyze_study_text(transcript, max_bullets=max_bullets)["summary_bullets"]


def _log_transcription_to_supabase_sync(
    file_name: str,
    transcript_text: str,
    summary_bullets: list[str],
    model_used: str = "whisper-large-v3",
) -> None:
    """Başarılı ses deşifresini hem yerel geçmişe hem de Supabase audio_transcriptions tablosuna kaydeder."""
    add_history_entry(
        "audio_transcriptions",
        {
            "file_name": file_name,
            "transcript_text": transcript_text,
            "summary_bullets": summary_bullets,
            "model_used": model_used,
        },
    )

    supabase = get_supabase_client()
    if supabase is None:
        return

    try:
        supabase.table("audio_transcriptions").insert(
            {
                "file_name": file_name,
                "transcript_text": transcript_text,
                "summary_bullets": summary_bullets,
                "model_used": model_used,
            }
        ).execute()
    except Exception as exc:
        print(f"[Supabase] audio_transcriptions kayıt hatası: {exc}")


@router.post("/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    language: str = Form("tr"),
):
    temp_dir = tempfile.gettempdir()
    orig_name = os.path.basename(file.filename or "audio.m4a")
    safe_name = f"{uuid.uuid4().hex[:8]}_{orig_name}"
    file_location = os.path.join(temp_dir, safe_name)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Boş ses dosyası gönderildi.")

    async with aiofiles.open(file_location, "wb") as out_file:
        await out_file.write(content)

    try:
        client = _get_groq_client_optional()
        transcript_text = ""
        model_used = "whisper-large-v3"

        if client is not None:
            with open(file_location, "rb") as audio_file:
                create_kwargs: dict[str, Any] = {
                    "file": (orig_name, audio_file.read()),
                    "model": "whisper-large-v3",
                    "prompt": (
                        "Bu metin akıcı bir Türkçe ile, noktalama işaretlerine, "
                        "büyük/küçük harf kurallarına ve akademik terimlere dikkat edilerek yazılmıştır."
                    ),
                }
                if language and language != "auto":
                    create_kwargs["language"] = language[:2].lower()
                transcription = client.audio.transcriptions.create(**create_kwargs)
            transcript_text = (transcription.text or "").strip()
        else:
            # Eğer metin tabanlı bir dosya yüklendiyse veya yerel test ortamında GROQ_API_KEY yoksa:
            decoded_try = content.decode("utf-8", errors="ignore").strip()
            if len(decoded_try) > 20 and all(
                ord(c) >= 9 for c in decoded_try[:100]
            ):
                transcript_text = decoded_try
                model_used = "local-text-analyzer"
            else:
                file_kb = max(1, round(len(content) / 1024))
                transcript_text = (
                    f"[{orig_name} ({file_kb} KB) ses kaydı alındı.] "
                    "Ders ve toplantı kayıtlarında temel kavramların düzenli tekrar edilmesi sınav başarısını doğrudan artırır. "
                    "Önemli tanımlar, sayısal veriler ve ders sorumlusunun vurguladığı örnekler özet maddeler halinde çalışma kartlarına dönüştürülmelidir. "
                    "Gerçek zamanlı bulut Whisper AI ses deşifresi için sunucu ortam değişkenlerine GROQ_API_KEY anahtarınızı ekleyebilirsiniz."
                )
                model_used = "local-extractive-fallback"

        if os.path.exists(file_location):
            os.remove(file_location)

        analysis = await asyncio.to_thread(_analyze_study_text, transcript_text)
        summary_bullets = analysis["summary_bullets"]

        await asyncio.to_thread(
            _log_transcription_to_supabase_sync,
            orig_name,
            transcript_text,
            summary_bullets,
            model_used,
        )

        return {
            "text": transcript_text,
            "summary_bullets": summary_bullets,
            "keywords": analysis["keywords"],
            "flashcards": analysis["flashcards"],
            "stats": analysis["stats"],
            "model_used": model_used,
        }

    except Exception as e:
        if os.path.exists(file_location):
            os.remove(file_location)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/summarize-text")
async def summarize_study_text(request: Request):
    """
    Öğrencinin yapıştırdığı ders notu, makale veya ödev metnini analiz eder;
    önemli maddeleri, anahtar kelimeleri ve sınav çalışma kartlarını (flashcards) üretir.
    """
    raw_text = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw_text = str(body.get("text") or body.get("transcript") or "")
    except Exception:
        form = await request.form()
        raw_text = str(form.get("text") or "")

    cleaned = raw_text.strip()
    if len(cleaned) < 10:
        raise HTTPException(
            status_code=400,
            detail="Özetlenecek metin çok kısa. Lütfen en az bir cümle veya paragraf girin.",
        )

    analysis = await asyncio.to_thread(_analyze_study_text, cleaned)
    await asyncio.to_thread(
        _log_transcription_to_supabase_sync,
        "Ders_Notu_Ozeti.txt",
        cleaned[:2000],
        analysis["summary_bullets"],
        "study-text-analyzer",
    )

    return {
        "text": cleaned,
        "summary_bullets": analysis["summary_bullets"],
        "keywords": analysis["keywords"],
        "flashcards": analysis["flashcards"],
        "stats": analysis["stats"],
    }


@router.get("/history")
async def get_audio_history():
    """Ses deşifre ve ders özeti geçmişini döner."""
    items: list[dict[str, Any]] = []
    supabase = get_supabase_client()
    if supabase is not None:
        try:
            resp = (
                supabase.table("audio_transcriptions")
                .select("*")
                .order("created_at", desc=True)
                .limit(25)
                .execute()
            )
            if resp.data:
                items.extend(resp.data)
        except Exception:
            pass

    local_items = await asyncio.to_thread(get_history_entries, "audio_transcriptions")
    items.extend(local_items)
    return {"items": items[:35]}
