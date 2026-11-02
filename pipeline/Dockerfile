# Pipeline scraper + analisis sentimen (headless; CPU, atau GPU lewat docker-compose.gpu.yml).
# Catatan: verifikator Qwen memakai MLX (khusus Apple Silicon) sehingga di container
# tahap tersebut otomatis dilewati; IndoBERT, CLIP, EasyOCR & Whisper tetap berjalan.
FROM python:3.12-slim-bookworm

# HOME=/data/home: semua cache model (Hugging Face, EasyOCR, Argos Translate) tersimpan di volume
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Jakarta \
    HOME=/data/home \
    LOG_DIR=/app/logs \
    SESSION_DIR=/app/sessions \
    DATA_DIR=/app/data

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# PyTorch: default versi CPU (jauh lebih kecil). Server GPU: docker-compose.gpu.yml mengganti
# TORCH_INDEX_URL ke versi CUDA; kode otomatis memakai GPU bila tersedia (torch.cuda.is_available()).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --index-url "$TORCH_INDEX_URL" torch torchvision

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .
RUN chmod +x run_pipeline.sh docker/scheduler.sh \
    && mkdir -p /data/home logs sessions data

VOLUME ["/data/home", "/app/logs", "/app/sessions", "/app/data"]

ENTRYPOINT ["/app/docker/scheduler.sh"]
