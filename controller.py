import time

from AppKit import NSEvent
from Foundation import NSObject, NSTimer

from config import CONFIG
from log import log, log_exc
from audio_capture import AudioRecorder
from processing import AudioProcessingWorker, AudioProcessor, ImageProcessingWorker
from screen_capture import ScreenCapturer
from session import create_session
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

    def __init__(self, delegate, hotkey, config=CONFIG):
        self.delegate = delegate
        self.hotkey = hotkey
        self.cfg = config
        self.active = False
        self.session = None
        self.last_session = None
        self.streak = StreakOverlay(config)
        self.image_worker = ImageProcessingWorker()
        self.image_worker.start()
        self.capturer = None
        self.audio_worker = AudioProcessingWorker(AudioProcessor(), self.on_transcript)
        self.audio_worker.start()
        self.recorder = None
        self.transcript: list[str] = []
        self._watchdog = None
        self._timer_target = WatchdogTimerTarget.alloc().initWithCallback_(self._watchdog_tick)

    def start(self) -> None:
        if self.active:
            return
        self.active = True
        self.session = create_session(self.cfg.captures_dir, self.last_session, self.cfg.session_reuse_window_s)
        s = self.session
        how = f"continuing (segment {s.segment}, from frame {s.frame_count + 1})" if s.segment > 1 else "new"
        log("session", f"start {s.id} {how} display={s.display_id} folder={s.folder}")
        try:
            self.streak.start()
        except Exception:
            log_exc("session", "streak failed to start")
        self.transcript = []
        try:
            self.audio_worker.start_session(s)
            self.recorder = AudioRecorder(s, self.audio_worker.submit, self.cfg)
            self.recorder.start()
        except Exception:
            log_exc("session", "audio failed to start")
        try:
            if self.cfg.screen_capture_enabled:
                self.capturer = ScreenCapturer(self.session, self.image_worker.submit, self.cfg)
                self.capturer.start()
            else:
                log("session", "screen capture disabled (config.screen_capture_enabled=False)")
        except Exception:
            log_exc("session", "screen capture failed to start")
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
        frames = 0
        if self.capturer is not None:
            self.capturer.stop()
            frames = self.capturer.count
            self.capturer = None
        chunks = 0
        if self.recorder is not None:
            self.recorder.stop()
            chunks = self.recorder.count
            self.recorder = None
            self.audio_worker.end_session()
        if reason == "quit":
            self.streak.close()
        else:
            self.streak.stop()
        self.delegate.set_active(False)
        s = self.session
        s.frame_count += frames
        s.ended_at = time.monotonic()
        self.last_session, self.session = s, None
        log("session", f"stop {s.id} ({reason}): segment {s.segment} {s.ended_at - s.segment_started_at:.2f}s, "
                       f"{frames} frames ({s.frame_count} in folder), {chunks} audio chunks, folder={s.folder}")

    def on_transcript(self, text: str, is_final: bool) -> None:
        """Text from the audio processor (main thread). Caption display comes in Phase 6."""
        log("transcr", f"{'final' if is_final else 'partial'}: {text}")
        if is_final:
            self.transcript.append(text)
            self.delegate.last_transcript = " ".join(self.transcript)

    def shutdown(self) -> None:
        self.stop("quit")
        self.image_worker.stop()
        self.audio_worker.stop()

    def _watchdog_tick(self) -> None:
        flags = NSEvent.modifierFlags()
        if flags & COMBO_FLAGS != COMBO_FLAGS:
            log("watchdog", f"modifiers no longer held (flags=0x{flags:x}); forcing stop")
            self.hotkey.reset()
            self.stop("watchdog")
