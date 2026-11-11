import json
import os
import time

from AppKit import NSEvent, NSWorkspace, NSWorkspaceDidActivateApplicationNotification
from Foundation import NSObject, NSTimer

from PyObjCTools import AppHelper

from config import CONFIG
from log import log, log_exc
from audio_capture import AudioRecorder
from assembly import Assembler
from caption import CaptionPanel
from collections import deque
import gesture_debug
from gestures import GestureDetector
from processing import AudioProcessingWorker, AudioProcessor, FakeAudioProcessor, ImageProcessingWorker
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


class AppActivationObserver(NSObject):
    def initWithCallback_(self, callback):
        self = self.init()
        self.callback = callback
        return self

    def appActivated_(self, note):
        app = note.userInfo()["NSWorkspaceApplicationKey"]
        self.callback(str(app.localizedName() or ""), int(app.processIdentifier()))


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
        self.detector = None
        self.streak.on_sample = self._on_sample
        self.speech = None
        if config.fake_transcript:
            processor = FakeAudioProcessor()
            delegate.set_model_status("Speech: fake transcript (config)")
        else:
            from asr import CactusProcessor, SpeechModel

            self.speech = SpeechModel(self._on_model_state, config)
            processor = CactusProcessor(self.speech, config)
        self.audio_worker = AudioProcessingWorker(processor, self.on_transcript)
        self.caption = CaptionPanel()
        self.audio_worker.start()
        self.recorder = None
        self.partial_text: dict[str, str] = {}  # session id -> committed text so far (assembly timeout fallback)
        self._assemblers: dict[str, deque] = {}  # session id -> assemblers waiting for their final transcript
        self.last_request = None
        self._first_gesture = 1
        self._watchdog = None
        self._timer_target = WatchdogTimerTarget.alloc().initWithCallback_(self._watchdog_tick)
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        self.current_app = str(front.localizedName()) if front is not None else ""
        self._app_observer = AppActivationObserver.alloc().initWithCallback_(self._on_app_activated)
        NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self._app_observer, "appActivated:", NSWorkspaceDidActivateApplicationNotification, None
        )
        if self.speech is not None:
            delegate.set_model_status("Loading speech model…")
            self.speech.start()

    def _on_model_state(self, state: str, detail: str) -> None:
        """Called on the model-loading thread."""
        text = {"loading": "Loading speech model…", "ready": "Speech model ready",
                "error": "Speech model unavailable"}[state]
        AppHelper.callAfter(self.delegate.set_model_status, text)

    def start(self) -> None:
        if self.active:
            return
        self.active = True
        self.session = create_session(self.cfg.captures_dir, self.last_session, self.cfg.session_reuse_window_s)
        s = self.session
        how = f"continuing (segment {s.segment}, from frame {s.frame_count + 1})" if s.segment > 1 else "new"
        log("session", f"start {s.id} {how} display={s.display_id} folder={s.folder}")
        self._first_gesture = len(s.gestures) + 1
        self.detector = GestureDetector(self._on_gesture, first_index=self._first_gesture, config=self.cfg)
        try:
            self.streak.start()
        except Exception:
            log_exc("session", "streak failed to start")
        try:
            self.caption.show(s.visible_frame, "Listening…")
        except Exception:
            log_exc("session", "caption failed to show")
        try:
            self.audio_worker.start_session(s)
            self.recorder = AudioRecorder(s, self.audio_worker.submit, self.cfg)
            self.recorder.start()
        except Exception:
            log_exc("session", "audio failed to start")
        try:
            if self.cfg.screen_capture_enabled:
                self.capturer = ScreenCapturer(
                    self.session, self._on_frame, self.cfg,
                    current_app=lambda: self.current_app,
                    protect_from=lambda: self.detector.stroke_start if self.detector else None,
                )
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
        if self.detector is not None:  # first, so gestures closed by release still get their frames
            try:
                self.detector.finish()
            except Exception:
                log_exc("gesture", "gesture detection failed at stop")
            self.detector = None
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
            self.caption.close()
        else:
            self.streak.stop()
            self.caption.hide_after(self.cfg.caption_hide_delay_s)
        self.delegate.set_active(False)
        s = self.session
        s.frame_count += frames
        s.ended_at = time.monotonic()
        self._write_path(s)
        self._write_frames(s)
        if self.cfg.gesture_debug_images and s.gestures:
            gesture_debug.render_async(s, s.frames, self.cfg.jpeg_quality)
        if reason != "quit":
            seg = {"segment": s.segment, "first_gesture": self._first_gesture,
                   "t0": s.segment_started_at - s.started_at, "t1": s.ended_at - s.started_at + 1.0}
            asm = Assembler(self, s, seg, self.cfg)
            self._assemblers.setdefault(s.id, deque()).append(asm)
            asm.start()
        self.last_session, self.session = s, None
        log("session", f"stop {s.id} ({reason}): segment {s.segment} {s.ended_at - s.segment_started_at:.2f}s, "
                       f"{frames} frames ({s.frame_count} in folder), {chunks} audio chunks, {len(s.gestures)} gestures, "
                       f"folder={s.folder}")

    def _on_frame(self, frame) -> None:
        """Capture thread: a frame was kept; hand it to the image worker."""
        self.image_worker.submit(frame)

    def _on_app_activated(self, name: str, pid: int) -> None:
        """Main thread: the frontmost app changed."""
        if pid == os.getpid():
            return  # our own alerts/menus
        self.current_app = name
        if self.session is not None and self.capturer is not None:
            t = time.monotonic() - self.session.started_at
            log("capture", f"app switch -> {name!r} at t={t:.2f}s")
            self.capturer.request_app_switch(t, name)

    def _write_frames(self, s) -> None:
        try:
            frames = [{k: v for k, v in r.items() if k not in ("thumb", "path")} for r in s.frames]
            (s.folder / "frames.json").write_text(json.dumps({"session": s.id, "frames": frames}, indent=1))
        except Exception:
            log_exc("capture", "writing frames.json failed")

    def _on_sample(self, x: float, y: float, now: float) -> None:
        """Every streak sample (main thread, ~120 Hz) while a session is active."""
        s = self.session
        if s is None or self.detector is None:
            return
        t = now - s.started_at
        s.path.append((round(t, 4), round(x, 1), round(y, 1)))
        try:
            self.detector.feed(t, x, y)
        except Exception:
            log_exc("gesture", "gesture detection failed")
            self.detector = None

    def _on_gesture(self, g) -> None:
        s = self.session or self.last_session
        if s is not None:
            s.gestures.append(g)
        x, y, w, h = g.bbox
        log("gesture", f"#{g.index} {g.kind} t={g.t_start:.2f}-{g.t_end:.2f}s bbox=({x:.0f},{y:.0f}) {w:.0f}x{h:.0f}")
        if self.capturer is not None:
            self.capturer.request_gesture(g)

    def _write_path(self, s) -> None:
        try:
            (s.folder / "path.json").write_text(json.dumps({
                "session": s.id,
                "coords": "global Cocoa points (origin bottom-left of primary screen, y up); t = s since session start",
                "samples": s.path,
                "gestures": [g.to_dict() for g in s.gestures],
            }))
        except Exception:
            log_exc("gesture", "writing path.json failed")

    def on_transcript(self, text: str, is_final: bool, pending: str = "", words=None, session=None) -> None:
        """Text from the audio processor (main thread)."""
        log("transcr", f"{'final' if is_final else 'partial'}: {text}" + (f" […{pending}]" if pending else ""),
            debug=not is_final)
        stale = session is not None and self.session is not None and session is not self.session
        s = session or self.session or self.last_session
        if not is_final:
            if s is not None:
                self.partial_text[s.id] = text
            if not stale:
                self.caption.set_parts(text, pending)
            return
        if not stale:  # don't overwrite a newer session's caption
            self.caption.set_text(text or "No speech detected")
        if s is not None and self._assemblers.get(s.id):
            self._assemblers[s.id].popleft().deliver(text, words)
        if s is not None and words is not None:
            s.words.extend(words)
            s.transcript_text = " ".join(t for t in (s.transcript_text, text) if t)
            try:
                (s.folder / "transcript.json").write_text(json.dumps(
                    {"session": s.id, "text": s.transcript_text, "words": s.words}, indent=1))
            except Exception:
                log_exc("transcr", "writing transcript.json failed")

    def on_request(self, request: dict) -> None:
        """Main thread: an assembled request for one released segment (sent to the LLM in Phase 12)."""
        self.last_request = request
        if request["status"] == "no_speech":
            log("assembly", "gestures but no instructions heard; nothing to send")
            if self.session is None:
                s = self.last_session
                self.caption.show(s.visible_frame, "No instructions heard")
                self.caption.hide_after(self.cfg.no_instructions_caption_s)
            return
        if self.session is None:  # re-hide after a "Transcribing…" status
            self.caption.hide_after(self.cfg.caption_hide_delay_s)
        if request["status"] == "empty":
            return
        self.delegate.last_transcript = request["transcript_with_markers"]
        log("assembly", f"request ready: {len(request['images'])} image(s): {request['transcript_with_markers']!r}")

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
