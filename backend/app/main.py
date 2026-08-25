"""FastAPI app for w2run."""
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routing import generate_routes

app = FastAPI(title="w2run")

FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend"


@app.get("/api/routes")
def routes(
    lat: float = Query(...),
    lon: float = Query(...),
    distance_km: float = Query(10.0, ge=1, le=42),
    max_routes: int = Query(8, ge=1, le=12),
):
    """Generate ranked running loops around (lat, lon)."""
    result = generate_routes(lat, lon, distance_km * 1000.0,
                             max_routes=max_routes)
    return result


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND)), name="static")
