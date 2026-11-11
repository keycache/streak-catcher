import queue
import threading

from PyObjCTools import AppHelper

from log import log, log_exc

_STOP = object()


class _BoundedWorker(threading.Thread):
    """Daemon thread draining a bounded queue. When full, the oldest item is dropped."""

    def __init__(self, name: str, maxsize: int):
        super().__init__(name=name, daemon=True)
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)

    def _put(self, item, describe) -> None:
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    dropped = self.queue.get_nowait()
                    log(self.name, f"queue full; dropped {describe(dropped)}")
                except queue.Empty:
                    pass

    def stop(self, timeout: float = 2.0) -> None:
        self._put(_STOP, lambda _: "item")
        self.join(timeout)


# --- image post-processing ---------------------------------------------------


def process_image(frame) -> None:
    """Post-process a single captured screenshot. Intentionally a no-op for now."""
    return None


class ImageProcessingWorker(_BoundedWorker):
    def __init__(self, maxsize: int = 64):
        super().__init__("image-worker", maxsize)
        self.processed = 0

    def submit(self, frame) -> None:
        self._put(frame, lambda f: f"frame {getattr(f, 'index', '?')}")

    def run(self) -> None:
        while (frame := self.queue.get()) is not _STOP:
            try:
                process_image(frame)
                self.processed += 1
                log("image", f"processed frame {frame.index} ({frame.path.name}) qsize={self.queue.qsize()}")
            except Exception:
                log_exc("image", f"process_image failed for frame {getattr(frame, 'index', '?')}")
        log("image", f"worker stopped ({self.processed} frames processed)")


# --- real-time audio processing ----------------------------------------------


class AudioProcessor:
    """Real-time audio processing stub; replaced by a cactus Whistle processor in Phase 8."""

    def start_session(self, session) -> None:
        return None

    def process_chunk(self, chunk) -> str | None:
        return None  # later: partial/final transcript text

    def end_session(self) -> str | None:
        return None  # later: final flush of buffered utterance


class FakeAudioProcessor(AudioProcessor):
    """Emits a growing placeholder sentence: one word per 0.5 s of audio, final text at session end."""

    WORDS = "the quick brown fox jumps over the lazy dog while the streak catcher keeps listening".split()

    def start_session(self, session) -> None:
        self.n = 0
        self.words: list[str] = []

    def process_chunk(self, chunk) -> str | None:
        self.n += 1
        if self.n % 5:
            return None
        self.words.append(self.WORDS[len(self.words) % len(self.WORDS)])
        return " ".join(self.words) + " …"

    def end_session(self) -> str | None:
        return " ".join(self.words) + "." if self.words else None


class AudioProcessingWorker(_BoundedWorker):
    """Feeds ("start", session) / ("chunk", AudioChunk) / ("end", None) to an AudioProcessor.

    Any text is posted to the main thread as on_transcript(text, is_final).
    """

    def __init__(self, processor: AudioProcessor, on_transcript, maxsize: int = 100):
        super().__init__("audio-worker", maxsize)
        self.processor = processor
        self.on_transcript = on_transcript
        self.chunks = 0

    def _put_control(self, msg) -> None:
        # Control messages are never dropped; wait briefly for space instead.
        try:
            self.queue.put(msg, timeout=1.0)
        except queue.Full:
            log("audio-w", f"ERROR queue still full; lost {_describe(msg)}")

    def start_session(self, session) -> None:
        self._put_control(("start", session))

    def submit(self, chunk) -> None:
        self._put(("chunk", chunk), _describe)

    def end_session(self) -> None:
        self._put_control(("end", None))

    def run(self) -> None:
        while (msg := self.queue.get()) is not _STOP:
            kind, payload = msg
            try:
                if kind == "start":
                    self.chunks = 0
                    self.processor.start_session(payload)
                    log("audio-w", f"session {payload.id} started")
                elif kind == "chunk":
                    self.chunks += 1
                    text = self.processor.process_chunk(payload)
                    log("audio-w", f"chunk {payload.index} qsize={self.queue.qsize()}", debug=True)
                    if text:
                        AppHelper.callAfter(self.on_transcript, text, False)
                elif kind == "end":
                    text = self.processor.end_session()
                    log("audio-w", f"session ended ({self.chunks} chunks processed)")
                    if text:
                        AppHelper.callAfter(self.on_transcript, text, True)
            except Exception:
                log_exc("audio-w", f"audio processor failed on {kind}")
        log("audio-w", "worker stopped")


def _describe(msg) -> str:
    kind, payload = msg
    return f"audio chunk {payload.index}" if kind == "chunk" else f"'{kind}' message"
