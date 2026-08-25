"""Fetch the walkable path network from OpenStreetMap via Overpass, build a
weighted graph, and provide a synthetic-network fallback when offline.

The graph is a networkx.Graph where:
  - nodes are OSM node ids with attrs: lat, lon, ele (filled later by DEM)
  - edges have attrs: length (m), highway, surface, nature (0..1), road (bool),
    geom (list of (lat,lon) along the segment)
"""
import hashlib
import json
import math
import time

import networkx as nx
import requests

from . import config
from .geo import haversine, destination

WALKABLE_FILTER = (
    '["highway"~"^(path|track|footway|bridleway|cycleway|pedestrian|steps|'
    'living_street|residential|unclassified|service|tertiary|secondary|'
    'primary|trunk|road)$"]'
)


def _cache_key(lat, lon, radius):
    raw = f"{round(lat,4)}_{round(lon,4)}_{int(radius)}"
    return hashlib.md5(raw.encode()).hexdigest()


def _load_cache(key):
    p = config.CACHE_DIR / f"osm_{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _save_cache(key, data):
    (config.CACHE_DIR / f"osm_{key}.json").write_text(json.dumps(data))


HEADERS = {"User-Agent": "w2run/0.1 (running route finder; contact: local)"}


def fetch_osm(lat, lon, radius):
    """Return raw Overpass JSON (cached). None on failure."""
    key = _cache_key(lat, lon, radius)
    cached = _load_cache(key)
    if cached is not None:
        return cached

    query = f"""
    [out:json][timeout:45];
    (
      way{WALKABLE_FILTER}(around:{int(radius)},{lat},{lon});
    );
    (._;>;);
    out body;
    """
    # Try each mirror; on a transient failure move to the next. Endpoints are
    # ordered by reliability. A 200 with valid JSON wins.
    for endpoint in config.OVERPASS_ENDPOINTS:
        try:
            r = requests.post(endpoint, data={"data": query},
                              headers=HEADERS, timeout=60)
            if r.status_code == 200:
                data = r.json()
                if data.get("elements"):
                    _save_cache(key, data)
                    return data
        except Exception:
            continue
    return None


def _nature_score(tags):
    """Compute nature score 0..1 from OSM way tags."""
    hw = tags.get("highway", "")
    base = config.HIGHWAY_BASE_SCORE.get(hw, 0.3)
    surface = tags.get("surface")
    if surface in config.SURFACE_SCORE:
        # blend highway base with explicit surface knowledge
        base = 0.4 * base + 0.6 * config.SURFACE_SCORE[surface]
    # tracktype grades: grade1(paved-ish)..grade5(dirt)
    tt = tags.get("tracktype")
    if tt:
        grade = {"grade1": 0.4, "grade2": 0.6, "grade3": 0.75,
                 "grade4": 0.9, "grade5": 1.0}.get(tt)
        if grade:
            base = 0.5 * base + 0.5 * grade
    return max(0.0, min(1.0, base))


def build_graph(lat, lon, radius):
    """Build weighted graph from OSM. Returns (graph, source_str).
    On Overpass failure returns (mock, "mock") only if mock fallback is enabled,
    otherwise (None, "unavailable")."""
    data = fetch_osm(lat, lon, radius)
    if data is None or not data.get("elements"):
        return _fallback(lat, lon, radius)

    nodes = {}
    ways = []
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif el["type"] == "way":
            ways.append(el)

    G = nx.Graph()
    for wid, (la, lo) in nodes.items():
        G.add_node(wid, lat=la, lon=lo, ele=None)

    for way in ways:
        tags = way.get("tags", {})
        hw = tags.get("highway", "")
        # respect access/foot restrictions
        if tags.get("foot") == "no" or tags.get("access") in ("private", "no"):
            continue
        nat = _nature_score(tags)
        road = hw in config.ROAD_HIGHWAYS
        nds = way.get("nodes", [])
        for a, b in zip(nds[:-1], nds[1:]):
            if a not in nodes or b not in nodes:
                continue
            la1, lo1 = nodes[a]
            la2, lo2 = nodes[b]
            d = haversine(la1, lo1, la2, lo2)
            if d <= 0:
                continue
            if G.has_edge(a, b):
                continue
            G.add_edge(a, b, length=d, highway=hw, surface=tags.get("surface"),
                       nature=nat, road=road, geom=[(la1, lo1), (la2, lo2)])

    # keep only the largest connected component (avoids dead islands)
    if G.number_of_nodes() == 0:
        return _fallback(lat, lon, radius)
    largest = max(nx.connected_components(G), key=len)
    G = G.subgraph(largest).copy()
    if G.number_of_edges() < 20:
        return _fallback(lat, lon, radius)
    G = simplify_graph(G)
    return G, "osm"


def _fallback(lat, lon, radius):
    """Return the synthetic network only when explicitly enabled (dev/offline);
    otherwise signal that OSM data is unavailable so the user gets a real error
    instead of the fake grid."""
    if config.ALLOW_MOCK_FALLBACK:
        return build_mock_graph(lat, lon, radius), "mock"
    return None, "unavailable"


def simplify_graph(G):
    """Contract chains of degree-2 nodes into single edges, preserving geometry,
    length (summed) and averaged nature. Greatly reduces node count so we make
    far fewer elevation-API calls."""
    G = G.copy()
    changed = True
    while changed:
        changed = False
        for n in list(G.nodes()):
            if G.degree(n) == 2:
                nbrs = list(G.neighbors(n))
                if len(nbrs) != 2:
                    continue
                a, b = nbrs
                if a == b or G.has_edge(a, b):
                    continue
                e1 = G[a][n]
                e2 = G[n][b]
                # Merge geometry so the result reads a -> n -> b. Orient each
                # sub-geometry by which end is closest to the shared node n
                # (robust against float mismatches and prior merges).
                g1 = list(e1.get("geom", []))
                g2 = list(e2.get("geom", []))
                nlat, nlon = G.nodes[n]["lat"], G.nodes[n]["lon"]

                def _near_n(pt):
                    return haversine(pt[0], pt[1], nlat, nlon)

                # g1 (edge a-n) must END at n
                if g1 and _near_n(g1[0]) < _near_n(g1[-1]):
                    g1 = list(reversed(g1))
                # g2 (edge n-b) must START at n
                if g2 and _near_n(g2[-1]) < _near_n(g2[0]):
                    g2 = list(reversed(g2))

                if g1 and g2:
                    geom = g1 + g2[1:]
                else:
                    geom = g1 or g2
                length = e1["length"] + e2["length"]
                nat = (e1.get("nature", 0.3) * e1["length"] +
                       e2.get("nature", 0.3) * e2["length"]) / max(length, 1)
                road = e1.get("road") or e2.get("road")
                G.add_edge(a, b, length=length, nature=nat, road=road,
                           highway=e1.get("highway"), surface=e1.get("surface"),
                           geom=geom)
                G.remove_node(n)
                changed = True
    return G


def build_mock_graph(lat, lon, radius):
    """Synthetic grid + a couple of 'nature' zones so the app always works
    offline / for testing. Grid nodes spaced ~150 m."""
    G = nx.Graph()
    spacing = 150.0
    n = max(6, int(radius / spacing))
    node_id = 0
    grid = {}
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            # north offset i, east offset j
            plat, plon = destination(lat, lon, 0, i * spacing)
            plat, plon = destination(plat, plon, 90, j * spacing)
            grid[(i, j)] = node_id
            G.add_node(node_id, lat=plat, lon=plon, ele=None)
            node_id += 1

    # a couple of "forest/park" zones (higher nature) and gentle hills
    def zone_nature(i, j):
        # circular park in NE quadrant
        if (i - n // 2) ** 2 + (j - n // 2) ** 2 < (n * 0.5) ** 2:
            return 0.9
        # riverside strip
        if abs(i + j) < 2:
            return 0.75
        return 0.3

    # synthetic gentle elevation: a low hill so profiles aren't flat-zero
    def synth_ele(i, j):
        return 200.0 + 25.0 * math.exp(-((i) ** 2 + (j) ** 2) / (2 * (n * 0.6) ** 2)) \
            + 3.0 * math.sin(i / 2.0) + 2.0 * math.cos(j / 2.0)

    for (i, j), a in grid.items():
        G.nodes[a]["ele"] = synth_ele(i, j)
        for di, dj in ((0, 1), (1, 0)):
            nb = (i + di, j + dj)
            if nb in grid:
                b = grid[nb]
                la1, lo1 = G.nodes[a]["lat"], G.nodes[a]["lon"]
                la2, lo2 = G.nodes[b]["lat"], G.nodes[b]["lon"]
                d = haversine(la1, lo1, la2, lo2)
                nat = (zone_nature(i, j) + zone_nature(*nb)) / 2
                G.add_edge(a, b, length=d, highway="path" if nat > 0.5 else "residential",
                           surface="ground" if nat > 0.6 else "asphalt",
                           nature=nat, road=nat < 0.5,
                           geom=[(la1, lo1), (la2, lo2)])
    return G
