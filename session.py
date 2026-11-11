import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from AppKit import NSEvent, NSMouseInRect, NSScreen


@dataclass
class Session:
    id: str  # "20261110-203912"
    folder: Path  # captures/<id>/
    started_at: float  # time.monotonic() of the first segment; frame t_offset_s is relative to this
    started_wall: datetime
    display_id: int  # CGDirectDisplayID where the current segment's combo started
    screen_frame: object  # NSRect (Cocoa frame of that screen)
    visible_frame: object  # NSRect excluding menu bar / Dock (caption placement)
    frame_count: int = 0  # frames saved in the folder so far (across segments)
    segment: int = 1  # 1 for a new folder, 2+ when the folder is reused
    segment_started_at: float = 0.0
    ended_at: float | None = None  # time.monotonic() when the latest segment stopped


def screen_under_mouse():
    p = NSEvent.mouseLocation()
    for s in NSScreen.screens():
        if NSMouseInRect(p, s.frame(), False):
            return s
    return NSScreen.mainScreen()


def create_session(captures_dir: Path, previous: Session | None = None, reuse_window_s: float = 0.0) -> Session:
    """Start a session on the screen under the mouse. Main thread only.

    Continues `previous` (same folder, frame numbering carries on) if it stopped
    no more than `reuse_window_s` seconds ago and its folder still exists.
    """
    screen = screen_under_mouse()
    now = time.monotonic()
    display_id = int(screen.deviceDescription()["NSScreenNumber"])

    if (
        previous is not None
        and previous.ended_at is not None
        and reuse_window_s > 0
        and now - previous.ended_at <= reuse_window_s
        and previous.folder.is_dir()
    ):
        previous.segment += 1
        previous.segment_started_at = now
        previous.ended_at = None
        previous.display_id = display_id
        previous.screen_frame = screen.frame()
        previous.visible_frame = screen.visibleFrame()
        return previous

    wall = datetime.now()
    base = wall.strftime("%Y%m%d-%H%M%S")
    sid, n = base, 1
    while (captures_dir / sid).exists():  # two new folders within the same second
        n += 1
        sid = f"{base}-{n}"
    folder = captures_dir / sid
    folder.mkdir(parents=True)
    return Session(
        id=sid,
        folder=folder,
        started_at=now,
        started_wall=wall,
        display_id=display_id,
        screen_frame=screen.frame(),
        visible_frame=screen.visibleFrame(),
        segment_started_at=now,
    )
