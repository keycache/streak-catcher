import os
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # hotkey (pynput vk codes)
    combo_vks: frozenset[int] = frozenset({59, 56, 58})  # L-Ctrl, L-Shift, L-Option

    # streak
    streak_fade_s: float = 0.50
    streak_sample_hz: int = 120
    streak_core_rgba: tuple = (1.0, 1.0, 1.0, 0.95)  # white core
    streak_glow_rgba: tuple = (0.35, 0.9, 1.0, 1.0)  # cyan glow
    streak_widths: tuple = (18.0, 8.0, 3.0)  # outer glow, inner glow, core

    # screen capture
    screen_capture_enabled: bool = True  # False: no screenshots are taken during sessions
    capture_interval_s: float = 0.5
    jpeg_quality: float = 0.85

    # audio (must match the asr module, provided later; importing it would load torch/NeMo)
    sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = 1600  # 100 ms
    # Input device: None = system default, otherwise a case-insensitive substring of the
    # device name (e.g. "MacBook Pro Microphone"). Device names are logged at session start.
    audio_input_device: str | None = None

    # output
    captures_dir: Path = Path(__file__).parent / "captures"
    # A session starting within this many seconds after the previous one stopped
    # continues in the same capture folder. 0 disables reuse.
    session_reuse_window_s: float = 10.0

    # caption
    caption_hide_delay_s: float = 2.0

    # debugging
    debug: bool = field(default_factory=lambda: os.environ.get("STREAK_DEBUG") == "1")

    def describe(self) -> str:
        return "\n".join(f"  {f.name} = {getattr(self, f.name)!r}" for f in fields(self))


CONFIG = Config()
