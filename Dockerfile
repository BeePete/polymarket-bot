# =============================================================================
#  Polymarket Bot - Dockerfile (multi-stage build)
# =============================================================================
#  Stage 1 (builder) - instaluje zależności
#  Stage 2 (runtime) - kopiuje tylko to co potrzebne, mniejszy obraz
# =============================================================================

# ---------- Stage 1: builder ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Instaluj zależności do osobnego prefixu, żeby skopiować tylko je do runtime
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


# ---------- Stage 2: runtime ----------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOT_CONFIG_PATH=/app/config.yaml \
    BOT_DB_PATH=/app/data/bot_state.db \
    BOT_LOG_FILE=/app/logs/bot.log

# Stwórz nieuprzywilejowanego usera (bezpieczniej niż root)
RUN groupadd --system pmbot && \
    useradd --system --gid pmbot --create-home --home-dir /home/pmbot pmbot

WORKDIR /app

# Skopiuj zainstalowane biblioteki z buildera
COPY --from=builder /install /usr/local

# Skopiuj kod aplikacji
COPY --chown=pmbot:pmbot bot/ ./bot/

# Foldery na dane (config, db, logi) - zostaną zmapowane jako volumes
RUN mkdir -p /app/data /app/logs && \
    chown -R pmbot:pmbot /app

USER pmbot

# Domyślne polecenie - można nadpisać w docker-compose
CMD ["python", "-m", "bot.main"]
