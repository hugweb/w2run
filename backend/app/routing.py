"""Loop route generation.

Algorithm:
  1. Build weighted graph (osm.py).
  2. Assign edge costs from a preference (flat / nature / road weights).
  3. Pick start nodes near the user (expanding radius).
  4. For each start + a set of compass bearings, find a far waypoint node,
     route out (min cost), then route back penalizing reused edges -> a loop.
  5. Measure elevation/nature, score, dedupe, return diverse top-N.
"""
import math

import networkx as nx
import numpy as np

from . import osm, dem, scoring
from .config import PREFERENCES, DEFAULT_WEIGHTS
from .geo import haversine, destination, bearing


class _NodeIndex:
    """Fast approximate nearest-node lookup using numpy on a lat/lon array."""
    def __init__(self, G):
        self.ids = list(G.nodes())
        self.lat = np.array([G.nodes[n]["lat"] for n in self.ids])
        self.lon = np.array([G.nodes[n]["lon"] for n in self.ids])
        self.coslat = math.cos(math.radians(float(self.lat.mean())))

    def nearest(self, lat, lon, max_m=None):
        dlat = (self.lat - lat)
        dlon = (self.lon - lon) * self.coslat
        d2 = dlat * dlat + dlon * dlon
        i = int(np.argmin(d2))
        if max_m is not None:
            # approx meters: 1 deg ~ 111320 m
            dist_m = math.sqrt(d2[i]) * 111320.0
            if dist_m > max_m:
                return None
        return self.ids[i]

    def within(self, lat, lon, radius_m, k=6):
        dlat = (self.lat - lat)
        dlon = (self.lon - lon) * self.coslat
        d2 = dlat * dlat + dlon * dlon
        dist_m = np.sqrt(d2) * 111320.0
        idx = np.argsort(dist_m)[: k * 3]
        out = [(float(dist_m[j]), self.ids[j]) for j in idx
               if dist_m[j] <= radius_m]
        return out[:k]


def _edge_cost(data, pref):
    """Base cost = length scaled by flatness + nature + road penalties.
    Elevation grade added later once ele known; here we use nature/road."""
    length = data["length"]
    nat = data.get("nature", 0.3)
    penalty = 1.0
    penalty += pref["nature"] * (1.0 - nat) * 0.8
    if data.get("road"):
        penalty += pref["road"] * 0.6
    return length * penalty


def _apply_costs(G, pref):
    for _, _, d in G.edges(data=True):
        d["cost"] = _edge_cost(d, pref)


def _apply_elevation_cost(G, pref):
    """Add a climb penalty to each edge cost. Uses the edge's full geometry
    elevation (sampled from the local DEM) so climbs *within* a simplified edge
    are counted. The penalty is expressed as extra 'virtual metres' per metre of
    the edge, and CAPPED, so steep edges are strongly discouraged but never made
    effectively infinite (which would collapse loops to tiny flat fragments)."""
    for u, v, d in G.edges(data=True):
        gain = d.get("edge_gain")
        if gain is None:
            e1 = G.nodes[u].get("ele")
            e2 = G.nodes[v].get("ele")
            if e1 is None or e2 is None:
                continue
            gain = abs(e2 - e1)
        length = max(d["length"], 1.0)
        # grade as a fraction (m climbed per m travelled)
        grade = gain / length
        # penalty scales with grade (not absolute gain) so long flat edges aren't
        # punished; capped at ~pref*length so it stays comparable to distance.
        grade_pen = min(pref["elev"] * grade, pref["elev"] * 0.8) * length * 2.0
        d["cost"] = d["cost"] + grade_pen


def _path_edges(path):
    return list(zip(path[:-1], path[1:]))


def _make_heuristic(G):
    coslat = math.cos(math.radians(G.nodes[next(iter(G.nodes))]["lat"]))

    def h(a, b):
        la1, lo1 = G.nodes[a]["lat"], G.nodes[a]["lon"]
        la2, lo2 = G.nodes[b]["lat"], G.nodes[b]["lon"]
        dlat = (la2 - la1) * 111320.0
        dlon = (lo2 - lo1) * 111320.0 * coslat
        return math.sqrt(dlat * dlat + dlon * dlon)  # meters, admissible lower bound
    return h


def _build_loop(G, waypoints, heuristic=None, penalty_factor=4.0):
    """Route through ordered waypoints with A* (fast), penalizing already-used
    edges on later legs to encourage a true non-overlapping loop."""
    full = []
    used = {}
    modified = []
    ok = True
    legs = list(zip(waypoints[:-1], waypoints[1:]))
    try:
        for src, dst in legs:
            try:
                if heuristic is not None:
                    seg = nx.astar_path(G, src, dst, heuristic=heuristic,
                                        weight="cost")
                else:
                    seg = nx.shortest_path(G, src, dst, weight="cost")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                ok = False
                break
            if full:
                full.extend(seg[1:])
            else:
                full.extend(seg)
            # penalize these edges for later legs
            for a, b in _path_edges(seg):
                if not G.has_edge(a, b):
                    continue
                if (a, b) not in used:
                    used[(a, b)] = G[a][b]["cost"]
                    modified.append((a, b))
                    G[a][b]["cost"] *= penalty_factor
    finally:
        for a, b in modified:
            G[a][b]["cost"] = used[(a, b)]
    if not ok or len(full) < 4:
        return None
    return full


def _loop_geometry(G, path):
    coords = []
    natures = []
    coord_eles = []
    for a, b in _path_edges(path):
        d = G[a][b]
        alat, alon = G.nodes[a]["lat"], G.nodes[a]["lon"]
        blat, blon = G.nodes[b]["lat"], G.nodes[b]["lon"]
        geom = list(d.get("geom", [(alat, alon), (blat, blon)]))
        # Orient the edge geometry to match travel direction a -> b. Simplified
        # edges may store geom in either order; using it backwards would draw a
        # straight jump across the map and inflate elevation gain.
        if geom:
            d_start = haversine(geom[0][0], geom[0][1], alat, alon)
            d_end = haversine(geom[-1][0], geom[-1][1], alat, alon)
            if d_end < d_start:
                geom = list(reversed(geom))
        ea = G.nodes[a].get("ele")
        eb = G.nodes[b].get("ele")
        if not coords:
            coords.append(geom[0])
            coord_eles.append(ea)
        # interpolate elevation along intermediate geom points
        npts = len(geom) - 1
        for k, p in enumerate(geom[1:], 1):
            coords.append(p)
            if ea is not None and eb is not None:
                coord_eles.append(ea + (eb - ea) * k / npts)
            else:
                coord_eles.append(None)
        natures.append(d.get("nature", 0.3))
    return coords, natures, coord_eles


def _overlap_ratio(path):
    edges = [frozenset((a, b)) for a, b in _path_edges(path)]
    if not edges:
        return 1.0
    unique = len(set(edges))
    return 1.0 - unique / len(edges)


def _fill_elevations(G):
    pts = []
    node_list = []
    for n, d in G.nodes(data=True):
        if d.get("ele") is None:
            pts.append((d["lat"], d["lon"]))
            node_list.append(n)
    if pts:
        eles = dem.elevations(pts)
        for n, e in zip(node_list, eles):
            G.nodes[n]["ele"] = e


def _fill_edge_gains(G):
    """With a local DEM, sample elevation along each edge's geometry and store
    the real cumulative gain, so the routing cost sees climbs *inside* long
    simplified edges (endpoint difference alone misses up-and-over hills)."""
    if not dem._local.datasets:
        return
    for u, v, d in G.edges(data=True):
        geom = d.get("geom")
        if not geom or len(geom) < 3:
            e1 = G.nodes[u].get("ele")
            e2 = G.nodes[v].get("ele")
            d["edge_gain"] = abs(e2 - e1) if (e1 is not None and e2 is not None) else 0.0
            continue
        eles = [dem._local.sample(p[0], p[1]) for p in geom]
        eles = [e for e in eles if e is not None]
        gain = 0.0
        for a, b in zip(eles[:-1], eles[1:]):
            if b - a > 1.0:
                gain += b - a
        d["edge_gain"] = gain


def _coord_elevations(coords, precomputed=None):
    """Use precomputed (from graph) where available; DEM only for gaps."""
    if precomputed and all(e is not None for e in precomputed):
        return precomputed
    if not precomputed:
        return dem.elevations(coords)
    missing = [i for i, e in enumerate(precomputed) if e is None]
    if not missing:
        return precomputed
    fetched = dem.elevations([coords[i] for i in missing])
    out = list(precomputed)
    for i, e in zip(missing, fetched):
        out[i] = e
    return out


def generate_routes(lat, lon, target_m, preferences=None, max_routes=8):
    """Main entry. Returns dict with routes list + meta."""
    if preferences is None:
        preferences = list(PREFERENCES.keys())

    radius = target_m / 6.3 * 1.35 + 600  # cover triangular loop + buffer
    G, source = osm.build_graph(lat, lon, radius)

    # Elevation strategy:
    #  - With a LOCAL DEM (rasterio tiles) sampling is instant, so we fill all
    #    node elevations and use gradient in the routing cost.
    #  - Otherwise (public elevation API, rate-limited) we skip per-node fill and
    #    sample elevation only for the final candidate loops. Elevation then acts
    #    as a strong RANKING factor rather than a routing cost. (documented MVP
    #    compromise; install rasterio + local DEM tiles to enable elev routing.)
    have_local_dem = bool(dem._local.datasets)
    if source == "mock" or have_local_dem:
        _fill_elevations(G)
    if have_local_dem:
        _fill_edge_gains(G)

    nidx = _NodeIndex(G)
    heur = _make_heuristic(G)

    # expanding start search
    start_nodes = []
    for r in (500, 1000, 2000, 4000):
        start_nodes = nidx.within(lat, lon, r, k=6)
        if start_nodes:
            break
    if not start_nodes:
        return {"routes": [], "source": source, "error": "No path network found nearby"}

    # triangular-loop circumradius so perimeter ~ target distance.
    # equilateral triangle perimeter = 3*sqrt(3)*R ~= 5.196*R, but real paths
    # add detours, so use a larger divisor to avoid overshooting the target.
    # Generation uses a small set of cost profiles (balanced + nature-leaning)
    # to keep routing calls bounded; the full preference set is applied during
    # SELECTION (category winners) so users still get diverse alternatives.
    gen_prefs = [p for p in ("balanced", "nature", "flattest")
                 if p in preferences]
    if not gen_prefs:
        gen_prefs = ["balanced"]

    tri_radii = [target_m / f for f in (5.0, 6.5, 8.0, 10.0, 13.0)]
    base_bearings = [0, 45, 90, 135, 180, 225, 270, 315]

    candidates = []
    seen_hashes = set()
    diag = {"loops_built": 0, "dist_samples": []}

    def try_generate(radii):
        for pref_key in gen_prefs:
            pref = PREFERENCES[pref_key]
            _apply_costs(G, pref)
            if have_local_dem or source == "mock":
                _apply_elevation_cost(G, pref)

            for start_dist, start in start_nodes[:2]:
                slat = G.nodes[start]["lat"]
                slon = G.nodes[start]["lon"]
                for base in base_bearings:
                  for tri_radius in radii:
                    # three waypoints 120 deg apart -> rounded triangular loop.
                    # In sparse networks a waypoint may not snap; skip just that
                    # vertex rather than abandoning the whole loop (a 2-point
                    # loop is still a valid out-and-different-back circuit).
                    wps = [start]
                    for off in (0, 120, 240):
                        wlat, wlon = destination(slat, slon, base + off, tri_radius)
                        # generous snap radius so sparse areas still find nodes
                        wp = nidx.nearest(wlat, wlon, max_m=tri_radius * 1.1)
                        if wp is not None and wp not in wps:
                            wps.append(wp)
                    if len(wps) < 3:      # need start + >=2 waypoints for a loop
                        continue
                    wps.append(start)  # close the loop
                    path = _build_loop(G, wps, heuristic=heur)
                    if not path or len(path) < 4:
                        continue
                    coords, natures, pre_eles = _loop_geometry(G, path)
                    if len(coords) < 4:
                        continue
                    # multi-point geometry signature for dedupe
                    sig_pts = [coords[int(len(coords)*f)] for f in (0.2, 0.4, 0.6, 0.8)]
                    h = tuple((round(p[0], 3), round(p[1], 3)) for p in sig_pts)
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)

                    # preliminary metrics WITHOUT elevation (fast)
                    m = scoring.compute_metrics(coords, natures, pre_eles or [],
                                                start_dist, target_m,
                                                skip_elevation=True)
                    diag["loops_built"] += 1
                    diag["dist_samples"].append(m["distance_m"])
                    if not (0.6 * target_m <= m["distance_m"] <= 1.4 * target_m):
                        continue
                    ov = _overlap_ratio(path)
                    prelim, _ = scoring.score_route(m, ov, target_m,
                                                    elevation_known=False)
                    candidates.append({
                        "coords_ll": coords,
                        "natures": natures,
                        "pre_eles": pre_eles,
                        "metrics": m,
                        "overlap": ov,
                        "prelim": prelim,
                        "start_dist": start_dist,
                        "pref": pref_key,
                        "start": [slat, slon],
                    })

    try_generate(tri_radii)

    # Out-and-back candidates: in steep terrain (e.g. a valley blocked by a
    # reservoir) a flat LOOP may be impossible, but a flat out-and-back along a
    # valley road/river usually exists. We add these only when a local DEM lets
    # us route on true gradient; loops are still preferred by scoring.
    if have_local_dem:
        _add_out_and_back(G, nidx, heur, start_nodes, target_m,
                          candidates, seen_hashes, source)

    # Adaptive retry: if nothing landed in the window but we DID build loops,
    # rescale the circumradius toward the target using the observed detour factor.
    if not candidates and diag["dist_samples"]:
        import statistics
        median_d = statistics.median(diag["dist_samples"])
        median_r = statistics.median(tri_radii)
        # detour factor: actual loop length per unit circumradius
        factor = median_d / median_r if median_r else 6.0
        ideal_r = target_m / factor if factor else target_m / 6.0
        retry_radii = [ideal_r * s for s in (0.75, 0.9, 1.0, 1.1, 1.25)]
        seen_hashes.clear()
        try_generate(retry_radii)

    if not candidates:
        dbg = ""
        if diag["dist_samples"]:
            lo = min(diag["dist_samples"]) / 1000
            hi = max(diag["dist_samples"]) / 1000
            dbg = (f" Built {diag['loops_built']} loops but none matched "
                   f"{target_m/1000:.0f} km (they ranged {lo:.1f}-{hi:.1f} km).")
        else:
            dbg = " No loops could be formed from the local path network."
        return {"routes": [], "source": source,
                "error": "Could not construct loops of the target distance here." + dbg}


    # Phase 2: fetch elevation only for the most promising candidates, then
    # compute full metrics + score. Cap keeps public-API calls bounded.
    candidates.sort(key=lambda c: c["prelim"], reverse=True)
    ELEV_CAP = 16
    finalists = candidates[:ELEV_CAP]
    # always keep a few out-and-back candidates (they're the flat option in
    # steep terrain and might otherwise be cut by the prelim cap)
    oab = [c for c in candidates if c.get("shape") == "out-and-back"]
    for c in oab[:4]:
        if c not in finalists:
            finalists.append(c)

    scored = []
    for c in finalists:
        coords = c["coords_ll"]
        eles = _coord_elevations(coords, c["pre_eles"])
        m = scoring.compute_metrics(coords, c["natures"], eles,
                                    c["start_dist"], target_m)
        shape = c.get("shape", "loop")
        sc, breakdown = scoring.score_route(m, c["overlap"], target_m,
                                            shape=shape)
        scored.append({
            "coords": [[p[0], p[1]] for p in coords],
            "elevations": [round(e, 1) for e in eles],
            "metrics": m,
            "overlap": c["overlap"],
            "score": sc,
            "breakdown": breakdown,
            "pref": c["pref"],
            "start": c["start"],
            "shape": shape,
            "elevation_profile": _profile(coords, eles),
        })

    # Guarantee a genuinely flat option (<=100 m gain) even in steep terrain,
    # by repeating the flattest short segment back-and-forth until the target
    # distance is reached. Accepts road/repetition in exchange for flatness.
    FLAT_MAX_GAIN = 100.0
    if have_local_dem and not any(x["metrics"]["elevation_gain_m"] <= FLAT_MAX_GAIN
                                  for x in scored):
        flat = _guaranteed_flat(G, nidx, start_nodes, target_m, FLAT_MAX_GAIN)
        if flat:
            scored.append(flat)

    # pick category winners for diversity
    result = _select_diverse(scored, max_routes, target_m)
    return {"routes": result, "source": source,
            "elevation_source": dem.source_name()}


def _guaranteed_flat(G, nidx, start_nodes, target_m, max_gain):
    """Build the flattest possible route of ~target length by finding the
    flattest reachable stretch and shuttling back and forth on it. This always
    stays under `max_gain` because we only extend along near-level ground."""
    from .config import PREFERENCES
    pref = dict(PREFERENCES["flattest"])
    pref["elev"] = 40.0    # dominate: avoid any climb
    pref["nature"] = 0.2
    pref["road"] = 0.2     # roads are fine if they're flat
    _apply_costs(G, pref)
    _apply_elevation_cost(G, pref)

    best_route = None
    best_gain = 1e18
    for start_dist, start in start_nodes[:3]:
        try:
            _, paths = nx.single_source_dijkstra(G, start, weight="cost")
        except Exception:
            continue
        # Choose the flattest usable shuttle leg: maximise length while keeping
        # the PER-LEG gain tiny, so repeating it many times still stays flat.
        # We want the leg with the best (length / gain) ratio, i.e. flattest
        # ground, preferring legs of a few hundred metres to ~2 km.
        turn = None
        best_ratio = -1.0
        for node, path in paths.items():
            if len(path) < 2:
                continue
            g = _path_gain(G, path)
            plen = _path_length(G, path)
            if plen < 250 or plen > target_m * 0.55:
                continue
            # flatness ratio: metres travelled per metre climbed (higher=flatter)
            ratio = plen / (g + 2.0)
            if ratio > best_ratio:
                best_ratio = ratio
                turn = path
        if not turn:
            continue
        turn_len = _path_length(G, turn)
        leg_gain = _path_gain(G, turn)
        if turn_len < 200:
            continue
        # how many round-trips before we exceed the flat-gain budget?
        # each full round-trip climbs ~leg_gain (up on the way, the descent
        # doesn't add to gain), so cap repeats to stay under max_gain.
        max_repeats_by_gain = int(max_gain / max(leg_gain, 1.0))
        repeats_for_dist = int((target_m * 0.97) / turn_len) + 1
        n_legs = max(2, min(repeats_for_dist, max_repeats_by_gain))

        one_leg = turn
        seq = [start]
        forward = True
        guard = 0
        while guard < n_legs:
            leg = one_leg[1:] if forward else list(reversed(one_leg))[1:]
            if not leg:
                break
            seq.extend(leg)
            forward = not forward
            guard += 1
        coords, natures, pre_eles = _loop_geometry(G, seq)
        if len(coords) < 4:
            continue
        eles = _coord_elevations(coords, pre_eles)
        m = scoring.compute_metrics(coords, natures, eles, start_dist, target_m)
        if m["elevation_gain_m"] < best_gain:
            best_gain = m["elevation_gain_m"]
            sc, breakdown = scoring.score_route(m, 1.0, target_m,
                                                shape="out-and-back")
            best_route = {
                "coords": [[p[0], p[1]] for p in coords],
                "elevations": [round(e, 1) for e in eles],
                "metrics": m, "overlap": 1.0, "score": sc,
                "breakdown": breakdown, "pref": "flattest",
                "start": [G.nodes[start]["lat"], G.nodes[start]["lon"]],
                "shape": "out-and-back",
                "elevation_profile": _profile(coords, eles),
            }
    return best_route


def _profile(coords, eles):
    """Return cumulative-distance vs elevation samples (downsampled), each with
    its lat/lon so the frontend can link the chart to a marker on the map."""
    prof = []
    cum = 0.0
    step = max(1, len(coords) // 120)
    for i in range(0, len(coords), step):
        if i > 0:
            # add distance since last sample
            for j in range(max(1, i - step + 1), i + 1):
                cum += haversine(coords[j-1][0], coords[j-1][1],
                                 coords[j][0], coords[j][1])
        prof.append({"d": round(cum, 1), "e": round(eles[i], 1),
                     "lat": coords[i][0], "lon": coords[i][1]})
    return prof


def _add_out_and_back(G, nidx, heur, start_nodes, target_m,
                      candidates, seen_hashes, source):
    """Generate flat out-and-back routes: go out along the flattest path to
    ~half the target distance, then return the same way. Marked as loops with
    overlap ~1.0 (full reuse) so scoring knows they aren't circuits."""
    from .config import PREFERENCES
    pref = dict(PREFERENCES["flattest"])
    # For out-and-back we care ONLY about flatness of the outbound path, so use
    # an extra-strong elevation weight and near-zero nature/road influence.
    pref["elev"] = 20.0
    pref["nature"] = 0.3
    pref["road"] = 0.3
    _apply_costs(G, pref)
    _apply_elevation_cost(G, pref)
    half = target_m / 2.0

    for start_dist, start in start_nodes[:2]:
        slat = G.nodes[start]["lat"]
        slon = G.nodes[start]["lon"]
        # Dijkstra outward, weighted by (elevation-aware) cost; pick turnaround
        # nodes whose PATH LENGTH is close to half the target.
        try:
            dist_by_cost, paths = nx.single_source_dijkstra(
                G, start, weight="cost", cutoff=None)
        except Exception:
            continue
        # Among nodes whose outbound length is close to half the target, choose
        # the one with the least elevation gain (flattest there-and-back of the
        # right total distance).
        best = None
        best_gain = 1e18
        for node, path in paths.items():
            if len(path) < 3:
                continue
            plen = _path_length(G, path)
            # keep the full out-and-back within ~15% of target => outbound within
            # 0.85..1.0 of half. This prevents collapsing to a short flat stub.
            if not (0.85 * half <= plen <= 1.05 * half):
                continue
            g = _path_gain(G, path)
            if g < best_gain:
                best_gain = g
                best = path
        if not best:
            continue
        # build out-and-back node sequence
        full = best + list(reversed(best[:-1]))
        coords, natures, pre_eles = _loop_geometry(G, full)
        if len(coords) < 4:
            continue
        m = scoring.compute_metrics(coords, natures, pre_eles or [],
                                    start_dist, target_m, skip_elevation=True)
        if not (0.7 * target_m <= m["distance_m"] <= 1.3 * target_m):
            continue
        sig = tuple((round(coords[int(len(coords)*f)][0], 3),
                     round(coords[int(len(coords)*f)][1], 3))
                    for f in (0.25, 0.5, 0.75))
        if sig in seen_hashes:
            continue
        seen_hashes.add(sig)
        prelim, _ = scoring.score_route(m, 1.0, target_m, elevation_known=False)
        candidates.append({
            "coords_ll": coords, "natures": natures, "pre_eles": pre_eles,
            "metrics": m, "overlap": 1.0, "prelim": prelim,
            "start_dist": start_dist, "pref": "flattest",
            "start": [slat, slon], "shape": "out-and-back",
        })


def _path_length(G, path):
    total = 0.0
    for a, b in _path_edges(path):
        total += G[a][b]["length"]
    return total


def _path_gain(G, path):
    """Elevation gain along a node path, using per-edge gains when available."""
    total = 0.0
    for a, b in _path_edges(path):
        eg = G[a][b].get("edge_gain")
        if eg is not None:
            total += eg
        else:
            e1 = G.nodes[a].get("ele")
            e2 = G.nodes[b].get("ele")
            if e1 is not None and e2 is not None and e2 > e1:
                total += e2 - e1
    return total


def _select_diverse(cands, max_routes, target_m):
    """Choose category winners + top scorers, tagging each."""
    tagged = []
    used = set()

    # Hide routes that climb too much (unrunnable-as-flat). Keep at least the
    # flattest one even if everything is steep, so the user always sees options.
    MAX_GAIN = 300.0
    flat_enough = [c for c in cands if c["metrics"]["elevation_gain_m"] <= MAX_GAIN]
    if flat_enough:
        cands = flat_enough
    else:
        cands = sorted(cands, key=lambda c: c["metrics"]["elevation_gain_m"])[:3]

    # Prefer candidates reasonably close to the requested distance for the
    # category winners, so "Flattest" can't cheat by returning a short loop
    # (less total climb simply because it's shorter).
    near = [c for c in cands
            if 0.85 * target_m <= c["metrics"]["distance_m"] <= 1.15 * target_m]
    pool_for = lambda: near if near else cands

    def take(key, sort_fn, label, restrict=True):
        base = pool_for() if restrict else cands
        pool = [c for c in base if id(c) not in used]
        if not pool:
            pool = [c for c in cands if id(c) not in used]
        if not pool:
            return
        best = min(pool, key=sort_fn) if key == "min" else max(pool, key=sort_fn)
        used.add(id(best))
        best = dict(best)
        best["category"] = label
        tagged.append(best)

    def gain_per_km(c):
        km = max(c["metrics"]["distance_m"] / 1000.0, 0.1)
        return c["metrics"]["elevation_gain_m"] / km

    take("max", lambda c: c["score"], "Best overall")
    take("min", gain_per_km, "Flattest")
    take("max", lambda c: c["metrics"]["nature_pct"], "Most nature")
    take("min", lambda c: c["metrics"]["start_distance_m"], "Closest start", restrict=False)
    take("max", lambda c: c["metrics"]["nature_pct"] + 0.3 *
         (c["metrics"]["elevation_gain_m"] / 100), "Scenic / Adventure")

    # fill remaining with next best scorers
    remaining = sorted([c for c in cands if id(c) not in used],
                       key=lambda c: c["score"], reverse=True)
    for c in remaining:
        if len(tagged) >= max_routes:
            break
        c = dict(c)
        c["category"] = "Alternative"
        tagged.append(c)

    # Order the final list by elevation gain (flattest first).
    tagged.sort(key=lambda c: c["metrics"]["elevation_gain_m"])
    return tagged
