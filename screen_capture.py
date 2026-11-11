"""Event-driven screen capture: a rolling in-memory frame buffer, trigger-based selection, and changed-area dedup."""
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import objc
import Quartz
from AppKit import NSEvent
from Foundation import NSURL, NSMutableData

from config import CONFIG
from log import log, log_exc


@dataclass(frozen=True)
class CapturedFrame:
    """A frame that was kept (written to disk). Handed to the image worker."""
    session_id: str
    index: int  # 1-based, per session folder
    path: Path  # captures/<id>/frame_0001.jpg
    captured_at: datetime
    t_offset_s: float  # seconds since session start
    display_id: int
    mouse_xy: tuple[float, float]  # global Cocoa coordinates at capture time
    width: int
    height: int
    triggers: tuple = ()


@dataclass
class BufferedFrame:
    t: float  # seconds since session start (capture start)
    jpeg: bytes
    thumb: np.ndarray  # grayscale uint8, dedup_thumb_width wide
    display_id: int
    mouse: tuple[float, float]
    width: int
    height: int
    app: str
    wall: datetime = field(default_factory=datetime.now)


def write_jpeg(img, path: Path, quality: float) -> bool:
    url = NSURL.fileURLWithPath_(str(path))
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.jpeg", 1, None)
    if dest is None:
        return False
    Quartz.CGImageDestinationAddImage(dest, img, {Quartz.kCGImageDestinationLossyCompressionQuality: quality})
    return bool(Quartz.CGImageDestinationFinalize(dest))


def encode_jpeg(img, quality: float) -> bytes | None:
    data = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    if dest is None:
        return None
    Quartz.CGImageDestinationAddImage(dest, img, {Quartz.kCGImageDestinationLossyCompressionQuality: quality})
    return bytes(data) if Quartz.CGImageDestinationFinalize(dest) else None


def scale_image(img, factor: float):
    """Resample a CGImage by `factor` with high-quality interpolation."""
    w = max(1, round(Quartz.CGImageGetWidth(img) * factor))
    h = max(1, round(Quartz.CGImageGetHeight(img) * factor))
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaNoneSkipFirst | Quartz.kCGBitmapByteOrder32Little,
    )
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)
    return Quartz.CGBitmapContextCreateImage(ctx)


def gray_thumb(img, width: int) -> np.ndarray:
    """Grayscale thumbnail `width` px wide as a uint8 array (rows x cols)."""
    w = width
    h = max(1, round(Quartz.CGImageGetHeight(img) * width / Quartz.CGImageGetWidth(img)))
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, w, Quartz.CGColorSpaceCreateDeviceGray(), Quartz.kCGImageAlphaNone)
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationMedium)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)
    data = Quartz.CGBitmapContextGetData(ctx)
    return np.frombuffer(data.as_buffer(w * h), np.uint8).reshape(h, w).copy()


def changed_pct(a: np.ndarray, b: np.ndarray, delta: int) -> float:
    """Percent of thumbnail pixels whose gray level differs by more than `delta`."""
    if a.shape != b.shape:
        return 100.0
    return float((np.abs(a.astype(np.int16) - b.astype(np.int16)) > delta).mean() * 100.0)


class ScreenCapturer(threading.Thread):
    """Grabs a frame every frame_buffer_interval_s into a rolling buffer; persists only requested frames.

    Main-thread API: request_gesture(g), request_app_switch(t, app), stop(). Kept frames are appended to
    session.frames (dicts) and passed to on_frame(CapturedFrame).
    """

    def __init__(self, session, on_frame, config=CONFIG, current_app=lambda: "", protect_from=lambda: None):
        super().__init__(name="screen-capture", daemon=True)
        self.session = session
        self.on_frame = on_frame
        self.cfg = config
        self.current_app = current_app  # () -> frontmost app name (maintained on the main thread)
        self.protect_from = protect_from  # () -> session time of the stroke in progress (frames kept from there)
        self.stop_event = threading.Event()
        self.requests: queue.Queue = queue.Queue()
        self.buffer: deque[BufferedFrame] = deque()
        self.pending: list[dict] = []  # requests waiting for a frame (gesture end, app switch)
        self.count = 0  # frames kept by this capturer
        self.dropped = 0
        self.grabbed = 0
        self.skipped = 0
        self._next_safety = config.safety_net_interval_s

    # --- main-thread API -----------------------------------------------------

    def request_gesture(self, g) -> None:
        self.requests.put({"kind": "gesture", "gesture": g})

    def request_app_switch(self, t: float, app: str) -> None:
        self.requests.put({"kind": "app_switch", "t": t, "app": app})

    def stop(self, timeout: float = 3.0) -> None:
        self.stop_event.set()
        self.join(timeout)
        if self.is_alive():
            log("capture", "WARNING capture thread did not stop in time; leaving it (daemon)")

    # --- capture thread ------------------------------------------------------

    def run(self) -> None:
        cfg = self.cfg
        log("capture", f"start: display={self.session.display_id} buffer every {cfg.frame_buffer_interval_s}s, "
                       f"safety net every {cfg.safety_net_interval_s}s")
        next_t = time.monotonic()
        first = True
        while not self.stop_event.is_set():
            with objc.autorelease_pool():
                try:
                    f = self._grab()
                    if f is not None:
                        self._handle(f, first)
                        first = False
                except Exception:
                    log_exc("capture", "capture tick failed")
            next_t += cfg.frame_buffer_interval_s
            now = time.monotonic()
            if next_t < now:  # overran: skip missed ticks instead of bunching
                missed = int((now - next_t) // cfg.frame_buffer_interval_s) + 1
                self.skipped += missed
                next_t += missed * cfg.frame_buffer_interval_s
            self.stop_event.wait(max(0.0, next_t - time.monotonic()))

        # Release: one fresh frame, then settle everything still pending.
        with objc.autorelease_pool():
            try:
                f = self._grab()
                if f is not None:
                    self._drain_requests(f.t)
                    self._persist(f, "release")
                    self._resolve_pending(final=True)
            except Exception:
                log_exc("capture", "release capture failed")
        log("capture", f"stopped: grabbed {self.grabbed}, kept {self.count}, dropped {self.dropped} duplicates, "
                       f"{self.skipped} ticks skipped")

    def _grab(self) -> BufferedFrame | None:
        cfg = self.cfg
        t0 = time.monotonic()
        mouse = NSEvent.mouseLocation()
        img = Quartz.CGDisplayCreateImage(self.session.display_id)
        if img is None:
            ms = (time.monotonic() - t0) * 1000
            log("capture", f"CGDisplayCreateImage returned None for display {self.session.display_id} after "
                           f"{ms:.0f} ms ({'capture service stalled' if ms > 2000 else 'display gone or Screen Recording denied'}); "
                           "skipping")
            return None
        if cfg.capture_scale != 1.0:
            img = scale_image(img, cfg.capture_scale)
        jpeg = encode_jpeg(img, cfg.jpeg_quality)
        if jpeg is None:
            log("capture", "ERROR JPEG encode failed; skipping")
            return None
        f = BufferedFrame(
            t=t0 - self.session.started_at,
            jpeg=jpeg,
            thumb=gray_thumb(img, cfg.dedup_thumb_width),
            display_id=self.session.display_id,
            mouse=(mouse.x, mouse.y),
            width=Quartz.CGImageGetWidth(img),
            height=Quartz.CGImageGetHeight(img),
            app=self.current_app(),
        )
        self.grabbed += 1
        log("capture", f"buffered t={f.t:.2f}s {(time.monotonic() - t0) * 1000:.0f}ms {len(jpeg) // 1024}KB "
                       f"app={f.app!r} buffer={len(self.buffer) + 1}", debug=True)
        return f

    def _handle(self, f: BufferedFrame, first: bool) -> None:
        self.buffer.append(f)
        if first:
            self._persist(f, "start")
        self._drain_requests(f.t)
        self._resolve_pending(final=False)
        if f.t >= self._next_safety:
            self._persist(f, "safety")
            self._next_safety = f.t + self.cfg.safety_net_interval_s
        self._prune(f.t)

    def _drain_requests(self, now_t: float) -> None:
        while True:
            try:
                r = self.requests.get_nowait()
            except queue.Empty:
                return
            if r["kind"] == "gesture":
                g = r["gesture"]
                before = [f for f in self.buffer if f.t <= g.t_start]
                start = before[-1] if before else (self.buffer[0] if self.buffer else None)
                if start is None:
                    log("capture", f"gesture #{g.index}: no buffered frame for its start")
                else:
                    if not before:
                        log("capture", f"gesture #{g.index}: start {g.t_start:.2f}s is older than the buffer; "
                                       f"using oldest frame t={start.t:.2f}s")
                    self._persist(start, "gesture_start", gesture=g.index)
                self.pending.append({"kind": "gesture_end", "t": g.t_end, "gesture": g.index})
            elif r["kind"] == "app_switch":
                self.pending.append({"kind": "app_switch", "t": r["t"] + self.cfg.app_switch_settle_s, "app": r["app"]})

    def _resolve_pending(self, final: bool) -> None:
        still = []
        for p in self.pending:
            after = [f for f in self.buffer if f.t >= p["t"]]
            f = after[0] if after else (self.buffer[-1] if final and self.buffer else None)
            if f is None:
                still.append(p)
            elif p["kind"] == "gesture_end":
                self._persist(f, "gesture_end", gesture=p["gesture"])
            else:
                self._persist(f, "app_switch")
        self.pending = still

    def _prune(self, now_t: float) -> None:
        keep_from = now_t - self.cfg.frame_buffer_s
        protect = self.protect_from()
        if protect is not None:
            keep_from = min(keep_from, protect - self.cfg.frame_buffer_interval_s)
        while self.buffer and (self.buffer[0].t < keep_from or len(self.buffer) > self.cfg.frame_buffer_max):
            self.buffer.popleft()

    def _persist(self, f: BufferedFrame, trigger: str, gesture: int | None = None) -> None:
        """Keep `f` unless it duplicates an already-kept frame; gesture refs merge into the duplicate."""
        cfg, s = self.cfg, self.session
        best, best_pct = None, 100.0
        for k in s.frames:
            pct = changed_pct(f.thumb, k["thumb"], cfg.dedup_pixel_delta)
            if pct < best_pct:
                best, best_pct = k, pct
        if best is not None and best_pct < cfg.dedup_changed_pct:
            if trigger not in best["triggers"]:
                best["triggers"].append(trigger)
            if gesture is not None and gesture not in best["gestures"]:
                best["gestures"].append(gesture)
            best["merged"].append({"trigger": trigger, "t": round(f.t, 3), "changed_pct": round(best_pct, 2),
                                   **({"gesture": gesture} if gesture is not None else {})})
            if gesture is None:
                self.dropped += 1
            log("capture", f"{trigger}{f' #{gesture}' if gesture else ''} t={f.t:.2f}s duplicates {best['file']} "
                           f"({best_pct:.1f}% changed) -> {'merged' if gesture else 'dropped'}", debug=True)
            return
        index = s.frame_count + self.count + 1
        path = s.folder / f"frame_{index:04d}.jpg"
        try:
            path.write_bytes(f.jpeg)
        except OSError:
            log_exc("capture", f"writing {path} failed; skipping")
            return
        self.count += 1
        record = {
            "file": path.name, "path": path, "t": round(f.t, 3), "triggers": [trigger],
            "gestures": [gesture] if gesture is not None else [], "app": f.app, "display_id": f.display_id,
            "mouse": [round(f.mouse[0], 1), round(f.mouse[1], 1)], "width": f.width, "height": f.height,
            "thumb": f.thumb, "merged": [],
            "changed_pct_vs_nearest": None if best is None else round(best_pct, 2),
        }
        s.frames.append(record)
        log("capture", f"kept {path.name} [{trigger}{f' #{gesture}' if gesture else ''}] t={f.t:.2f}s "
                       f"app={f.app!r} {len(f.jpeg) // 1024}KB"
                       + ("" if best is None else f" ({best_pct:.1f}% changed vs {best['file']})"))
        self.on_frame(CapturedFrame(
            session_id=s.id, index=index, path=path, captured_at=f.wall, t_offset_s=f.t,
            display_id=f.display_id, mouse_xy=f.mouse, width=f.width, height=f.height, triggers=(trigger,),
        ))
