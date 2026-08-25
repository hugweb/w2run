# w2run — find your best running loops

Given your current location, w2run finds the **5–10 best ~10 km running loops**
you could run right now, prioritizing **flat terrain**, **natural surfaces**
(trails/forest/parks over asphalt), and a **nearby start point**.

It builds routes from the raw OpenStreetMap path network rather than searching
for pre-existing routes, so it can construct genuinely different alternatives.

---

## Deploy with Docker + Traefik (VPS)

The app ships as a single container serving both API and frontend on port 8000.
The included `docker-compose.yml` wires it to an **existing Traefik** on an
external `traefik` network with HTTPS (required for browser geolocation on
mobile Safari/Chrome).

1. Copy the project to your VPS, e.g. `/home/docker/w2run/`.
2. Edit `docker-compose.yml` and replace **`w2run.CHANGE-ME.com`** (3 places)
   with your domain/subdomain. If you want Traefik to fetch the cert via ACME,
   uncomment the `certresolver` label; otherwise it uses your existing cert.
 3. Build & start:
    ```bash
    docker compose up -d --build
    ```
 4. Open `https://your-domain` on your phone → tap **Find my routes** → allow
    location. (Geolocation only prompts over HTTPS/localhost.)

DEM elevation tiles are **auto-managed**: the app downloads the Copernicus
GLO-30 tile(s) for the current search area on first use and deletes tiles from
previously-searched areas, so disk usage stays at just a few tiles (~40 MB each).
`./scripts/download_dem.sh` is only needed if you want to pre-seed a tile.

Notes:
- `./cache` is bind-mounted, so OSM/elevation caches and DEM tiles persist
  across restarts and rebuilds.
- The container has a `/api/health` healthcheck.
- First request per new area takes ~20–40 s (Overpass + elevation), then cached.

---

## Quick start (local dev)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.app.main:app --reload --port 8000
# open http://127.0.0.1:8000
```

Click **Find my routes**, allow location access, pick a target distance.

> First request for a new area takes ~20–40 s (fetching OSM + elevation).
> Everything is cached to `cache/`, so subsequent requests are fast.

---

## Architecture

| Concern        | Choice                                                            |
|----------------|-------------------------------------------------------------------|
| Backend        | Python + FastAPI (`backend/app`)                                  |
| Routing engine | Custom graph + A* loop generator (`networkx`, `numpy`)           |
| OSM data       | Overpass API (cached per area on disk)                           |
| Elevation      | Local Copernicus GLO-30 DEM via `rasterio` **or** public opentopodata SRTM30m API (fallback) |
| Frontend       | Static HTML/JS, Leaflet map + Chart.js elevation profile (no build step) |
| Map tiles      | OpenTopoMap                                                       |
| Cache / DB     | On-disk JSON in `cache/` (no database needed for the MVP)        |
| Deploy         | A single `uvicorn` process. No serverless required.             |

Serverless was not chosen: route generation is CPU- and latency-heavy and
benefits from a warm process with local OSM/elevation/graph caches. A small
always-on server is simpler and cheaper here.

---

## The route-generation algorithm

1. **Fetch** the walkable path network within `radius ≈ target/6 · 1.35 + 600 m`
   from Overpass (`highway=path/track/footway/...`), respecting `foot`/`access`.
2. **Build a weighted graph.** Each edge stores length, `highway`, `surface`,
   `tracktype`, a derived **nature score (0–1)**, and geometry.
3. **Simplify** degree-2 chains into single edges (fewer nodes → fewer elevation
   lookups), keep the largest connected component.
4. **Cost function** per edge:
   `length · (1 + w_nature·(1−nature) + w_road·road_penalty)` (+ gradient penalty
   when local elevation is available).
5. **Start nodes:** nearest graph nodes to the user, searched with an expanding
   radius `500 m → 1 → 2 → 4 km`.
6. **Loop construction (the core):** for each start, several compass bearings and
   several circumradii, place **three waypoints 120° apart** and route through
   them with **A***, penalizing already-used edges on later legs. This forces a
   **real non-overlapping loop** (a rounded triangle) rather than an out-and-back.
7. **Filter** to `0.6·target … 1.4·target`, dedupe by geometry signature.
8. **Two-phase scoring** (keeps public elevation-API calls bounded): rank
   candidates first without elevation, then fetch elevation only for the top ~16
   and compute full metrics (gain/loss, highest/lowest, profile).
9. **Score** each route (0–100), weighted:
   elevation (flatness) and nature carry the strongest weight; distance accuracy
   is important; start proximity is meaningful but lower; loop quality a bonus.
   Weights live in `backend/app/config.py` (`ScoreWeights`, `PREFERENCES`).
10. **Diverse selection:** return category winners — *Best overall*, *Flattest*,
    *Most nature*, *Closest start*, *Scenic / Adventure* — plus top alternatives.

Scoring rationale (per the brief): `10 km/+20 m` → excellent … `10 km/+300 m` →
poor, using **total elevation gain along the route** (with smoothing to reduce
SRTM noise), not start-to-finish difference.

---

## Elevation data (accuracy)

By default the app uses the **opentopodata public SRTM30m API** (free, no key,
rate-limited to ~1 req/s). It works out of the box but is noisy and slower.

For **accurate, rate-limit-free** elevation, download a free **Copernicus GLO-30**
DEM tile and install `rasterio`:

```bash
.venv/bin/pip install rasterio
./scripts/download_dem.sh 51 0      # floor(lat) floor(lon) covering your area
```

Any `.tif`/`.hgt` DEM files placed in `cache/dem/` are sampled locally and
automatically preferred. Other free sources: Copernicus GLO-30 / SRTM 30m
(global), USGS 3DEP 10m/1m (US only).

---

## Configuration

- **Scoring weights & nature tags:** `backend/app/config.py`
- **Preference presets** (flattest / nature / scenic …): `PREFERENCES` in the same file
- **Target distance:** UI buttons (5/8/10/15/20 km) or `distance_km` query param

`GET /api/routes?lat=..&lon=..&distance_km=10&max_routes=8`

---

## Caching / rate limits

- Overpass responses cached per area (`cache/osm_*.json`).
- Elevation cached on a ~11 m grid (`cache/elevation.json`); duplicate cells are
  requested once.
- opentopodata calls are throttled to respect its 1 req/s / 100-points limit.
- Public Overpass requires a real `User-Agent` (set automatically).

---

## Known limitations / MVP compromises

- **First-call latency** (~20–40 s/new area) from Overpass + elevation fetching.
  Cached afterwards.
- **Elevation as a ranking factor, not a routing cost**, when using the public
  API (per-node elevation would exceed rate limits on dense networks). Install a
  **local DEM** to enable gradient-aware routing (flatter route *shapes*).
- **SRTM noise** can inflate gain by a few tens of metres on flat terrain;
  Copernicus GLO-30 tiles are markedly cleaner.
- **Loop shapes** are rounded triangles; not yet optimized for scenery beyond
  surface tags.
- **Offline / API-down fallback:** if Overpass is unreachable the app serves a
  synthetic demo network (labelled ⚠︎ in the UI) so it always renders.
- Region coverage depends on OSM `surface`/`highway` tagging quality.
