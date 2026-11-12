# Hafif ve hızlı bir Python 3.10 tabanı
FROM python:3.10-slim

# Sistem paketlerini güncelle, ffmpeg, Türkçe font ve Tesseract OCR kur
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tesseract-ocr tesseract-ocr-tur && \
    rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp'nin YouTube 'n challenge' (JS) cozumu icin gerekli; varsayilan olarak otomatik kullanilir
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Çalışma dizinini ayarla
WORKDIR /app

# Gereksinimleri kopyala ve yükle
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodlarını ve derlenmiş Flutter Web arayüzünü kopyala
COPY ./app ./app
COPY ./web ./web

# FastAPI'yi Uvicorn ile başlat (Render/Railway/Fly.io gibi sistemler PORT değişkenini otomatik atar)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
