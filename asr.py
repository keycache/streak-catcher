"""Real-time speech recognition with cactus Whistle (in-process)."""
import os
import queue
import threading
import time

import numpy as np

from config import CONFIG
from log import log, log_exc
from processing import AudioProcessor

# cactus reads this at import time; must be set before the first `import cactus`.
os.environ["CACTUS_TELEMETRY"] = "1" if CONFIG.cactus_telemetry else "0"

_END = object()


class SpeechModel:
    """Loads Whistle on a background thread. on_state(state, detail) is called from that thread."""

    def __init__(self, on_state, config=CONFIG):
        self.cfg = config
        self.on_state = on_state
        self.model = None
        self.state = "loading"
        self.ready = threading.Event()
        self.failed = threading.Event()
        self._thread = threading.Thread(target=self._load, name="asr-load", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready.wait(0.1):
                return True
            if self.failed.is_set():
                return False
        return self.ready.is_set()

    def _load(self) -> None:
        self.on_state("loading", "")
        t0 = time.monotonic()
        try:
            from cactus.agent.whistle import Whistle

            model = Whistle()
            t_load = time.monotonic() - t0
            # Warm-up on low-level noise (pure silence is skipped without running the model).
            noise = (np.random.default_rng(0).standard_normal(self.cfg.sample_rate) * 0.01).astype(np.float32)
            model.transcribe(noise, language=self.cfg.asr_language)
            self.model = model
            self.state = "ready"
            self.ready.set()
            detail = f"load {t_load:.2f}s, warm-up {time.monotonic() - t0 - t_load:.2f}s"
            log("asr", f"speech model ready ({detail}) weights={model.weights}")
            self.on_state("ready", detail)
        except Exception as exc:
            self.state = "error"
            self.failed.set()
            log_exc("asr", "speech model failed to load")
            self.on_state("error", str(exc))


class CactusProcessor(AudioProcessor):
    """Streams each session segment through Whistle.stream() on its own thread.

    Results are posted with `post(text, is_final, pending, words, session)`; process_chunk/end_session never block.
    """

    def __init__(self, model: SpeechModel, config=CONFIG):
        self.model = model
        self.cfg = config
        self.post = None  # set by AudioProcessingWorker
        self._queue = None

    def start_session(self, session) -> None:
        self._queue = queue.Queue()
        threading.Thread(
            target=self._run, args=(session, self._queue), name="asr-stream", daemon=True
        ).start()

    def process_chunk(self, chunk) -> None:
        if self._queue is not None:
            self._queue.put(chunk)
        return None

    def end_session(self) -> None:
        if self._queue is not None:
            self._queue.put(_END)
            self._queue = None
        return None

    def _chunks(self, q, first_offset):
        """Regroup ~100 ms AudioChunks into asr_chunk_s float32 buffers until _END."""
        target = int(self.cfg.asr_chunk_s * self.cfg.sample_rate)
        buf, size = [], 0
        while True:
            item = q.get()
            if item is _END:
                if size:
                    yield np.concatenate(buf)
                return
            if first_offset[0] is None:
                first_offset[0] = item.t_offset_s - len(item.samples) / self.cfg.sample_rate
            buf.append(item.samples)
            size += len(item.samples)
            if size >= target:
                yield np.concatenate(buf)
                buf, size = [], 0

    def _run(self, session, q) -> None:
        sid = session.id
        if not self.model.ready.is_set():
            log("asr", f"session {sid}: waiting for speech model (queued audio)")
            if not self.model.wait(self.cfg.model_wait_timeout_s):
                log("asr", f"ERROR session {sid}: speech model not ready after "
                           f"{self.cfg.model_wait_timeout_s:.0f}s; audio not transcribed")
                self.post("", True, "", [], session)
                return
        committed: list[str] = []
        words: list[dict] = []
        first_offset = [None]  # session time of the segment's first sample
        t0 = time.monotonic()
        try:
            for step in self.model.model.stream(
                self._chunks(q, first_offset),
                language=self.cfg.asr_language,
                keywords=list(self.cfg.asr_keywords) or None,
            ):
                if step.get("text"):
                    committed.append(step["text"])
                base = first_offset[0] or 0.0
                for w in step.get("words") or []:
                    words.append({"word": w["word"], "start": round(base + w["start"], 3),
                                  "end": round(base + w["end"], 3), "p": round(w.get("probability", 0.0), 3)})
                log("asr", f"pass {step.get('pass_ms', 0):.0f}ms recv={step.get('received', 0):.1f}s "
                           f"text={step.get('text')!r} pending={step.get('pending')!r}", debug=True)
                self.post(" ".join(committed), False, step.get("pending") or "", None, session)
        except Exception:
            log_exc("asr", f"session {sid}: stream failed")
        text = " ".join(committed).strip()
        log("asr", f"session {sid}: final ({len(words)} words, {time.monotonic() - t0:.2f}s stream): {text!r}")
        self.post(text, True, "", words, session)
