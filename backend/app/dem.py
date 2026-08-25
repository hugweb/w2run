"""Elevation sampling.

Strategy (in priority order):
  1. Local DEM tiles via rasterio (Copernicus GLO-30 or SRTM .hgt/.tif) placed in
     cache/dem/. No rate limits, most accurate. Auto-used if rasterio + tiles present.
  2. opentopodata public SRTM 30m API (batched, cached, 1 req/s). Fallback.
  3. Flat (0 m) as a last resort so the app still runs.

Copernicus GLO-30 tiles (free, no key) can be downloaded from AWS:
  s3://copernicus-dem-30m/  (e.g. Copernicus_DSM_COG_10_N45_00_E006_00_DEM/...tif)
Place any .tif/.hgt DEM files covering your area into cache/dem/ and they'll be
used automatically.
"""
import json
import time
import requests

from . import config

try:
    import rasterio
    _HAS_RASTERIO = True
except Exception:
    _HAS_RASTERIO = False


class _LocalDEM:
    """Opens all DEM rasters in cache/dem and samples them."""
    def __init__(self):
        self.datasets = []
        self.reload()

    def reload(self):
        # close any previously-open datasets before reopening
        for ds in self.datasets:
            try:
                ds.close()
            except Exception:
                pass
        self.datasets = []
        if not _HAS_RASTERIO:
            return
        for f in config.DEM_DIR.glob("*"):
            if f.suffix.lower() in (".tif", ".tiff", ".hgt"):
                try:
                    self.datasets.append(rasterio.open(f))
                except Exception:
                    pass

    def sample(self, lat, lon):
        for ds in self.datasets:
            b = ds.bounds
            if b.left <= lon <= b.right and b.bottom <= lat <= b.top:
                try:
                    val = list(ds.sample([(lon, lat)]))[0][0]
                    if val is not None and val > -1000:
                        return float(val)
                except Exception:
                    continue
        return None


# ---------------------------------------------------------------------------
# Automatic tile management: keep ONLY the Copernicus GLO-30 tiles covering the
# current search area. When the user's location changes, unneeded tiles are
# deleted and the newly-needed ones are downloaded. This bounds disk usage to a
# few tiles (~40 MB each) rather than accumulating tiles for every visited area.
# ---------------------------------------------------------------------------
COPERNICUS_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


def _tile_name(tile_lat, tile_lon):
    """Copernicus tile id for the 1x1 degree cell whose SW corner is
    (tile_lat, tile_lon), both integers."""
    ns = "N" if tile_lat >= 0 else "S"
    ew = "E" if tile_lon >= 0 else "W"
    return (f"Copernicus_DSM_COG_10_{ns}{abs(tile_lat):02d}_00_"
            f"{ew}{abs(tile_lon):03d}_00_DEM")


def _needed_tiles(lat, lon, radius_m):
    """Set of tile ids covering the bounding box of the search area."""
    import math
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 0.1))
    lats = range(math.floor(lat - dlat), math.floor(lat + dlat) + 1)
    lons = range(math.floor(lon - dlon), math.floor(lon + dlon) + 1)
    return {_tile_name(la, lo) for la in lats for lo in lons}


def _download_tile(tile_id):
    """Download one Copernicus tile to cache/dem. Returns True on success."""
    dest = config.DEM_DIR / f"{tile_id}.tif"
    if dest.exists():
        return True
    url = f"{COPERNICUS_BASE}/{tile_id}/{tile_id}.tif"
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            if r.status_code != 200:
                return False  # ocean tiles legitimately don't exist -> API fallback
            tmp = dest.with_suffix(".tif.part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            tmp.rename(dest)
        return True
    except Exception:
        # clean up any partial file
        try:
            dest.with_suffix(".tif.part").unlink(missing_ok=True)
        except Exception:
            pass
        return False


def sync_tiles(lat, lon, radius_m):
    """Ensure exactly the tiles for this area are on disk: download the needed
    ones, delete everything else, then reload. Safe no-op without rasterio."""
    if not _HAS_RASTERIO:
        return
    needed = _needed_tiles(lat, lon, radius_m)

    # delete tiles that are no longer needed (frees disk on location change)
    for f in config.DEM_DIR.glob("*"):
        if f.suffix.lower() in (".tif", ".tiff", ".hgt"):
            if f.stem not in needed:
                try:
                    f.unlink()
                except Exception:
                    pass

    # download any needed tiles we don't have yet
    for tile_id in needed:
        _download_tile(tile_id)

    _local.reload()


_local = _LocalDEM()

_ele_cache = {}
_cache_file = config.CACHE_DIR / "elevation.json"
if _cache_file.exists():
    try:
        _ele_cache = json.loads(_cache_file.read_text())
    except Exception:
        _ele_cache = {}


def _pk(lat, lon):
    # ~30m grid (SRTM resolution) so nearby points share a cached value,
    # drastically reducing API calls for dense path networks.
    return f"{round(lat,4)},{round(lon,4)}"


def _save_cache():
    try:
        _cache_file.write_text(json.dumps(_ele_cache))
    except Exception:
        pass


def _opentopodata_batch(points):
    """points: list of (lat,lon). Returns dict pk->ele. Respects 100/req, 1req/s."""
    out = {}
    for i in range(0, len(points), 100):
        chunk = points[i:i + 100]
        locs = "|".join(f"{la},{lo}" for la, lo in chunk)
        try:
            r = requests.get(config.OPENTOPODATA_URL,
                             params={"locations": locs}, timeout=30)
            if r.status_code == 200:
                for (la, lo), res in zip(chunk, r.json().get("results", [])):
                    e = res.get("elevation")
                    if e is not None:
                        out[_pk(la, lo)] = float(e)
            time.sleep(1.05)  # rate limit
        except Exception:
            break
    return out


def elevations(points):
    """Return list of elevations (meters) for list of (lat,lon).

    Uses local DEM -> cache -> opentopodata -> 0.0.
    """
    result = [None] * len(points)
    need_api = []
    need_api_idx = []

    for idx, (la, lo) in enumerate(points):
        pk = _pk(la, lo)
        if pk in _ele_cache:
            result[idx] = _ele_cache[pk]
            continue
        if _local.datasets:
            v = _local.sample(la, lo)
            if v is not None:
                result[idx] = v
                _ele_cache[pk] = v
                continue
        need_api.append((la, lo))
        need_api_idx.append(idx)

    if need_api:
        # dedupe by grid key so we don't request the same cell twice
        uniq = {}
        for la, lo in need_api:
            uniq.setdefault(_pk(la, lo), (la, lo))
        fetched = _opentopodata_batch(list(uniq.values()))
        for (la, lo), idx in zip(need_api, need_api_idx):
            pk = _pk(la, lo)
            v = fetched.get(pk)
            # Only cache REAL values. A missing/None result (API failure, void,
            # water) must NOT be stored as 0.0 — that poisons the elevation
            # profile with fake cliffs. Leave as None; gaps are filled below.
            if v is not None:
                result[idx] = v
                _ele_cache[pk] = v
            else:
                result[idx] = None
        _save_cache()

    # Fill any remaining None gaps by interpolating from valid neighbours so a
    # single failed sample never produces a false 0 m drop.
    result = _fill_gaps(result)

    return result


def _fill_gaps(vals):
    """Linearly interpolate None gaps; extend ends with nearest valid value.
    If nothing is valid, fall back to 0.0."""
    n = len(vals)
    if n == 0:
        return vals
    valid_idx = [i for i, v in enumerate(vals) if v is not None]
    if not valid_idx:
        return [0.0] * n
    out = list(vals)
    # leading/trailing fill
    first, last = valid_idx[0], valid_idx[-1]
    for i in range(first):
        out[i] = vals[first]
    for i in range(last + 1, n):
        out[i] = vals[last]
    # interior interpolation
    prev = first
    for cur in valid_idx[1:]:
        if cur - prev > 1:
            v0, v1 = vals[prev], vals[cur]
            span = cur - prev
            for k in range(1, span):
                out[prev + k] = v0 + (v1 - v0) * k / span
        prev = cur
    return out


def source_name():
    if _local.datasets:
        return f"local DEM ({len(_local.datasets)} tile(s), auto-managed)"
    if _HAS_RASTERIO:
        return "opentopodata SRTM30m (DEM tile unavailable here)"
    return "opentopodata SRTM30m (install rasterio for local DEM)"
