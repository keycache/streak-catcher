import queue
import threading

from log import log, log_exc


def process_image(frame) -> None:
    """Post-process a single captured screenshot. Intentionally a no-op for now."""
    return None


class ImageProcessingWorker(threading.Thread):
    """Hands captured frames to process_image on a background thread."""

    _STOP = object()

    def __init__(self, maxsize: int = 64):
        super().__init__(name="image-worker", daemon=True)
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self.processed = 0

    def submit(self, frame) -> None:
        while True:
            try:
                self.queue.put_nowait(frame)
                return
            except queue.Full:
                try:
                    dropped = self.queue.get_nowait()
                    log("image", f"queue full; dropped frame {getattr(dropped, 'index', '?')}")
                except queue.Empty:
                    pass

    def stop(self, timeout: float = 2.0) -> None:
        self.submit(self._STOP)
        self.join(timeout)

    def run(self) -> None:
        while True:
            frame = self.queue.get()
            if frame is self._STOP:
                break
            try:
                process_image(frame)
                self.processed += 1
                log("image", f"processed frame {frame.index} ({frame.path.name}) qsize={self.queue.qsize()}")
            except Exception:
                log_exc("image", f"process_image failed for frame {getattr(frame, 'index', '?')}")
        log("image", f"worker stopped ({self.processed} frames processed)")
