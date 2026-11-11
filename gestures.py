"""Gesture detection (circle, back-and-forth underline) from the recorded cursor path.

Pure geometry: no AppKit, so recorded `path.json` files can be replayed offline.
Coordinates are global Cocoa points (y up); times are seconds since session start.
"""
import math
from dataclasses import asdict, dataclass

from config import CONFIG
from log import log


@dataclass
class Gesture:
    index: int  # 1-based, session-wide, in time order
    kind: str  # "circle" | "underline"
    t_start: float
    t_end: float
    bbox: tuple[float, float, float, float]  # (x, y, w, h), global Cocoa coordinates

    def to_dict(self) -> dict:
        d = asdict(self)
        d["t_start"], d["t_end"] = round(self.t_start, 3), round(self.t_end, 3)
        d["bbox"] = [round(v, 1) for v in self.bbox]
        return d


def _bbox(pts):
    xs = [p[1] for p in pts]
    ys = [p[2] for p in pts]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _overlap(a, b) -> float:
    """Intersection area / smaller box area (1.0 when one box lies inside the other)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    smaller = min(aw * ah, bw * bh)
    return ix * iy / smaller if smaller > 0 else 0.0


def _union_box(a, b):
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0)


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def resample(stroke, spacing: float):
    """Points (t, x, y) spaced ~`spacing` apart along the path (keeps the first and last)."""
    if not stroke:
        return []
    out = [stroke[0]]
    acc = 0.0
    for prev, cur in zip(stroke, stroke[1:]):
        acc += math.hypot(cur[1] - prev[1], cur[2] - prev[2])
        if acc >= spacing:
            out.append(cur)
            acc = 0.0
    if out[-1] is not stroke[-1]:
        out.append(stroke[-1])
    return out


def _turns(pts):
    """Signed turning angle (radians) at each interior vertex; turns[k] is at pts[k+1]."""
    out = []
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        a1 = math.atan2(b[2] - a[2], b[1] - a[1])
        a2 = math.atan2(c[2] - b[2], c[1] - b[1])
        d = a2 - a1
        while d > math.pi:
            d -= 2 * math.pi
        while d < -math.pi:
            d += 2 * math.pi
        out.append(d)
    return out


class GestureDetector:
    """Feed cursor samples; strokes are split at pauses and classified when they end."""

    def __init__(self, on_gesture, first_index: int = 1, config=CONFIG):
        self.cfg = config
        self.on_gesture = on_gesture
        self.next_index = first_index
        self.gestures: list[Gesture] = []
        self._samples: list[tuple[float, float, float]] = []  # recent samples for windowed speed
        self._stroke: list[tuple[float, float, float]] = []
        self._still_since = None

    # --- streaming -------------------------------------------------------

    def feed(self, t: float, x: float, y: float) -> None:
        cfg = self.cfg
        self._samples.append((t, x, y))
        while self._samples and t - self._samples[0][0] > cfg.gesture_speed_window_s:
            self._samples.pop(0)
        ref = self._samples[0]
        dt = t - ref[0]
        speed = math.hypot(x - ref[1], y - ref[2]) / dt if dt > 0 else 0.0

        if speed >= cfg.gesture_pause_speed_pt_s:
            self._still_since = None
            if not self._stroke:
                self._stroke.append(ref)  # start where the movement began
            self._stroke.append((t, x, y))
            return

        if self._stroke:
            self._stroke.append((t, x, y))
            if self._still_since is None:
                self._still_since = t
            elif t - self._still_since >= cfg.gesture_pause_s:
                self._end_stroke()

    def finish(self) -> None:
        """Close the current stroke (session/segment end)."""
        if self._stroke:
            self._end_stroke()
        self._samples.clear()

    def _end_stroke(self) -> None:
        stroke, self._stroke, self._still_since = self._stroke, [], None
        for kind, t0, t1, bbox in detect(stroke, self.cfg):
            last = self.gestures[-1] if self.gestures else None
            if last and last.kind == kind == "circle" and _iou(last.bbox, bbox) >= self.cfg.gesture_merge_iou:
                # Repeated loop around the same target: extend the previous gesture.
                last.t_end = t1
                last.bbox = _union_box(last.bbox, bbox)
                log("gesture", f"merged repeated circle into #{last.index}", debug=True)
                continue
            g = Gesture(self.next_index, kind, t0, t1, bbox)
            self.next_index += 1
            self.gestures.append(g)
            self.on_gesture(g)


# --- offline classification ---------------------------------------------------


def detect(stroke, config=CONFIG):
    """Classify one stroke. Returns [(kind, t_start, t_end, bbox)] in time order."""
    cfg = config
    pts = resample(stroke, cfg.gesture_resample_pt)
    if len(pts) < 4:
        return []
    found = []
    circles = _find_circles(pts, cfg)
    # Underlines are searched in the parts of the stroke not used by circles.
    start = 0
    for i, j in circles + [(len(pts), len(pts))]:
        found += _find_underlines(pts[start:i], cfg)
        start = j + 1
    # Loops drawn in one continuous stroke around the same area are one gesture (repeated circling,
    # including smaller loops inside a bigger one).
    loops = []
    for i, j in circles:
        seg = pts[i: j + 1]
        loop = ("circle", seg[0][0], seg[-1][0], _bbox(seg))
        if loops and _overlap(loops[-1][3], loop[3]) >= cfg.gesture_merge_overlap:
            prev = loops[-1]
            loops[-1] = ("circle", prev[1], loop[2], _union_box(prev[3], loop[3]))
        else:
            loops.append(loop)
    found += loops
    found.sort(key=lambda g: g[1])
    if cfg.debug:
        bx = _bbox(pts)
        log("gesture", f"stroke {stroke[0][0]:.2f}-{stroke[-1][0]:.2f}s pts={len(pts)} "
                       f"bbox={bx[2]:.0f}x{bx[3]:.0f} turn={math.degrees(sum(_turns(pts))):.0f}° "
                       f"loops={len(circles)} -> {[g[0] for g in found] or 'travel'}", debug=True)
    return found


def _find_circles(pts, cfg):
    """Non-overlapping loops (i, j): enough turning, closes on itself, roughly round, big enough."""
    turns = _turns(pts)
    prefix = [0.0]
    for a in turns:
        prefix.append(prefix[-1] + a)  # prefix[k] = sum of turns at pts[1..k]

    min_turn = math.radians(cfg.circle_min_turn_deg)
    loops, j, lo = [], 3, 0
    while j < len(pts):
        x0 = x1 = pts[j][1]
        y0 = y1 = pts[j][2]
        hit = None
        for i in range(j - 1, lo - 1, -1):
            x0, x1 = min(x0, pts[i][1]), max(x1, pts[i][1])
            y0, y1 = min(y0, pts[i][2]), max(y1, pts[i][2])
            if j - i < 3:
                continue
            turn = abs(prefix[j - 1] - prefix[i])  # turning at pts[i+1..j-1]
            if turn < min_turn:
                continue
            w, h = x1 - x0, y1 - y0
            diag = math.hypot(w, h)
            if max(w, h) < cfg.gesture_min_size_pt or min(w, h) <= 0:
                continue
            if max(w, h) / min(w, h) > cfg.circle_max_aspect:
                continue
            gap = math.hypot(pts[j][1] - pts[i][1], pts[j][2] - pts[i][2])
            if gap <= cfg.circle_close_ratio * diag:
                hit = i
                break
        if hit is not None:
            loops.append((hit, j))
            lo = j + 1
            j = lo + 3
        else:
            j += 1
    return loops


def _legs(pts, cfg):
    """Maximal runs with the same horizontal direction; legs narrower than underline_min_leg_pt are folded in."""
    if len(pts) < 2:
        return []
    legs = []
    cur = [pts[0], pts[1]]
    sign = math.copysign(1, pts[1][1] - pts[0][1])
    for a, b in zip(pts[1:], pts[2:]):
        dx = b[1] - a[1]
        s = math.copysign(1, dx) if dx else sign
        if s == sign:
            cur.append(b)
        else:
            legs.append(cur)
            cur, sign = [a, b], s
    legs.append(cur)
    merged = []
    for leg in legs:
        if merged and abs(leg[-1][1] - leg[0][1]) < cfg.underline_min_leg_pt:
            merged[-1] = merged[-1] + leg[1:]
        else:
            merged.append(leg)
    return merged


def _fit_band(seg, cfg):
    """Robust near-horizontal line y = a*x + b through most points (RANSAC + least-squares refit).

    Returns (a, b, inlier_mask). Only slopes up to underline_max_slope are considered, and a point is an
    inlier when its vertical distance to the line is within underline_band_pt.
    """
    n = len(seg)
    band, max_slope = cfg.underline_band_pt, cfg.underline_max_slope
    best = (0, 0.0, sum(p[2] for p in seg) / n)
    stride = max(1, n // 40)  # deterministic candidate pairs: ~40 x 40
    idx = range(0, n, stride)
    for i in idx:
        for j in idx:
            dx = seg[j][1] - seg[i][1]
            if dx <= cfg.underline_min_leg_pt * 0.5:
                continue
            a = (seg[j][2] - seg[i][2]) / dx
            if abs(a) > max_slope:
                continue
            b = seg[i][2] - a * seg[i][1]
            count = sum(1 for p in seg if abs(p[2] - (a * p[1] + b)) <= band)
            if count > best[0]:
                best = (count, a, b)
    _, a, b = best
    inl = [abs(p[2] - (a * p[1] + b)) <= band for p in seg]
    xs = [p[1] for p, k in zip(seg, inl) if k]
    ys = [p[2] for p, k in zip(seg, inl) if k]
    if len(xs) >= 3:  # least-squares refit on inliers
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        vx = sum((x - mx) ** 2 for x in xs)
        if vx > 0:
            a2 = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
            if abs(a2) <= max_slope:
                a, b = a2, my - a2 * mx
                inl = [abs(p[2] - (a * p[1] + b)) <= band for p in seg]
    return a, b, inl


def _longest_inlier_run(inl, max_gap: int):
    """(start, end) indices of the longest run of inliers, bridging outlier gaps of up to max_gap points."""
    best, start, last_in, gap = (0, -1), None, None, 0
    for k, ok in enumerate(inl):
        if ok:
            if start is None:
                start = k
            last_in, gap = k, 0
            if last_in - start > best[1] - best[0]:
                best = (start, last_in)
        elif start is not None:
            gap += 1
            if gap > max_gap:
                start, gap = None, 0
    return best


def _refine_underline(seg, cfg):
    """Drop stray travel (e.g. a slanted approach) around an underline candidate. None if nothing is left."""
    a, b, inl = _fit_band(seg, cfg)
    i, j = _longest_inlier_run(inl, cfg.underline_max_gap_pts)
    if j < 0:
        return None
    core = [p for p, k in zip(seg[i: j + 1], inl[i: j + 1]) if k]
    if len(core) < 3:
        return None
    bx = _bbox(core)
    reversals = max(0, len(_legs(core, cfg)) - 1)
    if bx[2] < cfg.underline_min_dx_pt or reversals < cfg.underline_min_reversals:
        return None
    if cfg.debug:
        log("gesture", f"underline refine: line y={a:.3f}x+{b:.0f}, inliers {sum(inl)}/{len(seg)}, "
                       f"kept {seg[i][0]:.2f}-{seg[j][0]:.2f}s ({len(core)} pts), bbox {bx[2]:.0f}x{bx[3]:.0f}, "
                       f"reversals {reversals}", debug=True)
    return core[0][0], core[-1][0], bx


def _find_underlines(pts, cfg):
    """Runs of ≥ (reversals + 1) mostly-horizontal legs that alternate direction, trimmed to the dominant line."""
    if len(pts) < 3:
        return []
    merged = _legs(pts, cfg)

    def flat(leg):
        bx = _bbox(leg)
        return abs(leg[-1][1] - leg[0][1]) >= cfg.underline_min_leg_pt and bx[3] <= cfg.underline_max_dy_ratio * max(bx[2], 1e-6)

    out, run = [], []
    for leg in merged + [None]:
        if leg is not None and flat(leg) and (not run or math.copysign(1, leg[-1][1] - leg[0][1])
                                              != math.copysign(1, run[-1][-1][1] - run[-1][0][1])):
            run.append(leg)
            continue
        if len(run) >= cfg.underline_min_reversals + 1:
            seg = [p for lg in run for p in lg]
            refined = _refine_underline(seg, cfg)
            if refined is not None:
                t0, t1, bx = refined
                if bx[3] <= cfg.underline_max_dy_ratio * bx[2]:
                    out.append(("underline", t0, t1, bx))
        run = [leg] if leg is not None and flat(leg) else []
    return out
