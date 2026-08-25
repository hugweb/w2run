"""Configuration and scoring weights for w2run."""
import os
from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
DEM_DIR = CACHE_DIR / "dem"
DEM_DIR.mkdir(exist_ok=True)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# When True, a synthetic "mock" path network is used if Overpass is unreachable
# (handy for offline dev). In production this should be False so users never see
# the fake grid — they get a clear "try again" error instead.
ALLOW_MOCK_FALLBACK = os.environ.get("W2RUN_ALLOW_MOCK", "0") == "1"

# opentopodata public fallback (SRTM 30m). 1 req/s, 100 locations/req.
OPENTOPODATA_URL = "https://api.opentopodata.org/v1/srtm30m"

# ---------------------------------------------------------------------------
# Nature / surface scoring.  1.0 = pristine nature, 0.0 = worst (major road).
# Score is derived from OSM tags. Higher = better for a nature-loving runner.
# ---------------------------------------------------------------------------
HIGHWAY_BASE_SCORE = {
    "path": 0.85,
    "track": 0.80,
    "bridleway": 0.85,
    "footway": 0.55,
    "cycleway": 0.55,
    "pedestrian": 0.55,
    "steps": 0.40,
    "living_street": 0.45,
    "service": 0.40,
    "residential": 0.35,
    "unclassified": 0.35,
    "tertiary": 0.20,
    "secondary": 0.12,
    "primary": 0.06,
    "trunk": 0.03,
    "motorway": 0.0,
}

SURFACE_SCORE = {
    "ground": 1.0, "dirt": 1.0, "earth": 1.0, "grass": 1.0, "mud": 0.9,
    "sand": 0.85, "fine_gravel": 0.9, "gravel": 0.85, "pebblestone": 0.8,
    "wood": 0.8, "unpaved": 0.9, "compacted": 0.8, "woodchips": 0.95,
    "paving_stones": 0.4, "cobblestone": 0.4, "sett": 0.4,
    "asphalt": 0.15, "concrete": 0.1, "paved": 0.2, "metal": 0.1,
}

# Bonus if the way runs through/next to nature (park, forest, water, etc.)
NATURE_CONTEXT_BONUS = 0.15

ROAD_HIGHWAYS = {"tertiary", "secondary", "primary", "trunk", "motorway",
                 "residential", "unclassified", "living_street", "service"}


@dataclass
class ScoreWeights:
    """All configurable. Elevation & nature carry the strongest weight."""
    w_distance: float = 25.0      # distance accuracy vs target
    w_elevation: float = 30.0     # flatness (elevation gain)
    w_nature: float = 25.0        # natural surface percentage
    w_start: float = 12.0         # proximity of start to user
    w_loop: float = 8.0           # loop quality (low overlap)
    # tuning constants
    elev_good_per_km: float = 6.0   # m gain per km considered "good" baseline
    start_good_m: float = 500.0     # start within this = full marks


DEFAULT_WEIGHTS = ScoreWeights()

# Preference presets tweak the routing cost + scoring emphasis.
PREFERENCES = {
    "balanced":    {"elev": 1.0, "nature": 1.0, "road": 1.0, "label": "Best overall"},
    "flattest":    {"elev": 8.0, "nature": 0.5, "road": 0.5, "label": "Flattest"},
    "nature":      {"elev": 0.7, "nature": 2.5, "road": 2.0, "label": "Most nature"},
    "closest":     {"elev": 1.0, "nature": 1.0, "road": 1.0, "label": "Closest"},
    "scenic":      {"elev": 0.5, "nature": 3.0, "road": 2.5, "label": "Scenic / Adventure"},
}
