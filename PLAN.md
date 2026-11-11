# Streak Catcher: Implementation Plan

A macOS menu bar app written in Python. While **Left Control + Left Shift + Left Option** are held down, it:

1. Draws a **light streak** trail behind the mouse cursor.
2. Records **microphone** audio and sends each chunk to a real-time processor. The processor will later produce transcribed text, which the app shows in a caption panel.
3. Takes a **screenshot** of the screen where the combo started, every 500 ms, and sends each image to a post-processor.

Technical details are in [SPECIFICATIONS.md](./SPECIFICATIONS.md).

---

## Working rules (apply to every request on this project)

- If there are issues and the app still doesn't work after multiple changes, add `print` statements to debug the underlying issue. Include the necessary information: thread name, timestamps, key codes, display IDs, file paths, queue sizes, permission state, and exceptions with tracebacks.
- Do **not** create summary files when a phase is completed.
- The **only** two Markdown files are `PLAN.md` and `SPECIFICATIONS.md`.
- All other files created are Python code files.

---

## Decisions (agreed)

| Area | Decision |
|---|---|
| Location | Modules live directly in the repo root (`./`). No `streak_catcher/` package folder |
| Environment | Existing root uv venv (`.venv`, Python 3.14). Dependencies go in the root `pyproject.toml` |
| Run mode | Python script: `.venv/bin/python main.py`. Permissions are granted to Terminal or VS Code |
| Menu bar | Pure PyObjC `NSStatusBar`, accessory app (no Dock icon) |
| Menu items | Enable/Disable hotkey, Open captures folder, Show last transcript, Quit |
| Hotkey | `pynput`. Left Ctrl + Left Shift + Left Option held; other modifiers ignored |
| Streak | White/cyan glow, short fade (~250 ms), visible in screenshots |
| Capture | Quartz `CGDisplayCreateImage`, display where the combo started (fixed), every 500 ms whether or not the mouse moves |
| Images | JPEG saved to a per-session folder, then passed to a per-image background worker |
| Audio | Same as `asr/main.py`: PyAudio, 16 kHz, mono, int16 → float32, 1600-sample (100 ms) chunks |
| Audio save | Session audio is also saved as `audio.wav` |
| STT | Engine comes from `asr/main.py` (Parakeet via NeMo). Stub for now |
| Transcript UI | Floating caption panel at the bottom of the screen |
| Tests | None |

---

## Phase 0: Scaffold and environment

**Goal:** an empty, runnable app in the repo root.

- [x] Add dependencies to the root `pyproject.toml` with `uv add --no-sync` (`pyaudio`, `pynput`, `pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz`), then `uv pip install` them. `--no-sync` stops uv from removing the undeclared torch and NeMo.
- [x] Verify that `CGDisplayCreateImage` works on macOS 26 and the pynput left-modifier codes are correct (59 / 56 / 58).
- [ ] Create the root files: `main.py` (entry point) and `config.py`.
- [ ] Add `captures/` to `.gitignore`.

**Done when:** `.venv/bin/python main.py` starts and exits cleanly, printing the config.

## Phase 1: Menu bar app shell and permissions

**Goal:** a status bar icon with a working menu and no Dock icon.

- [ ] `app.py`: `NSApplication` with the accessory activation policy, `AppDelegate`, and an `NSStatusItem` using the SF Symbol `sparkles`.
- [ ] Menu items: Enable/Disable hotkey (checkmark state), Open captures folder, Show last transcript (`NSAlert`), Quit.
- [ ] `permissions.py`: check and request Accessibility, Screen Recording, and Microphone. Print the status of each at startup.
- [ ] Ctrl+C in the terminal quits cleanly (`AppHelper.runEventLoop(installInterrupt=True)`).

**Done when:** the icon appears, each menu item works, and Quit and Ctrl+C both exit.

## Phase 2: Hotkey detection

**Goal:** reliable start and stop callbacks for the combo.

- [ ] `hotkey.py`: a `pynput.keyboard.Listener` that keeps a set of held left-modifier key codes.
- [ ] Fire `on_activate` when all three are down and `on_deactivate` when any of them is released. Each fires once per transition (edge-triggered).
- [ ] Send callbacks to the main thread through `AppHelper.callAfter`.
- [ ] Watchdog: on the main thread, poll `NSEvent.modifierFlags()` during a session. If Ctrl, Shift, or Option is no longer down, force a stop, in case pynput missed a key release.
- [ ] Respect the Enable/Disable toggle. Disabling during a session stops it.

**Done when:** logs show exactly one start and one stop per press and release. Right-side modifiers don't trigger a session.

## Phase 3: Light streak overlay

**Goal:** a glowing trail that follows the cursor on any screen.

- [ ] `streak.py`: a `Trail` model holding `(x, y, t)` points in global Cocoa coordinates and dropping points older than 250 ms.
- [ ] One borderless, transparent, click-through `NSWindow` per `NSScreen`, at `NSScreenSaverWindowLevel`, joining all Spaces.
- [ ] A `StreakView` (NSView) that draws tapered segments in three passes (outer cyan glow, inner cyan, white core) with alpha based on each point's age.
- [ ] A 120 Hz `NSTimer` samples `NSEvent.mouseLocation()` and redraws.
- [ ] On stop: stop sampling, let the trail fade out, then close the windows.

**Done when:** the streak is smooth, fades in ~250 ms, works across displays, and doesn't block clicks.

## Phase 4: Screen capture and image post-processing

**Goal:** a JPEG every 500 ms, each handed to a stub post-processor.

- [ ] `session.py`: create the session folder `captures/YYYYmmdd-HHMMSS/`.
- [ ] `screen_capture.py`: a `ScreenCapturer` thread that captures the `CGDirectDisplayID` of the screen where the combo started. The first frame is taken immediately, then one every 500 ms on a drift-free schedule.
- [ ] Encode JPEG through ImageIO (`CGImageDestination`, quality 0.85) to `frame_0001.jpg`, and so on.
- [ ] `processing.py`: an `ImageProcessingWorker` (a `queue.Queue` and a daemon thread) that calls `process_image(frame)`. `process_image` does nothing for now.

**Done when:** holding the combo for 3 s produces about 7 frames. The streak is visible in them, the caption panel isn't, and the worker logs each frame.

## Phase 5: Audio capture and real-time audio processing

**Goal:** the mic is live only during a session. Chunks are streamed to a stub processor and saved to WAV.

- [ ] `audio_capture.py`: an `AudioRecorder` thread that opens a PyAudio stream (paInt16, 1 channel, 16 kHz, 1600 frames) when the session starts and closes it on stop.
- [ ] Convert each chunk to float32 with the same formula as `asr/main.py` and send it to the `AudioProcessingWorker` queue.
- [ ] Write the raw int16 data to `audio.wav` using the `wave` module.
- [ ] `processing.py`: an `AudioProcessor` stub with `start_session()`, `process_chunk(chunk) -> str | None`, and `end_session() -> str | None`. All of them return `None` for now.

**Done when:** a 3 s session produces about 30 chunks and a playable `audio.wav` of about 3 s. The mic indicator turns off after release.

## Phase 6: Transcript caption panel

**Goal:** show transcribed text in a caption bar.

- [ ] `caption.py`: a `CaptionPanel` (a non-activating, borderless `NSPanel` with an `NSVisualEffectView` in HUD style, rounded corners, and white text) at the bottom center of the screen where the combo started.
- [ ] Show "Listening…" when a session starts, and update the text whenever the audio processor returns some (sent to the main thread).
- [ ] Exclude the panel from screenshots (`NSWindowSharingNone`).
- [ ] Hide the panel 2 s after the session stops. Store the final text as the "last transcript" for the menu item.

**Done when:** the panel appears and disappears correctly, and fake text (temporarily returned by the stub) shows up live.

## Phase 7: Integration and hardening

**Goal:** a stable end-to-end app.

- [ ] `SessionController` handles start and stop in order. Starting while already active or stopping while idle does nothing.
- [ ] Rapid press/release, holding for a long time (more than 60 s), and quitting during a session all clean up properly: threads joined, stream closed, windows closed, WAV finalized.
- [ ] Errors in one subsystem (for example, missing mic permission) are logged and don't break the others.
- [ ] Run the manual test checklist (see the Specification, §16).

**Done when:** every item on the checklist passes.

## Phase 8 (future): Real transcription and image analysis

- [ ] Replace the `AudioProcessor` stub with a wrapper around `asr/main.py` (`load_model`, `transcribe`) that reuses its VAD and partial-transcript logic. Load the model lazily at app start on a background thread.
- [ ] Implement real logic in `process_image`.
