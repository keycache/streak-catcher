import sys
import threading
import traceback
from datetime import datetime

from config import CONFIG


def log(component: str, msg: str, *, debug: bool = False) -> None:
    if debug and not CONFIG.debug:
        return
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    thread = threading.current_thread().name
    print(f"[{ts}] [{thread:<14.14}] [{component:<7.7}] {msg}", flush=True)


def log_exc(component: str, msg: str) -> None:
    log(component, f"ERROR {msg}\n{traceback.format_exc()}")
    sys.stdout.flush()
