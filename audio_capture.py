import threading
import time
import wave
from dataclasses import dataclass

import numpy as np
import pyaudio

from config import CONFIG
from log import log, log_exc

SILENCE_CHECK_CHUNKS = 10
RATE_CHECK_CHUNKS = 10  # check real-time delivery after 1 s of audio
SLOW_RATE_RATIO = 0.9


@dataclass(frozen=True)
class AudioChunk:
    session_id: str
    index: int  # 1-based, per segment
    samples: np.ndarray  # float32, mono, 16 kHz, values in [-1, 1], len chunk_size
    t_offset_s: float  # seconds since session start


def to_float(data: bytes) -> np.ndarray:
    """Same conversion as asr/main.py."""
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def wav_name(segment: int) -> str:
    return "audio.wav" if segment <= 1 else f"audio_{segment}.wav"


class AudioRecorder(threading.Thread):
    """Records the default mic for one session segment: WAV file + float chunks to `on_chunk`."""

    def __init__(self, session, on_chunk, config=CONFIG):
        super().__init__(name="audio-capture", daemon=True)
        self.session = session
        self.on_chunk = on_chunk
        self.cfg = config
        self.path = session.folder / wav_name(session.segment)
        self.stop_event = threading.Event()
        self.count = 0

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_event.set()
        self.join(timeout)
        if self.is_alive():
            log("audio", "WARNING audio thread did not stop within 2 s; leaving it (daemon)")

    def _pick_device(self, pa: pyaudio.PyAudio) -> dict:
        inputs = [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
        inputs = [d for d in inputs if d["maxInputChannels"] > 0]
        log("audio", "inputs: " + ", ".join(f"[{d['index']}] {d['name']}" for d in inputs), debug=True)
        want = self.cfg.audio_input_device
        if want:
            for d in inputs:
                if want.lower() in d["name"].lower():
                    return d
            log("audio", f"WARNING audio_input_device={want!r} not found; using system default. "
                         f"Available: {[d['name'] for d in inputs]}")
        return pa.get_default_input_device_info()

    def run(self) -> None:
        cfg = self.cfg
        # A fresh PyAudio per recording: PortAudio caches the device list at init,
        # so a long-lived instance would miss headsets connected after app start.
        pa = stream = wav = None
        all_zero = True
        try:
            pa = pyaudio.PyAudio()
            device = self._pick_device(pa)
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=cfg.channels,
                rate=cfg.sample_rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=cfg.chunk_size,
            )
            wav = wave.open(str(self.path), "wb")
            wav.setnchannels(cfg.channels)
            wav.setsampwidth(2)
            wav.setframerate(cfg.sample_rate)
            log("audio", f"recording from '{device['name']}' at {cfg.sample_rate} Hz -> {self.path.name}")
            t_first = None

            while not self.stop_event.is_set():
                data = stream.read(cfg.chunk_size, exception_on_overflow=False)
                if t_first is None:
                    t_first = time.monotonic()
                wav.writeframes(data)
                samples = to_float(data)
                self.count += 1
                if all_zero and np.any(samples):
                    all_zero = False
                if self.count == RATE_CHECK_CHUNKS + 1:
                    expected = RATE_CHECK_CHUNKS * cfg.chunk_size / cfg.sample_rate
                    ratio = expected / (time.monotonic() - t_first)
                    if ratio < SLOW_RATE_RATIO:
                        log("audio", f"WARNING '{device['name']}' delivers only {ratio:.2f}x the expected samples per "
                                     "second (seen with Bluetooth headset mics). Recordings will be shorter than "
                                     "the session. Pick another mic via config.audio_input_device or the system "
                                     "input setting.")
                if self.count == SILENCE_CHECK_CHUNKS and all_zero:
                    log("audio", "WARNING first 10 chunks are all zeros: microphone permission is probably "
                                 "denied for the host app (System Settings → Privacy & Security → Microphone)")
                chunk = AudioChunk(
                    session_id=self.session.id,
                    index=self.count,
                    samples=samples,
                    t_offset_s=time.monotonic() - self.session.started_at,
                )
                rms = float(np.sqrt(np.mean(samples**2)))
                self.on_chunk(chunk)
                log("audio", f"chunk {self.count} rms={rms:.4f}", debug=True)
        except Exception:
            log_exc("audio", "recording failed")
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    log_exc("audio", "closing stream failed")
            if wav is not None:
                wav.close()
            if pa is not None:
                pa.terminate()
            secs = self.count * cfg.chunk_size / cfg.sample_rate
            log("audio", f"stopped: {self.count} chunks ({secs:.1f}s) -> {self.path.name}")
