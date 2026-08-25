"""Route metrics + scoring."""
from .config import ScoreWeights, DEFAULT_WEIGHTS
from .geo import haversine


def _smooth(vals, window=3):
    if len(vals) < window:
        return list(vals)
    out = []
    half = window // 2
    for i in range(len(vals)):
        lo = max(0, i - half)
        hi = min(len(vals), i + half + 1)
        out.append(sum(vals[lo:hi]) / (hi - lo))
    return out


def _clean_voids(eles):
    """SRTM/void points often come back as 0.0 (or absurd values), producing
    false cliffs. Replace those with the nearest valid neighbour so elevation
    gain isn't corrupted."""
    if not eles:
        return eles
    valid = [e for e in eles if e is not None and 5.0 < e < 9000.0]
    if not valid:
        return [0.0] * len(eles)
    median = sorted(valid)[len(valid) // 2]

    def ok(e):
        # treat exact 0 / None / absurd as void; also values wildly off the
        # median (> 800 m jump) are almost certainly DEM voids.
        return (e is not None and 5.0 < e < 9000.0
                and abs(e - median) < 1500.0)

    out = list(eles)
    # forward-fill voids with last good value
    last_good = None
    for i, e in enumerate(out):
        if ok(e):
            last_good = e
        else:
            out[i] = last_good
    # back-fill any leading voids
    next_good = None
    for i in range(len(out) - 1, -1, -1):
        if ok(eles[i]):
            next_good = eles[i]
        elif out[i] is None:
            out[i] = next_good
    # anything still None -> median
    return [median if v is None else v for v in out]


def compute_metrics(coords, natures, eles, start_dist_m, target_m,
                    skip_elevation=False):
    """coords: [(lat,lon)], natures: per-segment nature (len = segments),
    eles: elevation per coord, start_dist_m, target_m.
    Returns metrics dict. If skip_elevation, elevation fields are 0/None."""
    # distance
    seg_len = []
    for a, b in zip(coords[:-1], coords[1:]):
        seg_len.append(haversine(a[0], a[1], b[0], b[1]))
    total = sum(seg_len)

    # elevation gain/loss with small smoothing threshold to reduce DEM noise
    gain = loss = 0.0
    highest = lowest = 0.0
    if not skip_elevation and eles:
        eles = _clean_voids(eles)
        # smoothing threshold filters DEM/SRTM noise (public SRTM ~30m is noisy).
        # Only count sustained changes above the threshold as real gain/loss.
        thresh = 4.0
        # light moving-average smoothing first
        sm = _smooth(eles, 3)
        prev = sm[0]
        highest = lowest = prev
        for e in sm[1:]:
            d = e - prev
            if d > thresh:
                gain += d
                prev = e
            elif d < -thresh:
                loss += -d
                prev = e
            highest = max(highest, e)
            lowest = min(lowest, e)

    # nature weighted by segment length
    if seg_len and natures:
        m = min(len(seg_len), len(natures))
        nat_num = sum(seg_len[i] * natures[i] for i in range(m))
        nat_pct = nat_num / sum(seg_len[:m]) if sum(seg_len[:m]) else 0
        road_len = sum(seg_len[i] for i in range(m) if natures[i] < 0.4)
        road_pct = road_len / total if total else 0
    else:
        nat_pct = road_pct = 0

    return {
        "distance_m": total,
        "elevation_gain_m": gain,
        "elevation_loss_m": loss,
        "highest_m": highest,
        "lowest_m": lowest,
        "nature_pct": nat_pct,
        "road_pct": road_pct,
        "start_distance_m": start_dist_m,
    }


def score_route(m, overlap_ratio, target_m, w: ScoreWeights = DEFAULT_WEIGHTS,
                elevation_known=True, shape="loop"):
    """Return (score 0..100, breakdown dict). If elevation is unknown, a
    neutral elevation score is used so ranking still works in phase 1.
    `shape` of 'out-and-back' isn't penalised for edge reuse (that's expected)."""
    km = m["distance_m"] / 1000.0
    target_km = target_m / 1000.0

    # distance accuracy: full marks within +-5%, decays outside
    diff = abs(km - target_km) / target_km
    dist_score = max(0.0, 1.0 - (diff / 0.30))  # 0 at 30% off

    # elevation: gain per km. good baseline configurable.
    if elevation_known:
        gpk = m["elevation_gain_m"] / max(km, 0.1)
        elev_score = max(0.0, 1.0 - (gpk / 30.0))
    else:
        gpk = 0.0
        elev_score = 0.7  # neutral placeholder for phase-1 ranking

    nature_score = m["nature_pct"]

    # start proximity
    sd = m["start_distance_m"]
    if sd <= w.start_good_m:
        start_score = 1.0
    else:
        start_score = max(0.0, 1.0 - (sd - w.start_good_m) / 2000.0)

    if shape == "out-and-back":
        # reusing the path is the whole point; give a moderate (not zero) score
        loop_score = 0.6
    else:
        loop_score = max(0.0, 1.0 - overlap_ratio)  # low reuse = good loop

    total_w = (w.w_distance + w.w_elevation + w.w_nature +
               w.w_start + w.w_loop)
    raw = (w.w_distance * dist_score +
           w.w_elevation * elev_score +
           w.w_nature * nature_score +
           w.w_start * start_score +
           w.w_loop * loop_score)
    score = 100.0 * raw / total_w

    difficulty = _difficulty(gpk)
    # A display-friendly flatness on a gentler curve than the scoring one, so the
    # UI bar stays informative across real terrain (0 m/km=100%, ~80 m/km~=0%).
    flatness_display = max(0.0, 1.0 - (gpk / 80.0))
    return round(score, 1), {
        "distance": round(dist_score, 2),
        "elevation": round(elev_score, 2),
        "flatness_display": round(flatness_display, 2),
        "nature": round(nature_score, 2),
        "start": round(start_score, 2),
        "loop": round(loop_score, 2),
        "gain_per_km": round(gpk, 1),
        "difficulty": difficulty,
    }


def _difficulty(gain_per_km):
    if gain_per_km < 3:
        return "Very easy"
    if gain_per_km < 8:
        return "Easy"
    if gain_per_km < 15:
        return "Moderate"
    if gain_per_km < 25:
        return "Hard"
    return "Very hard"
