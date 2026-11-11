# Streak Catcher: Technical Specification

Companion to [PLAN.md](./PLAN.md). This document describes how each part is built.

---

## Working rules (apply to every request on this project)

- If there are issues and the app still doesn't work after multiple changes, add `print` statements to debug the underlying issue. Include the necessary information: thread name, timestamps, key codes, display IDs, file paths, queue sizes, permission state, and exceptions with tracebacks.
- Do **not** create summary files when a phase is completed.
- The **only** two Markdown files are `PLAN.md` and `SPECIFICATIONS.md`.
- All other files created are Python code files.

---

## 1. Environment

| Item | Value |
|---|---|
| OS | macOS 26.6.2 (Apple Silicon). Minimum supported: macOS 13 |
| Python | 3.14 (root uv venv at `./.venv`) |
| Run | `.venv/bin/python main.py` (from the repo root) |
| Dependencies (root `pyproject.toml`) | `pyaudio`, `pynput` (1.8.x), `pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz` (13.x), `numpy` |
| Not imported at startup | `torch`, `nemo` (`asr/main.py`). These are only loaded when real STT is added (Phase 8) |

Dependencies are added with `uv add --no-sync` plus `uv pip install`. A plain `uv sync` would remove torch and NeMo, which are installed in the venv but not declared in `pyproject.toml`.

Verified on this machine:

- `Quartz.CGDisplayCreateImage(displayID)` works and returns a 3456 px wide image in about 140 ms.
- `NSScreen.deviceDescription()["NSScreenNumber"]` equals the `CGDirectDisplayID`.
- pynput virtual key codes: Left Ctrl **59** (0x3B), Left Shift **56** (0x38), Left Option **58** (0x3A), Right Ctrl 62. pynput's `Key.ctrl_l` is an alias of `Key.ctrl`, so the code compares **key codes (`vk`)**, not `Key` members.

## 2. Project layout

All modules live directly in the repo root (no `streak_catcher/` package folder).

```
./ (repo root)
├── PLAN.md
├── SPECIFICATIONS.md
├── pyproject.toml
├── main.py              # entry point: python main.py
├── config.py            # all constants (frozen dataclass)
├── log.py               # timestamped, thread-aware print helper
├── app.py               # NSApplication, AppDelegate, status item + menu
├── permissions.py       # Accessibility / Screen Recording / Mic checks
├── hotkey.py            # pynput listener -> activate/deactivate edges
├── controller.py        # SessionController: start/stop orchestration
├── session.py           # Session dataclass + folder creation
├── streak.py            # Trail model, overlay windows, StreakView
├── screen_capture.py    # ScreenCapturer thread (CG capture + JPEG)
├── audio_capture.py     # AudioRecorder thread (PyAudio + WAV)
├── processing.py        # ImageProcessingWorker, AudioProcessingWorker, stubs
├── caption.py           # CaptionPanel (transcript display)
└── captures/            # runtime output (git-ignored)
```

## 3. Configuration (`config.py`)

```python
@dataclass(frozen=True)
class Config:
    # hotkey (pynput vk codes)
    combo_vks: frozenset[int] = frozenset({59, 56, 58})   # L-Ctrl, L-Shift, L-Option

    # streak
    streak_fade_s: float = 0.25
    streak_sample_hz: int = 120
    streak_core_rgba: tuple = (1.0, 1.0, 1.0, 0.95)       # white core
    streak_glow_rgba: tuple = (0.35, 0.9, 1.0, 1.0)       # cyan glow
    streak_widths: tuple = (18.0, 8.0, 3.0)               # outer glow, inner glow, core

    # screen capture
    capture_interval_s: float = 0.5
    jpeg_quality: float = 0.85

    # audio (must match asr/main.py)
    sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = 1600                                 # 100 ms

    # output
    captures_dir: Path = Path(__file__).parent / "captures"

    # caption
    caption_hide_delay_s: float = 2.0

    # debugging
    debug: bool = os.environ.get("STREAK_DEBUG") == "1"
```

The audio values are **copied** from `asr/main.py` (`SAMPLE_RATE`, `CHUNK_SIZE`) instead of imported, because importing that module loads torch and NeMo, which takes seconds and gigabytes of memory.

## 4. Threading model

| Thread | Owner | Responsibility |
|---|---|---|
| **Main** (AppKit run loop) | `app.py` | Status bar, menu, overlay windows, `StreakView` drawing, caption panel, 120 Hz streak timer, modifier watchdog, all session state changes |
| `pynput-keyboard` | `hotkey.py` | Event tap. Tracks the held key codes and posts edges to the main thread with `AppHelper.callAfter` |
| `screen-capture` | `screen_capture.py` | Captures every 500 ms, encodes JPEG, enqueues `CapturedFrame` |
| `image-worker` | `processing.py` | Takes frames off the queue and calls `process_image(frame)` |
| `audio-capture` | `audio_capture.py` | Blocking `stream.read`, writes WAV, enqueues `AudioChunk` |
| `audio-worker` | `processing.py` | Takes chunks off the queue, calls `AudioProcessor.process_chunk`, and sends any text to the main thread |

Rules:

- **Only the main thread touches AppKit objects.** Background threads use `PyObjCTools.AppHelper.callAfter(fn, *args)`.
- `SessionController.start()` and `stop()` run only on the main thread, so they need no locks.
- Background threads stop through a `threading.Event` and are joined with a timeout of 2 s. If a join times out, the thread is logged and left alone (daemon).
- The workers are long-lived (one per app run). The capture and recorder threads are created per session.

## 5. Hotkey detection (`hotkey.py`)

```python
class HotkeyListener:
    def __init__(self, combo_vks, on_activate, on_deactivate): ...
    def start(self) -> None   # starts pynput.keyboard.Listener
    def stop(self) -> None
    enabled: bool             # toggled from the menu
```

- `_vk(key)`: returns `key.vk` for a `KeyCode`, or `key.value.vk` for a `Key`. Returns `None` if neither exists.
- `on_press`: adds the code to `held` if it's one of `combo_vks`. If `held == combo_vks` and the combo isn't already active, sets `active = True` and calls `callAfter(on_activate)`.
- `on_release`: removes the code. If the combo was active and `held` is no longer complete, sets `active = False` and calls `callAfter(on_deactivate)`.
- Other modifiers (Cmd, right-side keys) and other keys are **ignored**. They neither block nor cancel the combo.
- The listener never suppresses events (`suppress=False`).
- When `enabled` is False, the listener still tracks `held` but doesn't fire callbacks.

**Watchdog (main thread):** on every streak timer tick during a session, check `NSEvent.modifierFlags()`. If any of `NSEventModifierFlagControl` (1<<18), `NSEventModifierFlagShift` (1<<17), or `NSEventModifierFlagOption` (1<<19) is missing, call `controller.stop(reason="watchdog")` and clear `held` in the listener. This recovers from releases that were missed (for example, during Secure Input).

## 6. Session lifecycle (`controller.py`, `session.py`)

```python
@dataclass
class Session:
    id: str                    # "20261110-203912"
    folder: Path               # captures/<id>/
    started_at: float          # time.monotonic()
    started_wall: datetime
    display_id: int            # CGDirectDisplayID where the combo started
    screen_frame: NSRect       # Cocoa frame of that screen
```

**`start()`** (main thread; does nothing if a session is already active):

1. Find the screen under `NSEvent.mouseLocation()`: the first `NSScreen` where `NSMouseInRect(p, s.frame(), False)`, falling back to `NSScreen.mainScreen()`.
2. Create the `Session` and `mkdir -p` its folder.
3. Start the `StreakOverlay`.
4. Show the `CaptionPanel` with "Listening…" on that screen.
5. Call `audio_processor.start_session(session)` through the audio worker.
6. Start the `AudioRecorder`.
7. Start the `ScreenCapturer`.
8. Change the status item icon to the "active" symbol.

**`stop(reason)`** (main thread; does nothing if idle):

1. Stop the `ScreenCapturer` (signal, then join).
2. Stop the `AudioRecorder` (signal, join, close the stream, finalize the WAV).
3. Send an end-of-session marker to the audio worker, which calls `end_session()` and posts any final text.
4. Let the streak fade out; the overlay windows close once the trail is empty.
5. Hide the caption panel after `caption_hide_delay_s`.
6. Restore the idle icon. Log a summary: duration, frame count, chunk count, folder.

If a subsystem fails to start, the exception and traceback are logged and the other subsystems keep running (Phase 7).

## 7. Light streak (`streak.py`)

**Coordinates:** global Cocoa coordinates (origin at the bottom-left of the primary screen, y pointing up). `NSEvent.mouseLocation()` already uses them. Each view converts a point to local coordinates by subtracting `window.frame().origin`.

**`Trail`:**
```python
class Trail:
    points: deque[tuple[float, float, float]]   # (x, y, t_monotonic)
    def add(x, y, t)        # skip if within 0.5 px of the last point
    def prune(now)          # drop points older than fade_s
    def is_empty() -> bool
```

**Overlay windows** (one per `NSScreen`, created at session start):

- `NSWindow` with style `NSWindowStyleMaskBorderless`, `NSBackingStoreBuffered`, frame = `screen.frame()`.
- `setOpaque_(False)`, `setBackgroundColor_(NSColor.clearColor())`, `setHasShadow_(False)`.
- `setIgnoresMouseEvents_(True)`: clicks pass through.
- `setLevel_(NSScreenSaverWindowLevel)`.
- `setCollectionBehavior_(CanJoinAllSpaces | Stationary | FullScreenAuxiliary | IgnoresCycle)`.
- `setReleasedWhenClosed_(False)`. Python keeps references until the window closes.
- `sharingType` stays at its default, so the streak **appears in screenshots** (as decided).
- Shown with `orderFrontRegardless()`; the app doesn't become active.

**`StreakView.drawRect_`:**

- For each pair of consecutive points `(p0, p1)`: `age = now - p1.t` and `k = 1 - age / fade_s` (clamped to the 0..1 range).
- Three passes, each drawn as an `NSBezierPath` with a round line cap and round joins:
  1. Outer glow: cyan, width `18 * k`, alpha `0.15 * k`.
  2. Inner glow: cyan, width `8 * k`, alpha `0.40 * k`.
  3. Core: white, width `3 * k`, alpha `0.95 * k`.
- Skip segments with `k <= 0`.

**Timer:** `NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1/120, ...)`, added to the run loop in `NSRunLoopCommonModes` so it keeps firing while a menu is open. Each tick:

1. If sampling, `trail.add(mouseLocation, now)`.
2. Prune the trail.
3. Call `setNeedsDisplay_(True)` on every view.
4. Run the watchdog check.
5. If no longer sampling and the trail is empty, invalidate the timer and close the windows.

## 8. Screen capture (`screen_capture.py`)

```python
@dataclass(frozen=True)
class CapturedFrame:
    session_id: str
    index: int                 # 1-based
    path: Path                 # captures/<id>/frame_0001.jpg
    captured_at: datetime
    t_offset_s: float          # seconds since session start
    display_id: int
    mouse_xy: tuple[float, float]   # global Cocoa coordinates at capture time
    width: int
    height: int
```

**`ScreenCapturer(threading.Thread)`:**

- Schedule: `next_t = start`. Loop: capture, then `next_t += interval`, then `stop_event.wait(max(0, next_t - monotonic()))`. If a capture overruns, skip the missed ticks rather than bunching them. The first frame is taken immediately.
- Capture: `img = Quartz.CGDisplayCreateImage(display_id)`. If it returns `None`, log it (Screen Recording permission is likely missing) and continue.
- Encode with ImageIO and no PIL round trip:
  ```python
  url  = CFURLCreateFromFileSystemRepresentation(None, path_bytes, len(path_bytes), False)
  dest = CGImageDestinationCreateWithURL(url, "public.jpeg", 1, None)
  CGImageDestinationAddImage(dest, img, {kCGImageDestinationLossyCompressionQuality: 0.85})
  CGImageDestinationFinalize(dest)
  ```
- Mouse position: `NSEvent.mouseLocation()` is thread-safe for reading. `Quartz.CGEventGetLocation(CGEventCreate(None))` can be used instead; it gives top-left origin coordinates, which are converted.
- After the file is written, call `image_worker.submit(frame)`.
- Captured images are full resolution (Retina pixels).
- The caption panel is excluded from the images (§10). The streak overlay is included.

> `CGDisplayCreateImage` is deprecated by Apple in favour of ScreenCaptureKit, but it works on macOS 26.6.2 through PyObjC. If a future macOS removes it, `screen_capture.py` is the only module that needs replacing (same `CapturedFrame` output).

## 9. Audio capture (`audio_capture.py`)

Settings match `asr/main.py` exactly:

| Parameter | Value |
|---|---|
| Library | PyAudio |
| Format | `pyaudio.paInt16` |
| Channels | 1 |
| Rate | 16000 Hz |
| `frames_per_buffer` | 1600 (100 ms) |
| Read | `stream.read(1600, exception_on_overflow=False)` |
| Float conversion | `np.frombuffer(data, np.int16).astype(np.float32) / 32768.0` |

```python
@dataclass(frozen=True)
class AudioChunk:
    session_id: str
    index: int
    samples: np.ndarray        # float32, mono, 16 kHz, values in [-1, 1], len 1600
    t_offset_s: float
```

**`AudioRecorder(threading.Thread)`:**

- A single `pyaudio.PyAudio()` instance is created at app start and terminated when the app quits. Each session opens and closes its own stream, so the **mic is only active during a session**.
- Loop until `stop_event` is set: read, write the raw bytes to `wave` (`audio.wav`, 1 ch, sample width 2, 16 kHz), convert to float, then `audio_worker.submit(chunk)`.
- Stopping takes at most about 100 ms (one blocking read).
- `finally`: `stop_stream()`, `close()`, `wav.close()`.
- If the first 10 chunks are all zeros, log a warning that microphone permission is probably denied for the host terminal.

## 10. Processing hooks (`processing.py`)

### Image post-processing

```python
def process_image(frame: CapturedFrame) -> None:
    """Post-process a single captured screenshot. Intentionally a no-op for now."""
    return None
```

`ImageProcessingWorker`: a daemon thread with `queue.Queue(maxsize=64)`. `submit()` uses `put_nowait`; if the queue is full, it drops the **oldest** frame and logs it. The worker calls `process_image` inside try/except and logs any exception with its traceback.

### Real-time audio processing

```python
class AudioProcessor:
    """Real-time audio processing; will wrap asr/main.py (Parakeet) in Phase 8."""
    def start_session(self, session: Session) -> None: ...
    def process_chunk(self, chunk: AudioChunk) -> str | None:
        return None            # later: partial/final transcript text
    def end_session(self) -> str | None:
        return None            # later: final flush of buffered utterance
```

`AudioProcessingWorker`: a daemon thread with `queue.Queue(maxsize=100)` (10 s of audio). It handles three kinds of messages: `("start", session)`, `("chunk", AudioChunk)`, and `("end", None)`. Any non-`None` text is sent to the main thread with `callAfter(controller.on_transcript, text, is_final)`.

**Phase 8 integration** (not built yet): `AudioProcessor` lazily imports `asr.main` and calls `load_model(MODELS["tdt"])` on a background thread at app start. It reproduces the logic from `listen()` (noise calibration, RMS VAD, pre-roll, `PARTIAL_EVERY`, `SILENCE_CHUNKS`, `MAX_UTTERANCE_CHUNKS`) on the incoming chunks, calling `transcribe(model, np.concatenate(utterance))`. The chunk format above is already what those functions expect.

## 11. Menu bar and caption UI

### Status item (`app.py`)

- `NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)`: no Dock icon.
- Icon: `NSImage.imageWithSystemSymbolName_accessibilityDescription_("sparkles", ...)` as a template image when idle, and `"record.circle.fill"` while a session is active. If SF Symbols are unavailable, the title falls back to `✦`.
- Menu:

| Item | Action |
|---|---|
| ✓ Hotkey Enabled | Toggles `hotkey.enabled`. Disabling during a session calls `stop("disabled")` |
| Open Captures Folder | `NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(captures_dir))` (creates the folder if needed) |
| Show Last Transcript | `NSAlert` with the last final transcript, or "(no transcript yet)". Activates the app for the alert |
| — | separator |
| Quit Streak Catcher | `controller.stop("quit")`, stop the hotkey listener and workers, terminate PyAudio, then `NSApp.terminate_(None)` |

- Event loop: `AppHelper.runEventLoop(installInterrupt=True)`, so Ctrl+C in the terminal goes through the same clean shutdown.

### Caption panel (`caption.py`)

- `NSPanel` with style `Borderless | NonactivatingPanel`, level `NSStatusWindowLevel`, `ignoresMouseEvents=True`, transparent background, joins all Spaces.
- Content: `NSVisualEffectView` (material `HUDWindow`, state active, corner radius 14) containing an `NSTextField` label (not editable, not bezeled, white, system font 20 pt medium, centered, line break `NSLineBreakByTruncatingHead` so the latest words stay visible).
- Size and position: width `min(900, 0.7 * screen.visibleFrame.width)`, height 56, centered horizontally, 60 pt above the bottom of `visibleFrame` on the session's screen.
- **Excluded from screenshots:** `panel.setSharingType_(NSWindowSharingNone)`. Verified in Phase 4 by checking that saved frames don't contain it.
- API (main thread only): `show(screen, text)`, `set_text(text)`, `hide_after(delay_s)`. A new `show` cancels any pending hide.

`controller.on_transcript(text, is_final)` updates the caption. When `is_final` is True, it also appends the text to the session transcript, which becomes `last_transcript` when the session ends.

## 12. Permissions (`permissions.py`)

When run as a script, permissions are granted to the **host app** (Terminal, iTerm, or VS Code), not to Python.

| Permission | Needed for | Check / request |
|---|---|---|
| Accessibility / Input Monitoring | pynput event tap | `ApplicationServices.AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})` |
| Screen Recording | `CGDisplayCreateImage` returning real window content | `Quartz.CGPreflightScreenCaptureAccess()`. If False, `CGRequestScreenCaptureAccess()` |
| Microphone | PyAudio input | The system prompts the first time a stream opens. All-zero audio means it was denied (§9) |

At startup, print each permission's status. A missing permission produces a clear warning naming the host app and the System Settings path. The app keeps running.

## 13. Output layout

```
captures/
└── 20261110-203912/
    ├── frame_0001.jpg
    ├── frame_0002.jpg
    ├── ...
    └── audio.wav          # 16 kHz, mono, 16-bit PCM
```

`captures/` is added to `.gitignore`.

## 14. Logging and debugging (`log.py`)

```python
def log(component: str, msg: str, *, debug: bool = False) -> None
# [20:39:12.345] [MainThread    ] [hotkey ] activate (held={56, 58, 59})
```

- Always printed: session start and stop, permission status, errors with tracebacks, dropped queue items.
- Printed only when `STREAK_DEBUG=1`: every key event (vk, press or release, held set), each captured frame (index, ms taken, path, size), each audio chunk (index, RMS, queue size), each watchdog check that fires, and each overlay window created or closed (screen frame, display ID).
- **Debug rule (from the working rules):** if the app still fails after multiple fix attempts, add more `print` statements at the failing boundary, covering inputs, outputs, thread name, timing, and OS return values (`None` images, stream errors, permission flags), before trying further fixes.

## 15. Edge cases

| Case | Handling |
|---|---|
| Combo is pressed and released within 100 ms | Session starts and stops normally. At least 1 frame, possibly 0 to 1 audio chunks |
| Key release missed by pynput | Watchdog stops the session (§5) |
| Mouse moves to another display during a session | Streak follows (overlay windows on every screen). Capture stays on the starting display |
| Display connected or disconnected during a session | Overlays are rebuilt on the next session. The capture target may return `None`, which is logged and skipped |
| Quit during a session | `stop("quit")` before terminating. WAV is finalized |
| Hotkey disabled during a session | `stop("disabled")` |
| Disk write fails | Logged. That frame is skipped and the session continues |
| Processing hook raises | Logged with traceback. The worker continues |
| Workers back up | Bounded queues drop the oldest item and log it |

## 16. Manual test checklist (Phase 7)

1. The app starts, the menu bar icon appears, there's no Dock icon, and permission status is printed.
2. Holding L-Ctrl + L-Shift + L-Option makes the icon change, the caption show "Listening…", the streak follow the mouse, and the mic indicator turn on.
3. Releasing any one of the three keys stops everything. The streak fades and the caption hides after 2 s.
4. Right Ctrl, Shift, or Option in place of a left key doesn't trigger a session.
5. Holding the combo plus Cmd still works.
6. A 3 s hold gives about 7 JPEGs and a playable `audio.wav` of about 3 s in a new session folder.
7. Frames include the streak and don't include the caption panel.
8. Moving the mouse to a second display moves the streak there. Frames still show the starting display.
9. The Enable/Disable toggle works, including during a session.
10. Open Captures Folder opens Finder. Show Last Transcript shows an alert.
11. Quit and Ctrl+C both exit cleanly with no hanging threads, and the mic indicator turns off.
12. A 60 s hold uses stable memory and the queues don't overflow.
