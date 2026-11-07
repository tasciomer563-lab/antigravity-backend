import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
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
    return {"status": "ok", "build": "2026-11-04-tiktok-notes2"}


# Yerel kullanim: WEB_DIR (Flutter web build klasoru) verilirse arayuz da bu sunucudan sunulur
_web_dir = os.getenv("WEB_DIR", "").strip() or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
if _web_dir and os.path.isdir(_web_dir):
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
