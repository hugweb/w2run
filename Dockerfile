# w2run backend + frontend in one image.
# Uses a Debian-based Python image so rasterio's GDAL wheels install cleanly.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System libs for rasterio / GDAL (local DEM sampling) and curl for healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
# rasterio ships manylinux wheels bundling GDAL, so no apt GDAL needed.
RUN pip install -r requirements.txt && pip install "rasterio>=1.3"

# App code
COPY backend ./backend
COPY frontend ./frontend
COPY scripts ./scripts

# Cache dir (OSM/elevation/DEM). Mounted as a volume in compose so it persists.
RUN mkdir -p cache/dem
VOLUME ["/app/cache"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
