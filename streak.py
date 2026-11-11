import time
from collections import deque

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSLineCapStyleRound,
    NSLineJoinStyleRound,
    NSRunLoopCommonModes,
    NSScreen,
    NSScreenSaverWindowLevel,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSMakeRect, NSObject, NSRunLoop, NSTimer

from config import CONFIG
from log import log, log_exc

# Per-pass alpha multipliers: outer glow, inner glow, core.
PASS_ALPHAS = (0.15, 0.40, 1.0)


class Trail:
    def __init__(self, fade_s: float):
        self.fade_s = fade_s
        self.points: deque[tuple[float, float, float]] = deque()

    def add(self, x: float, y: float, t: float) -> None:
        if self.points:
            lx, ly, _ = self.points[-1]
            if abs(x - lx) < 0.5 and abs(y - ly) < 0.5:
                return
        self.points.append((x, y, t))

    def prune(self, now: float) -> None:
        while self.points and now - self.points[0][2] > self.fade_s:
            self.points.popleft()

    def is_empty(self) -> bool:
        return not self.points

    def bounds(self, pad: float):
        """Global (x0, y0, x1, y1) of all points, padded; None if empty."""
        if not self.points:
            return None
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


class StreakView(NSView):
    def initWithFrame_overlay_(self, frame, overlay):
        self = objc.super(StreakView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.overlay = overlay
        return self

    def isOpaque(self):
        return False

    def drawRect_(self, _rect):
        try:
            self.overlay.draw(self)
        except Exception:
            log_exc("streak", "drawRect_ failed")


class StreakTimerTarget(NSObject):
    def initWithCallback_(self, callback):
        self = self.init()
        self.callback = callback
        return self

    def tick_(self, _timer):
        try:
            self.callback()
        except Exception:
            log_exc("streak", "tick failed")


class StreakOverlay:
    """Glowing mouse trail drawn in transparent overlay windows. Main thread only."""

    def __init__(self, config=CONFIG):
        self.cfg = config
        self.trail = Trail(config.streak_fade_s)
        self.sampling = False
        self.windows: list = []
        self._timer = None
        self._target = StreakTimerTarget.alloc().initWithCallback_(self._tick)
        self._last_bounds = None
        self.on_sample = None  # fn(x, y, t_monotonic), called for every sample while sampling
        glow = NSColor.colorWithCalibratedRed_green_blue_alpha_(*config.streak_glow_rgba)
        core = NSColor.colorWithCalibratedRed_green_blue_alpha_(*config.streak_core_rgba)
        self._passes = [
            (glow, config.streak_widths[0], PASS_ALPHAS[0] * config.streak_glow_rgba[3]),
            (glow, config.streak_widths[1], PASS_ALPHAS[1] * config.streak_glow_rgba[3]),
            (core, config.streak_widths[2], PASS_ALPHAS[2] * config.streak_core_rgba[3]),
        ]

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if not self.windows:
            self._open_windows()
        self.sampling = True
        if self._timer is None:
            self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / self.cfg.streak_sample_hz, self._target, "tick:", None, True
            )
            NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)
        log("streak", f"start ({len(self.windows)} overlay windows)")

    def stop(self) -> None:
        """Stop sampling; windows close once the trail has faded out."""
        self.sampling = False
        log("streak", "stop sampling (fading out)")

    def close(self) -> None:
        """Close immediately (used on quit)."""
        self.sampling = False
        self.trail.points.clear()
        self._teardown()

    def _open_windows(self) -> None:
        behavior = (
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        for screen in NSScreen.screens():
            frame = screen.frame()
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
            )
            win.setOpaque_(False)
            win.setBackgroundColor_(NSColor.clearColor())
            win.setHasShadow_(False)
            win.setIgnoresMouseEvents_(True)
            win.setLevel_(NSScreenSaverWindowLevel)
            win.setCollectionBehavior_(behavior)
            win.setReleasedWhenClosed_(False)
            if not self.cfg.frames_include_streak:
                win.setSharingType_(NSWindowSharingNone)  # clean frames: streak not captured
            view = StreakView.alloc().initWithFrame_overlay_(
                NSMakeRect(0, 0, frame.size.width, frame.size.height), self
            )
            win.setContentView_(view)
            win.orderFrontRegardless()
            self.windows.append(win)
            display_id = screen.deviceDescription()["NSScreenNumber"]
            log("streak", f"overlay window opened: display={display_id} frame={tuple(frame.origin)}+{tuple(frame.size)}",
                debug=True)

    def _teardown(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        for win in self.windows:
            win.orderOut_(None)
            win.close()
        if self.windows:
            log("streak", f"closed {len(self.windows)} overlay windows", debug=True)
        self.windows = []
        self._last_bounds = None

    # --- per tick --------------------------------------------------------

    def _tick(self) -> None:
        now = time.monotonic()
        if self.sampling:
            p = NSEvent.mouseLocation()
            self.trail.add(p.x, p.y, now)
            if self.on_sample is not None:
                self.on_sample(p.x, p.y, now)
        self.trail.prune(now)

        pad = max(self.cfg.streak_widths)
        bounds = self.trail.bounds(pad)
        dirty = _union(bounds, self._last_bounds)
        self._last_bounds = bounds
        if dirty is not None:
            for win in self.windows:
                origin = win.frame().origin
                x0, y0, x1, y1 = dirty
                win.contentView().setNeedsDisplayInRect_(
                    NSMakeRect(x0 - origin.x, y0 - origin.y, x1 - x0, y1 - y0)
                )

        if not self.sampling and self.trail.is_empty():
            self._teardown()
            log("streak", "faded out")

    def draw(self, view) -> None:
        pts = self.trail.points
        if len(pts) < 2:
            return
        origin = view.window().frame().origin
        ox, oy = origin.x, origin.y
        now = time.monotonic()
        fade = self.cfg.streak_fade_s
        segments = []
        prev = pts[0]
        for cur in list(pts)[1:]:
            k = 1.0 - (now - cur[2]) / fade
            if k > 0:
                segments.append((prev[0] - ox, prev[1] - oy, cur[0] - ox, cur[1] - oy, min(k, 1.0)))
            prev = cur

        for color, width, alpha in self._passes:
            for x0, y0, x1, y1, k in segments:
                path = NSBezierPath.bezierPath()
                path.setLineCapStyle_(NSLineCapStyleRound)
                path.setLineJoinStyle_(NSLineJoinStyleRound)
                path.setLineWidth_(width * k)
                path.moveToPoint_((x0, y0))
                path.lineToPoint_((x1, y1))
                color.colorWithAlphaComponent_(alpha * k).setStroke()
                path.stroke()


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
