import os

import Quartz
from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

from log import log

_HOSTS = {
    "vscode": "Visual Studio Code",
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
}


def host_app() -> str:
    term = os.environ.get("TERM_PROGRAM", "")
    return _HOSTS.get(term, term or "your terminal app")


def check_accessibility(prompt: bool = True) -> bool:
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))


def check_screen_recording(request: bool = True) -> bool:
    if Quartz.CGPreflightScreenCaptureAccess():
        return True
    if request:
        Quartz.CGRequestScreenCaptureAccess()
    return False


def report() -> dict[str, bool]:
    host = host_app()
    status = {
        "accessibility": check_accessibility(),
        "screen_recording": check_screen_recording(),
    }
    log("perm", f"host app: {host}")

    if status["accessibility"]:
        log("perm", "Accessibility: granted")
    else:
        log("perm", f"WARNING Accessibility: NOT granted. Hotkey won't work. Enable {host} in "
                    "System Settings → Privacy & Security → Accessibility (and Input Monitoring), then restart.")

    if status["screen_recording"]:
        log("perm", "Screen Recording: granted")
    else:
        log("perm", f"WARNING Screen Recording: NOT granted. Screenshots will lack window content. Enable {host} "
                    "in System Settings → Privacy & Security → Screen & System Audio Recording, then restart.")

    log("perm", "Microphone: macOS will prompt on first recording. If audio is silent, enable "
                f"{host} in System Settings → Privacy & Security → Microphone.")
    return status
