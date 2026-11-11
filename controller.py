import time

from AppKit import NSEvent
from Foundation import NSObject, NSTimer

from log import log
from streak import StreakOverlay

FLAG_SHIFT = 1 << 17
FLAG_CONTROL = 1 << 18
FLAG_OPTION = 1 << 19
COMBO_FLAGS = FLAG_SHIFT | FLAG_CONTROL | FLAG_OPTION

WATCHDOG_INTERVAL_S = 0.05


class WatchdogTimerTarget(NSObject):
    def initWithCallback_(self, callback):
        self = self.init()
        self.callback = callback
        return self

    def tick_(self, _timer):
        self.callback()


class SessionController:
    """Owns session start/stop. Main thread only."""

    def __init__(self, delegate, hotkey):
        self.delegate = delegate
        self.hotkey = hotkey
        self.active = False
        self.started_at = 0.0
        self.streak = StreakOverlay()
        self._watchdog = None
        self._timer_target = WatchdogTimerTarget.alloc().initWithCallback_(self._watchdog_tick)

    def start(self) -> None:
        if self.active:
            return
        self.active = True
        self.started_at = time.monotonic()
        log("session", "start")
        self.streak.start()
        self.delegate.set_active(True)
        self._watchdog = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            WATCHDOG_INTERVAL_S, self._timer_target, "tick:", None, True
        )

    def stop(self, reason: str = "released") -> None:
        if not self.active:
            return
        self.active = False
        if self._watchdog is not None:
            self._watchdog.invalidate()
            self._watchdog = None
        if reason == "quit":
            self.streak.close()
        else:
            self.streak.stop()
        self.delegate.set_active(False)
        log("session", f"stop ({reason}) after {time.monotonic() - self.started_at:.2f}s")

    def _watchdog_tick(self) -> None:
        flags = NSEvent.modifierFlags()
        if flags & COMBO_FLAGS != COMBO_FLAGS:
            log("watchdog", f"modifiers no longer held (flags=0x{flags:x}); forcing stop")
            self.hotkey.reset()
            self.stop("watchdog")
