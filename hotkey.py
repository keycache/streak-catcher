import threading

from pynput import keyboard
from PyObjCTools import AppHelper

from log import log


def _vk(key) -> int | None:
    vk = getattr(key, "vk", None)
    if vk is None:
        value = getattr(key, "value", None)
        vk = getattr(value, "vk", None)
    return vk


class HotkeyListener:
    """Edge-triggered detector for a set of held key codes.

    Callbacks are dispatched to the main thread with AppHelper.callAfter.
    """

    def __init__(self, combo_vks, on_activate, on_deactivate):
        self.combo_vks = frozenset(combo_vks)
        self.on_activate = on_activate
        self.on_deactivate = on_deactivate
        self.enabled = True
        self.held: set[int] = set()
        self.active = False
        self._lock = threading.Lock()
        self._listener = None

    def start(self) -> None:
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release, suppress=False)
        self._listener.name = "pynput-keyboard"
        self._listener.start()
        log("hotkey", f"listening for vk {sorted(self.combo_vks)}")

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
            log("hotkey", "listener stopped")

    def reset(self) -> None:
        """Forget held keys and the active state (after a forced stop)."""
        with self._lock:
            self.held.clear()
            self.active = False

    def _on_press(self, key) -> None:
        vk = _vk(key)
        log("hotkey", f"press vk={vk} key={key}", debug=True)
        if vk not in self.combo_vks:
            return
        with self._lock:
            self.held.add(vk)
            fire = self.enabled and not self.active and self.held == self.combo_vks
            if fire:
                self.active = True
            held = sorted(self.held)
        log("hotkey", f"held={held}", debug=True)
        if fire:
            log("hotkey", f"activate (held={held})")
            AppHelper.callAfter(self.on_activate)

    def _on_release(self, key) -> None:
        vk = _vk(key)
        log("hotkey", f"release vk={vk} key={key}", debug=True)
        if vk not in self.combo_vks:
            return
        with self._lock:
            self.held.discard(vk)
            fire = self.active and self.held != self.combo_vks
            if fire:
                self.active = False
            held = sorted(self.held)
        log("hotkey", f"held={held}", debug=True)
        if fire:
            log("hotkey", f"deactivate (released vk={vk}, held={held})")
            AppHelper.callAfter(self.on_deactivate)
