import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import objc
import Quartz
from AppKit import NSEvent
from Foundation import NSURL

from config import CONFIG
from log import log, log_exc


@dataclass(frozen=True)
class CapturedFrame:
    session_id: str
    index: int  # 1-based
    path: Path  # captures/<id>/frame_0001.jpg
    captured_at: datetime
    t_offset_s: float  # seconds since session start
    display_id: int
    mouse_xy: tuple[float, float]  # global Cocoa coordinates at capture time
    width: int
    height: int


def write_jpeg(img, path: Path, quality: float) -> bool:
    url = NSURL.fileURLWithPath_(str(path))
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.jpeg", 1, None)
    if dest is None:
        return False
    Quartz.CGImageDestinationAddImage(dest, img, {Quartz.kCGImageDestinationLossyCompressionQuality: quality})
    return bool(Quartz.CGImageDestinationFinalize(dest))


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


class ScreenCapturer(threading.Thread):
    """Captures one display every `interval` seconds on a drift-free schedule."""

    def __init__(self, session, on_frame, config=CONFIG):
        super().__init__(name="screen-capture", daemon=True)
        self.session = session
        self.on_frame = on_frame
        self.interval = config.capture_interval_s
        self.quality = config.jpeg_quality
        self.scale = config.capture_scale
        self.stop_event = threading.Event()
        self.first_index = session.frame_count + 1  # continue numbering in a reused folder
        self.count = 0  # frames saved by this capturer
        self.skipped = 0

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_event.set()
        self.join(timeout)
        if self.is_alive():
            log("capture", "WARNING capture thread did not stop within 2 s; leaving it (daemon)")

    def run(self) -> None:
        log("capture", f"start: display={self.session.display_id} every {self.interval}s")
        next_t = time.monotonic()
        while not self.stop_event.is_set():
            with objc.autorelease_pool():
                try:
                    self._capture_one()
                except Exception:
                    log_exc("capture", "frame capture failed")
            next_t += self.interval
            now = time.monotonic()
            if next_t < now:  # overran: skip missed ticks instead of bunching
                missed = int((now - next_t) // self.interval) + 1
                self.skipped += missed
                next_t += missed * self.interval
                log("capture", f"overrun: skipped {missed} tick(s)", debug=True)
            self.stop_event.wait(max(0.0, next_t - time.monotonic()))
        log("capture", f"stopped: {self.count} frames ({self.skipped} ticks skipped)")

    def _capture_one(self) -> None:
        t0 = time.monotonic()
        mouse = NSEvent.mouseLocation()
        img = Quartz.CGDisplayCreateImage(self.session.display_id)
        if img is None:
            log("capture", f"CGDisplayCreateImage returned None for display {self.session.display_id} "
                           "(display gone or Screen Recording denied); skipping")
            return
        if self.scale != 1.0:
            img = scale_image(img, self.scale)
        index = self.first_index + self.count
        path = self.session.folder / f"frame_{index:04d}.jpg"
        if not write_jpeg(img, path, self.quality):
            log("capture", f"ERROR failed to write {path}; skipping")
            return
        self.count += 1
        frame = CapturedFrame(
            session_id=self.session.id,
            index=index,
            path=path,
            captured_at=datetime.now(),
            t_offset_s=t0 - self.session.started_at,
            display_id=self.session.display_id,
            mouse_xy=(mouse.x, mouse.y),
            width=Quartz.CGImageGetWidth(img),
            height=Quartz.CGImageGetHeight(img),
        )
        log("capture", f"frame {index} t={frame.t_offset_s:.2f}s {(time.monotonic() - t0) * 1000:.0f}ms "
                       f"{frame.width}x{frame.height} {path.stat().st_size // 1024}KB {path.name}", debug=True)
        self.on_frame(frame)
