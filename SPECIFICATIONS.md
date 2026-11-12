# Streak Catcher: Technical Specification

Companion to [PLAN.md](./PLAN.md). This document describes how each part is built.

---

## Working rules (apply to every request on this project)

- If there are issues and the app still doesn't work after multiple changes, add `print` statements to debug the underlying issue. Include the necessary information: thread name, timestamps, key codes, display IDs, file paths, queue sizes, permission state, and exceptions with tracebacks.
- Do **not** create summary files when a phase is completed.
- When reporting a completed phase in chat, don't list the code changes. Instead include a **"How to test"** section: what the user can verify by hand, with step-by-step instructions (commands, what to click or press, expected result). If nothing in the phase can be tested by hand, say so explicitly.
- The **only** Markdown files are `PLAN.md`, `SPECIFICATIONS.md`, and `docs/LEARNINGS.md`.
- Record non-obvious technical gotchas (surprising library, macOS, or tooling behavior) in `docs/LEARNINGS.md` (What / Why / Fix / Avoid next time). Do **not** record plan or documentation mismatches (stale checkboxes, outdated paths, bookkeeping); just fix those in place.
- All other files created are Python code files (temporary files in `.tmp/` are the exception).
- Prefer a `config.py` field over a hardcoded value for anything tunable (thresholds, timeouts, sizes, model names), with a sensible default.
- Run Python with `uv run` (e.g. `uv run main.py`, `uv run .tmp/scripts/check.py`), not `.venv/bin/python`.
- Put every temporary file (scratch scripts, test data, fixtures, logs) under `.tmp/` in the project root, never in `/tmp` or elsewhere in the repo. Use subfolders as needed: `.tmp/scripts/`, `.tmp/data/`, `.tmp/fixtures/`, `.tmp/logs/`. `.tmp/` is git-ignored.

---

## 1. Environment

| Item | Value |
|---|---|
| OS | macOS 26.6.2 (Apple Silicon). Minimum supported: macOS 13 |
| Python | 3.14 (root uv venv at `./.venv`) |
| Run | `uv run main.py` (from the repo root) |
| Dependencies (root `pyproject.toml`) | `pyaudio`, `pynput` (1.8.x), `pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz` (13.x), `numpy` |
| ASR | `cactus-compute` (Whistle, 16 MB model) runs in-process from Phase 8 (see §17.1). torch and NeMo are not used |

Dependencies are added with `uv add`. If the `asr` module later brings torch and NeMo, declare them in `pyproject.toml` too, so a `uv sync` doesn't remove them.

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
├── main.py              # entry point: uv run main.py
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
    screen_capture_enabled: bool = True                    # False: sessions take no screenshots
    frame_buffer_interval_s: float = 0.5                   # Phase 10 buffer cadence (see §17.3 for all capture keys)
    capture_scale: float = 0.5                             # 0.5 = logical size (~490 KB/frame); 1.0 = full Retina
    jpeg_quality: float = 0.85

    # audio (must match the asr module, provided later)
    sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = 1600                                 # 100 ms
    audio_input_device: str | None = None                  # None = system default; else substring of device name

    # output
    captures_dir: Path = Path(__file__).parent / "captures"
    session_reuse_window_s: float = 10.0                   # restart within N s of a stop reuses the folder; 0 = off

    # caption
    caption_hide_delay_s: float = 2.0
    fake_transcript: bool = False                          # placeholder words instead of the silent stub

    # debugging
    debug: bool = os.environ.get("STREAK_DEBUG") == "1"
```

The audio values are **defined here** instead of imported from the `asr` module (provided later, expected at `asr/main.py` with `SAMPLE_RATE`, `CHUNK_SIZE`), because importing that module loads torch and NeMo, which takes seconds and gigabytes of memory. When `asr` arrives, check that its values match these.

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

**Watchdog (main thread):** during a session, check `NSEvent.modifierFlags()` on every tick of a 50 ms `NSTimer` owned by `SessionController` (independent of the streak timer). If any of `NSEventModifierFlagControl` (1<<18), `NSEventModifierFlagShift` (1<<17), or `NSEventModifierFlagOption` (1<<19) is missing, call `controller.stop(reason="watchdog")` and clear `held` in the listener. This recovers from releases that were missed (for example, during Secure Input).

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
2. Create the `Session` and `mkdir -p` its folder. If a folder with that timestamp already exists (two sessions within one second), append `-2`, `-3`, and so on.
   - **Folder reuse:** if the previous session *stopped* no more than `session_reuse_window_s` seconds ago (default 10, `0` disables) and its folder still exists, the previous `Session` is continued instead: same folder and id, `segment` increments, frame numbering carries on (`frame_0007.jpg` …), and `frame_count` covers the whole folder. `t_offset_s` stays relative to the first segment's start. The display is re-picked for each segment.
3. Start the `StreakOverlay`.
4. Show the `CaptionPanel` with "Listening…" on that screen.
5. Call `audio_processor.start_session(session)` through the audio worker.
6. Start the `AudioRecorder`.
7. Start the `ScreenCapturer` (skipped when `screen_capture_enabled` is False; the session folder is still created).
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

> **Superseded in part by Phase 10 (§17.3):** frames are grabbed into an in-memory buffer and only trigger-selected, deduplicated frames are written. The capture primitives below (CGDisplayCreateImage, scaling, JPEG via ImageIO, `CapturedFrame`) are unchanged.

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
- Captured images are scaled by `capture_scale` (default 0.5, the logical screen size, for example 1728 px wide on a 3456 px Retina panel) with `kCGInterpolationHigh` before encoding. Measured on this Mac: about 490 KB per frame instead of about 1.3 MB at 1.0, and text stays readable. `1.0` keeps full Retina pixels.
- The caption panel is excluded from the images (§10). The streak overlay is included.

> `CGDisplayCreateImage` is deprecated by Apple in favour of ScreenCaptureKit, but it works on macOS 26.6.2 through PyObjC. If a future macOS removes it, `screen_capture.py` is the only module that needs replacing (same `CapturedFrame` output).

## 9. Audio capture (`audio_capture.py`)

Settings below are the contract the `asr` module (provided later) is expected to match:

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

- Each recording (session segment) creates its own `pyaudio.PyAudio()` on the `audio-capture` thread, opens one stream, and terminates both when it stops, so the **mic is only active during a session**. A long-lived instance would not see input devices connected after app start, because PortAudio caches the device list at init (re-init costs about 7 ms).
- Device: `audio_input_device` (case-insensitive substring of the device name) or the system default input. The chosen device name is logged at the start of each recording. All inputs are listed in debug mode.
- Real-time check: after 10 chunks, compare the audio duration received against wall time. Below 0.9x, log a warning naming the device (seen with Bluetooth headset mics at about 0.6x).
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
    """Real-time audio processing; replaced by CactusProcessor (cactus Whistle) in Phase 8."""
    def start_session(self, session: Session) -> None: ...
    def process_chunk(self, chunk: AudioChunk) -> str | None:
        return None            # later: partial/final transcript text
    def end_session(self) -> str | None:
        return None            # later: final flush of buffered utterance
```

`AudioProcessingWorker`: a daemon thread with `queue.Queue(maxsize=100)` (10 s of audio). It handles three kinds of messages: `("start", session)`, `("chunk", AudioChunk)`, and `("end", None)`. Any non-`None` text is sent to the main thread with `callAfter(controller.on_transcript, text, is_final)`.

**Phase 8** replaces this stub with an in-process cactus `Whistle.stream()` processor; see §17.1.

## 11. Menu bar and caption UI

### Status item (`app.py`)

- `NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)`: no Dock icon.
- Icon: `NSImage.imageWithSystemSymbolName_accessibilityDescription_("sparkles", ...)` as a template image when idle, and `"record.circle.fill"` while a session is active. If SF Symbols are unavailable, the title falls back to `✦`.
- Menu:

| Item | Action |
|---|---|
| Speech model status (disabled) | "Loading speech model…", then "Speech model ready" or "Speech model unavailable" (or "Speech: fake transcript (config)"). Set synchronously at launch, then updated from the loading thread with `callAfter` |
| ✓ Hotkey Enabled | Toggles `hotkey.enabled`. Disabling during a session calls `stop("disabled")` |
| Open Captures Folder | `NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(captures_dir))` (creates the folder if needed) |
| Show Last Transcript | `NSAlert` with the last final transcript, or "(no transcript yet)". Activates the app for the alert |
| — | separator |
| Quit Streak Catcher | `controller.stop("quit")`, stop the hotkey listener and workers, terminate PyAudio, then `NSApp.terminate_(None)` |

- Event loop: `AppHelper.installMachInterrupt()` then `AppHelper.runEventLoop()`, so Ctrl+C in the terminal goes through the same clean shutdown. (`runEventLoop(installInterrupt=True)` silently skips installing the handler when `NSApp` was created beforehand, which it is, to set the activation policy.)

### Caption panel (`caption.py`)

- `NSPanel` with style `Borderless | NonactivatingPanel`, level `NSStatusWindowLevel`, `ignoresMouseEvents=True`, transparent background, joins all Spaces.
- Appearance forced to `NSAppearanceNameVibrantDark`. The HUD material follows the system appearance and renders light gray in Light Mode, which makes white text unreadable.
- Content: `NSVisualEffectView` (material `HUDWindow`, state active, corner radius 14) containing an `NSTextField` label (not editable, not bezeled, white, system font 20 pt medium, centered, line break `NSLineBreakByTruncatingHead` so the latest words stay visible).
- Size and position: width `min(900, 0.7 * screen.visibleFrame.width)`, height 56, centered horizontally, 60 pt above the bottom of `visibleFrame` on the session's screen.
- **Excluded from screenshots:** `panel.setSharingType_(NSWindowSharingNone)`. Verified on macOS 26 with `CGDisplayCreateImage`: the panel region is unchanged in live captures and saved frames (mean pixel diff < 1), while the same panel set to `NSWindowSharingReadOnly` shows up clearly (diff around 199).
- API (main thread only): `show(visible_frame, text)`, `set_text(text)`, `hide_after(delay_s)`, `close()`. A new `show` cancels any pending hide (token checked when the delayed hide fires). `Session.visible_frame` holds the session screen's `visibleFrame()`.
- `config.fake_transcript = True` swaps in `FakeAudioProcessor`, which emits one placeholder word per 0.5 s of audio and a final sentence at session end, to exercise the caption before real STT.

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
    ├── audio.wav          # 16 kHz, mono, 16-bit PCM (segment 1)
    └── audio_2.wav        # later segments when the folder is reused (§6)
```

`captures/` is added to `.gitignore`.

From Phase 8 each session folder gets `transcript.json` (final text and word timestamps), and from Phase 9 `path.json` (cursor path and detected gestures). From Phases 10–11 it also gets `frames.json` (frame triggers and dedup), `context_N.jpg` (annotated overviews), and `request.json` (the assembled LLM request). See §17.

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

## 17. Phases 8–13 design (agreed, not built)

### 17.1 Real-time ASR with cactus Whistle (Phase 8)

Engine: `cactus-compute` 4.1.0 (`cactus.agent.whistle.Whistle`), following the user's reference streaming script. Dependencies are only `huggingface_hub` and `Pillow`. The model (`Cactus-Compute/whistle`, `whistle.cact`, 16 MB) and native runtime wheel are fetched from Hugging Face on first use and cached in `~/.cache/huggingface/hub`. It runs **in-process**.

Measured on this Mac (synthetic 3.4 s sentence, 1 s chunks): import ~50 ms, `Whistle()` load ~280 ms, `stream()` pass 16–30 ms per chunk, tail committed ~100 ms after the last chunk, transcript word-perfect.

| API | Behavior |
|---|---|
| `Whistle(weights=None)` | Loads weights (downloads on first use). Calls are serialized across threads |
| `transcribe(samples, language, keywords, word_timestamps)` | Batch; returns `text`, `language`, `ttft_ms`, `decode_tps`, plus `words` |
| `stream(chunks, language, keywords)` | Generator over ~1 s float32 16 kHz chunks. Yields one dict per chunk with committed `text` and `words` (`word`, `start`, `end`, `probability`; seconds from stream start), the unconfirmed `pending` tail, `language`, `received`, `pass_ms`. When the chunk iterator ends, it yields the committed tail. Holds a process-wide lock for its whole duration |

- **Telemetry:** cactus sends anonymous usage counts (function name, version, OS; no audio or text) unless `CACTUS_TELEMETRY=0`. The app sets it from `cactus_telemetry` (`False`) **before importing cactus**.
- **Preload:** at app start, a background thread creates `Whistle()` and runs a short warm-up `transcribe`. A disabled menu line shows "Loading speech model…", then "Speech model ready", or "Speech model unavailable" (error logged).
- **`CactusProcessor`** (replaces the `AudioProcessor` stub, on the `audio-worker` thread): per session segment, one `stream()` over a generator that regroups the recorder's 100 ms chunks into `asr_chunk_s` chunks. The `end` message ends the generator, so the tail commits. Each yielded step sends `on_transcript(committed + pending, is_final=False)`. The final message carries the joined committed text and all words.
- **Clock:** word times are relative to the segment's first audio sample. Add the recorder's start offset (`t_offset_s` of chunk 1) to put them on the session clock shared with gestures and frames.
- **Before ready:** sessions are allowed. Chunks queue in the worker and are streamed once the model has loaded.
- **Caption:** committed text in white, the `pending` tail dimmed (`CaptionPanel.set_parts`), updated about every 1 s. Captions from an older session's late results never overwrite a newer session's caption.
- **Push vs pull:** `stream()` pulls from a generator, but the audio worker pushes chunks. So each segment gets its own `asr-stream` thread that pulls regrouped chunks from a queue, and `CactusProcessor` posts results itself through `post(text, is_final, pending, words, session)`, which the worker wires to `controller.on_transcript` via `callAfter`. The audio worker never blocks, even while the model loads.
- **Output:** final words (session clock) accumulate on `Session.words`, and `transcript.json` (`{"session", "text", "words": [{"word", "start", "end", "p"}]}`) is written to the session folder after each segment.
- **Known behavior:** near-silent input can produce a hallucinated short transcript (for example "you"), as with Whisper-style models. Phase 11 has to treat very short, low-confidence finals as "no speech".

### 17.2 Cursor path and gestures (Phase 9)

- The streak sampler already reads the cursor at 120 Hz. Each sample `(t, x, y)` (session-relative seconds, global Cocoa coordinates) is also appended to the session path and saved as `path.json`.
- **Strokes:** split the path at pauses (speed below `gesture_pause_speed_pt_s` for at least `gesture_pause_s`). Speed is measured over `gesture_speed_window_s`, not sample to sample, because the 120 Hz sampler often repeats a position between mouse events. Each stroke is resampled every `gesture_resample_pt` before measuring turns, so jitter doesn't add turning.
- **Circle:** searched as a **loop anywhere inside the stroke** (circling then moving away without a pause still counts). For each end point, scan back for the nearest start where the loop closes (end-to-start distance ≤ `circle_close_ratio` × bounding-box diagonal), cumulative signed turning reaches about ±360° (≥ `circle_min_turn_deg`), the bounding-box aspect ratio is within `circle_max_aspect`, and the size is at least `gesture_min_size_pt`.
- **Underline:** mostly horizontal (vertical extent ≤ `underline_max_dy_ratio` × horizontal extent) with **at least `underline_min_reversals` direction reversals** (↔ back-and-forth), and width ≥ `underline_min_dx_pt`. A single horizontal sweep is treated as cursor travel.
- Only circle and underline are supported. The overlay can't take clicks, so there is no free drawing.
- **Merging repeated circling:** loops in **one continuous stroke** merge when overlap ÷ smaller-box area ≥ `gesture_merge_overlap` (so a spiral of shrinking loops, including small kinks inside the big loop, is one gesture). Loops in **separate strokes** (a pause between them) merge only at IoU ≥ `gesture_merge_iou`, so "circle a panel, pause, circle a button inside it" stays two gestures. The merged bbox is the union.
- **Underline trimming (robust line fit):** a candidate run can include stray travel, such as a slanted approach whose leg is nearly flat enough. Fit the dominant near-horizontal line with RANSAC (candidate point pairs, slope ≤ `underline_max_slope`, score = points within ±`underline_band_pt`), then refit by least squares on the inliers. Keep the **longest run of inliers in time** (bridging outlier gaps of ≤ `underline_max_gap_pts` resampled points), and re-check width ≥ `underline_min_dx_pt` and ≥ `underline_min_reversals` reversals. The bbox and `t_start`/`t_end` come from the kept points only.
- Underline legs narrower than `underline_min_leg_pt` (jitter at a reversal, or a small overshoot correction after travel) are folded into the neighboring leg, so ordinary travel doesn't produce a reversal.
- Implementation: `gestures.py` (pure Python, no AppKit), `GestureDetector.feed(t, x, y)` from `StreakOverlay.on_sample`, `finish()` on stop. Classification runs when a stroke ends (about 6 ms for a 2 s stroke, 50 ms for a 10 s one). Debug mode logs per-stroke metrics (points, bbox, total turn, result) for tuning.
- Output: `Gesture(index, kind, t_start, t_end, bbox)` with bbox `(x, y, w, h)` in global Cocoa points, numbered in time order within the session and continuing across reused segments. Stored on `Session.gestures` and in `path.json` (`{"session", "coords", "samples": [[t, x, y]], "gestures": [...]}`), which is rewritten at every stop.
- Thresholds are tuned by replaying recorded `path.json` files through a check script.
- **Debug images** (`gesture_debug.py`, when `gesture_debug_images`, which defaults to on with `STREAK_DEBUG=1`): after each stop, a `gesture-debug` thread writes `debug_gesture_N.jpg` to the session folder: the saved frame nearest the gesture's `t_start`, with the gesture's path (yellow), its bbox (magenta), and a label `#N kind t_start-t_end (frame @ t)`. Global Cocoa points map to frame pixels as `(p - screen_origin) × (frame_width / screen_width)`; both use a y-up origin, so no flip is needed. The session keeps `Session.frames` (`(t_offset_s, path, display_id)`) for this. Without screen capture, no image is written (logged).

### 17.3 Event-driven capture (Phase 10)

- **Clean frames:** overlay windows get `setSharingType_(NSWindowSharingNone)` unless `frames_include_streak`, so the streak is not captured. Annotations are drawn by the app later.
- **Rolling buffer:** the `screen-capture` thread grabs every `frame_buffer_interval_s`. Each `BufferedFrame` holds the JPEG bytes, a grayscale thumbnail (`dedup_thumb_width` px), the time, mouse position, and frontmost app, **in memory**. Pruning keeps the last `frame_buffer_s` seconds **plus everything since the stroke in progress began** (`GestureDetector.stroke_start`), so a gesture detected long after it started (a 6 s spiral) still has its start frame; `frame_buffer_max` caps memory. Nothing is written until a frame is chosen.
- **One thread does selection:** the main thread only enqueues requests (`request_gesture(g)` from `_on_gesture`, `request_app_switch(t, app)` from the `NSWorkspaceDidActivateApplicationNotification` observer, ignoring our own pid). Each capture tick drains them. Gesture start = last buffered frame with `t ≤ t_start`. Gesture end and app switch wait in `pending` until a frame at or after `t_end` (or switch time + `app_switch_settle_s`) exists. On stop the thread grabs one fresh `release` frame and resolves everything pending. `controller.stop()` finishes the gesture detector **before** stopping the capturer, so gestures closed by release still get frames.
- **Persist triggers:** session start; gesture start (the buffered frame just before `t_start`); gesture end; frontmost-app change (`NSWorkspaceDidActivateApplicationNotification`); release; and a safety net every `safety_net_interval_s` (2 s).
- **Dedup (changed area):** downscale both frames to grayscale `dedup_thumb_width` px wide and count the pixels whose difference exceeds `dedup_pixel_delta`. If changed area < `dedup_changed_pct`% of the frame, they are duplicates. Safety-net and app-switch frames can be dropped as duplicates. **Gesture frames are only merged** (they keep their gesture refs) and never dropped.
- `frames.json` (`{"session", "frames": [...]}`), per kept frame: `file`, `t`, `triggers` (all triggers that chose or merged into it), `gestures`, `app`, `display_id`, `mouse`, `width`, `height`, `changed_pct_vs_nearest`, and `merged` (each duplicate request: trigger, t, changed %, gesture). `Session.frames` holds the same records plus `path` and `thumb` (used for dedup across reused segments and by gesture debug images, which prefer the gesture's own start frame).
- Measured: a 9 s session with 2 gestures, an app switch, and 3 distinct screens grabbed 20 frames and kept 3 (4 duplicate requests dropped, gesture requests merged).

### 17.4 Contexts, annotation, and request assembly (Phase 11)

- **One request per release (segment).** A reused folder gets `request_N.json` and `context_N_k.jpg` for segment N. The segment's frames are those kept or merged-into during `[segment start, stop + 1 s]`; its gestures are those numbered from the segment's first index.
- **Context = a group of visually similar frames** (same changed-area metric, but with the looser `context_changed_pct`, since kept frames already differ by at least `dedup_changed_pct`). Greedy, in time order: a frame joins the context whose first frame differs least, if below the threshold. This never misplaces a box after scrolling, and returning to an earlier screen merges into its context. App switches are capture triggers, not grouping keys.
- **One annotated overview per context, no crops.** Draw a numbered box `[n]` for each gesture in that context on the context's representative frame: the gesture-start frame of its first gesture, or the latest frame for contexts without gestures. Box = gesture bbox + `annotation_pad_pt`, stroked in `annotation_rgba` over a wider `annotation_halo_rgba` halo (a magenta box alone was nearly invisible on red), with an `[n]` badge above the top-left corner.
- **Hallucination filter:** a word is dropped when the loudest 100 ms chunk overlapping `[start - speech_rms_margin_s, end + margin]` is below `speech_min_rms` (`Session.audio_levels`, recorded per chunk). Word confidence is useless here: hallucinated "you." scored p = 1.0. Dropped words are listed in the request.
- **Markers:** insert `[n]` after the word whose midpoint is nearest gesture `n`'s `t_start`. Without word timings (fake processor or timeout), the markers are appended at the end.
- **Concurrency:** on release, the image job (dedup, contexts, annotation) starts immediately on a worker thread. Assembly waits for the final transcript up to `assembly_timeout_s` (caption: "Transcribing…"), or up to `model_wait_timeout_s` if the model is still loading (caption: "Waiting for speech model…"). On timeout it uses the committed text so far (logged). Neither blocks the next session.
- **Edge cases:**
  - Speech and no gestures: the images are the unique (deduped) frames from start, release, app switches, and the safety net, without boxes.
  - Gestures and no speech (empty transcript): nothing is sent; a brief caption "No instructions heard" (~1.5 s).
  - Neither: nothing is sent.
- **Image cap:** `max_images` (`None` = no cap). When over the cap, keep the first or last contexts per `truncate_keep` (`"start"` / `"end"`).
- **Final transcript hand-off:** the controller keeps a per-session FIFO of assemblers; each final transcript (`on_transcript(is_final=True)`) is delivered to the oldest one. The committed text from partials is kept per session as the timeout fallback. "Transcribing…" / "Waiting for speech model…" is shown only if the wait exceeds `assembly_status_after_s`, and never over a newer session's caption.
- Saved per segment: `request.json` with `session`, `segment`, `status` (`ok` / `no_speech` / `empty`), `transcript`, `transcript_with_markers`, `words` (kept, with `rms`), `dropped_words`, `gestures` (with `context`), `contexts` (`id`, `image`, `frame`, `frames`, `app`, `gestures`), `images` (absolute paths, in order), `truncated_contexts`, and `assembly_s`. Also `context_k.jpg`. The request is handed to `controller.on_request` on the main thread (Phase 12 sends it); `last_transcript` becomes the marked transcript.

### 17.5 LLM provider and response (Phases 12–13)

```python
class LLMProvider(Protocol):
    def respond(self, request: LLMRequest) -> str: ...   # text with [n] markers + annotated image paths
```

- Chosen by `llm_provider` (env `STREAK_LLM_PROVIDER` overrides): `copilot`, `echo` (offline; describes what would be sent), or `none` (requests are only saved). `llm.py`: `make_provider()`, `build_prompt()`, `cap_images()`.
- **`CopilotProvider`** (PyPI `github-copilot-sdk`, Python 3.11+). The SDK downloads and runs its own runtime unless `copilot_cli_path` (→ `StdioRuntimeConnection(path=…)`) or `COPILOT_CLI_PATH` is set, and reuses the Copilot CLI login. Startup: `CopilotClient.start()` (~15 s cold, ~1–2 s warm), then `list_models()` validates `llm_model` and reads its vision support and `capabilities.limits.vision.max_prompt_images`.
- **Models (this account):** `gpt-5.4` is not available. All listed models except `auto` support vision, but **most accept only 1 image per prompt**; Claude Sonnet and Haiku accept 5. Default `llm_model = "claude-sonnet-5.5"`. When a request has more images than the model allows, `cap_images` keeps them per `truncate_keep` and logs the drop.
- **Safety:** sessions are created with `available_tools=[]`. The SDK is an agent runtime, and by default the model could run tools (shell, file edits); we only want text.
- Images are supported as **file attachments by absolute path**:
  ```python
  client = CopilotClient(); await client.start()              # once, at app launch
  session = await client.create_session(model=cfg.llm_model)  # per request
  event = await session.send_and_wait(prompt, attachments=[{"type": "file", "path": "/abs/context_1.jpg"}, ...],
                                      timeout=cfg.llm_timeout_s)    # reply text: event.data.content
  await session.disconnect()
  ```
- The client lives on a dedicated `llm-loop` asyncio thread, started at app launch. Each request runs on an `llm-request` thread (`respond()` blocks on the loop's future) and uses a new session (no memory across sessions), with timeout `llm_timeout_s`. Result or error goes to `controller.on_response` on the main thread, is saved as `response.json` / `response_N.json` (`provider`, `model`, `seconds`, `text`, `error`), and becomes `last_response`.
- **Markdown rendering** (`md_render.py`, shared with Phase 13): `normalize()` inserts the blank line Python-Markdown needs before a list that directly follows text (skipping fenced code). Then `markdown` (fenced_code, sane_lists, tables) → HTML with system-font CSS (`response_font_pt`) → `NSAttributedString` via the AppKit HTML importer (main thread). Post-processing: `labelColor` for all text (light/dark), a `quaternarySystemFillColor` background on monospaced runs but not on list markers, spacer paragraphs after lists and code blocks (the importer ignores their margins), and after layout `drop_wrapped_code_backgrounds()` removes the background from inline code that wraps inside indented list items (AppKit would fill it from the margin).
- **Show Last Response**: an `NSAlert` whose accessory view is a selectable `NSTextView` in an `NSScrollView` (`response_view_width_pt` wide, sized to content up to `response_view_max_height_pt`, then scrolls), with the provider, model and time as subtitle, and **OK** / **Copy** (copies the raw markdown).
- UI until Phase 13: a disabled menu line "LLM: starting copilot (model)… / copilot ready (model) / copilot unavailable", the caption shows "Thinking…" and then the response's first line for `response_caption_s`, and a **Show Last Response** menu item shows the full text.
- Measured: a red/blue test screenshot with box [1] around the red square and the transcript "What color is this [1] square?" got "Red." from claude-sonnet-5.5 in 1.8 s. A 1-image model (gpt-5.6-luna) given 2 images was capped to the last one and answered correctly.
- The prompt template explains that `[n]` in the text refers to numbered box `n` in the attached images.
- **Response panel** (`response_panel.py`): dark HUD style like the caption, bottom-center of the session's screen, replacing the caption. Width `min(response_max_width_pt, response_width_fraction × visibleFrame.width)`. Height grows with the text up to `response_max_height_fraction` × visibleFrame height, then an `NSScrollView` scrolls. Markdown is rendered through `NSAttributedString` when `response_markdown` is set. It has **Copy** and **✕** buttons, accepts mouse events but is non-activating (the user's app keeps focus), and is excluded from screenshots (`NSWindowSharingNone`). It is dismissed by ✕, Esc (detected by the existing pynput listener, since the panel never has focus), the next session, or `response_auto_dismiss_s` if set. It shows "Thinking…" while waiting.

### 17.6 Config additions (defaults; tune from real sessions)

| Key | Default | Purpose |
|---|---|---|
| `cactus_telemetry` | `False` | Sets `CACTUS_TELEMETRY` before importing cactus |
| `asr_language` | `"en"` | `None` = auto-detect (en, de, fr, es, it, nl, pl) |
| `asr_keywords` | `()` | Keyword biasing for UI and app terms |
| `asr_chunk_s` | `1.0` | Chunk length fed to `stream()` |
| `gesture_pause_s` / `gesture_pause_speed_pt_s` / `gesture_speed_window_s` | `0.15` / `60` / `0.05` | Stroke segmentation |
| `gesture_debug_images` | `STREAK_DEBUG=1` | Write `debug_gesture_N.jpg` per gesture after each session |
| `gesture_resample_pt` / `gesture_merge_overlap` / `gesture_merge_iou` | `4` / `0.6` / `0.5` | Path resampling; merging loops within one stroke (containment) / across strokes (IoU) |
| `gesture_min_size_pt` | `30` | Ignore tiny strokes |
| `circle_min_turn_deg` / `circle_close_ratio` / `circle_max_aspect` | `300` / `0.35` / `3.0` | Circle detection (closure relative to bbox diagonal) |
| `underline_max_dy_ratio` / `underline_min_dx_pt` / `underline_min_leg_pt` / `underline_min_reversals` | `0.3` / `60` / `30` / `1` | Underline (↔) detection |
| `underline_band_pt` / `underline_max_slope` / `underline_max_gap_pts` | `12` / `0.25` / `6` | Robust line fit that trims stray travel from underlines |
| `frame_buffer_interval_s` / `frame_buffer_s` / `frame_buffer_max` | `0.5` / `4.0` / `60` | Rolling in-memory frame buffer (plus the stroke in progress) |
| `app_switch_settle_s` / `frames_include_streak` | `0.3` / `False` | Delay before the app-switch frame; clean frames |
| `safety_net_interval_s` | `2.0` | Periodic safety-net capture |
| `dedup_thumb_width` / `dedup_pixel_delta` / `dedup_changed_pct` | `256` / `24` / `3.0` | Changed-area duplicate and context test |
| `max_images` / `truncate_keep` | `None` / `"end"` | Image cap and which end to keep |
| `context_changed_pct` | `15` | Changed area below which frames share a context (one image) |
| `speech_min_rms` / `speech_rms_margin_s` | `0.008` / `0.05` | Drop words over near-silent audio (hallucinations) |
| `assembly_status_after_s` / `no_instructions_caption_s` | `0.3` / `1.5` | Caption status timing |
| `annotation_rgba` / `annotation_halo_rgba` / `annotation_pad_pt` / `annotation_line_pt` / `annotation_halo_pt` / `annotation_label_pt` | magenta / white / `8` / `3` / `2` / `16` | Numbered gesture boxes |
| `assembly_timeout_s` / `model_wait_timeout_s` | `5.0` / `60.0` | Wait for the final transcript, or for the model to load |
| `llm_provider` / `llm_model` / `llm_timeout_s` / `llm_start_timeout_s` / `copilot_cli_path` | `"copilot"` (env `STREAK_LLM_PROVIDER`) / `"claude-sonnet-5.5"` / `60.0` / `60.0` / `None` | LLM |
| `llm_prompt_template` / `response_caption_s` | see `config.py` / `8.0` | Prompt (`{transcript}`, `{n_images}`, `{gestures}`); interim caption duration |
| `response_font_pt` / `response_view_width_pt` / `response_view_max_height_pt` | `13` / `560` / `520` | Rendered markdown response view |
| `response_max_width_pt` / `response_width_fraction` / `response_max_height_fraction` | `900` / `0.7` / `0.5` | Response panel size |
| `response_markdown` / `response_auto_dismiss_s` | `True` / `None` | Response rendering and dismissal |
