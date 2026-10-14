# ==============================================================================
# Production Multi-Stage Dockerfile for Telegram YouTube Downloader
# Designed for Debian 13 VPS (2 CPU cores, 4 GB RAM, 80 GB HDD)
# ==============================================================================

FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TEMP_DIR=/tmp/ytdl

# Install runtime system dependencies: FFmpeg, curl, unzip, ca-certificates, procps, and NodeJS (lightweight JS runtime for yt-dlp challenges)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    procps \
    nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create shared Unix groups:
# GID 1001: ytdl-runtime (shared across Web, Bot, Worker for /config/runtime)
# GID 101:  telegram-bot-api (shared with Local Bot API for /config/bot-api)
RUN groupadd -g 1001 ytdl-runtime \
    && groupadd -g 101 telegram-bot-api \
    && usermod -aG ytdl-runtime,telegram-bot-api root

# Copy application source code
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Create directory hierarchy with proper permissions
RUN mkdir -p /tmp/ytdl /transfer /config/master /config/runtime /config/state /config/bot-api /config/secrets \
    && chmod 755 /transfer /tmp/ytdl

# ==============================================================================
# Target: Web Administration Panel
# ==============================================================================
FROM base AS web
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1
CMD ["uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8080"]

# ==============================================================================
# Target: Telegram Bot
# ==============================================================================
FROM base AS bot
CMD ["python3", "-m", "src.bot.main"]

# ==============================================================================
# Target: Background Worker (yt-dlp, FFmpeg, Deno, AI translation)
# ==============================================================================
FROM base AS worker
CMD ["python3", "-m", "src.worker.main"]
