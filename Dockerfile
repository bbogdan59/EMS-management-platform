# syntax=docker/dockerfile:1

# --- Stage 1: build frontend assets local (Tailwind CSS + vendorizare JS) ---
FROM node:22-alpine AS frontend
WORKDIR /frontend
COPY package.json package-lock.json* ./
RUN npm install
COPY tailwind.config.js ./
COPY app/web/static ./app/web/static
RUN npm run build

# --- Stage 2: runtime Python ---
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY simulator ./simulator
COPY pyproject.toml ./

# Suprascrie asset-urile front-end (necompilate) cu cele compilate in stage 1.
COPY --from=frontend /frontend/app/web/static/css/app.css ./app/web/static/css/app.css
COPY --from=frontend /frontend/app/web/static/js/vendor ./app/web/static/js/vendor

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN useradd --create-home --uid 1000 emsuser && chown -R emsuser:emsuser /app
USER emsuser

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]
