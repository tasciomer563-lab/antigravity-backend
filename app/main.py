from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes_document import router as document_router
from app.api.routes_media import router as media_router
from app.api.routes_audio import router as audio_router

app = FastAPI(
    title="Öğrenci & Üretkenlik Asistanı API",
    description="VisionKit PDF/Word, yt-dlp Medya/MP3/Kapak ve Whisper AI Sesli Ders/Toplantı Servisi",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Flutter Web'in (tarayıcının) Supabase linkini okuyabilmesi için özel başlıkları açığa çıkar
    expose_headers=["X-Supabase-Url", "X-File-Size-Mb", "Content-Disposition", "Content-Length"],
)

app.include_router(document_router, prefix="/api/document", tags=["Document Hub"])
app.include_router(media_router, prefix="/api/media", tags=["Media Downloader"])
app.include_router(audio_router, prefix="/api/audio", tags=["Whisper Audio Assistant"])


@app.get("/health")
async def health_check():
    return {"status": "ok"}
