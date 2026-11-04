# Hafif ve hızlı bir Python 3.10 tabanı
FROM python:3.10-slim

# Sistem paketlerini güncelle ve ffmpeg kur (yt-dlp video+ses birleştirmesi için zorunlu)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

# Çalışma dizinini ayarla
WORKDIR /app

# Gereksinimleri kopyala ve yükle
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodlarını kopyala
COPY ./app ./app

# FastAPI'yi Uvicorn ile başlat (Render/Railway/Fly.io gibi sistemler PORT değişkenini otomatik atar)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
