# Streak Catcher: Implementation Plan

A macOS menu bar app written in Python. While **Left Control + Left Shift + Left Option** are held down, it:

1. Draws a **light streak** trail behind the mouse cursor.
2. Records **microphone** audio and sends each chunk to a real-time processor. The processor will later produce transcribed text, which the app shows in a caption panel.
3. Takes a **screenshot** of the screen where the combo started, every 500 ms, and sends each image to a post-processor.

Technical details are in [SPECIFICATIONS.md](./SPECIFICATIONS.md).

---

## Working rules (apply to every request on this project)

- This is a **toy project**. Rough edges are OK: prefer simple, working code over polish, edge-case handling, or abstractions.
- Keep testing **minimal**: just enough to confirm the basic core workflow works (hold the combo → streak + screenshots + audio → release → session folder with frames and `audio.wav`).
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

## Decisions (agreed)

| Area | Decision |
|---|---|
| Location | Modules live directly in the repo root (`./`). No `streak_catcher/` package folder |
| Environment | Existing root uv venv (`.venv`, Python 3.14). Dependencies go in the root `pyproject.toml` |
| Run mode | Python script: `uv run main.py`. Permissions are granted to Terminal or VS Code |
| Menu bar | Pure PyObjC `NSStatusBar`, accessory app (no Dock icon) |
| Menu items | Enable/Disable hotkey, Open captures folder, Show last transcript, Quit |
| Hotkey | `pynput`. Left Ctrl + Left Shift + Left Option held; other modifiers ignored |
| Streak | White/cyan glow, short fade (`streak_fade_s`). Visible in screenshots until Phase 10, which switches to clean frames with app-drawn annotations |
| Capture | Quartz `CGDisplayCreateImage`, display where the combo started (fixed). From Phase 10: grabbed every 500 ms into an in-memory buffer; only frames chosen by triggers (start, gesture start and end, app switch, 2 s safety net, release) are saved, after changed-area dedup. Frames are clean (streak excluded) |
| Images | JPEG (quality 0.85, half Retina size via `capture_scale = 0.5`, ~500 KB per frame) saved to a per-session folder, then passed to a per-image background worker. A restart within `session_reuse_window_s` (default 10 s) of the previous stop continues the same folder |
| Audio | PyAudio, 16 kHz, mono, int16 → float32, 1600-sample (100 ms) chunks (the format the `asr` module expects) |
| Audio save | Session audio is also saved as `audio.wav` |
| STT | `cactus-compute` Whistle (16 MB model), run in-process and preloaded at app start (~0.3 s load). `stream()` over ~1 s chunks gives live committed and pending text and word timestamps. Telemetry off. Replaces the earlier Parakeet/NeMo plan (Phase 8) |
| Gestures | Detected from the recorded cursor path (not image processing). Circle and back-and-forth underline only (Phase 9) |
| LLM input | One annotated overview per visual context with numbered boxes, plus a transcript with `[n]` markers. No crops. Image cap configurable (Phase 11) |
| LLM | Switchable provider interface. GitHub Copilot SDK first. Response shown in a dismissable panel (Phases 12–13) |
| Transcript UI | Floating caption panel at the bottom of the screen |
| Tests | Minimal. Only check that the basic core workflow works (see Working rules) |

---

## Phase 0: Scaffold and environment

**Goal:** an empty, runnable app in the repo root.

- [x] Add dependencies to the root `pyproject.toml` with `uv add` (`pyaudio`, `pynput`, `pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz`, `numpy`).
- [x] Verify that `CGDisplayCreateImage` works on macOS 26 and the pynput left-modifier codes are correct (59 / 56 / 58).
- [x] Create the root files: `main.py` (entry point) and `config.py`.
- [x] Add `captures/` to `.gitignore`.

**Done when:** `uv run main.py` starts and exits cleanly, printing the config.

## Phase 1: Menu bar app shell and permissions

**Goal:** a status bar icon with a working menu and no Dock icon.

- [x] `app.py`: `NSApplication` with the accessory activation policy, `AppDelegate`, and an `NSStatusItem` using the SF Symbol `sparkles`.
- [x] Menu items: Enable/Disable hotkey (checkmark state), Open captures folder, Show last transcript (`NSAlert`), Quit.
- [x] `permissions.py`: check and request Accessibility, Screen Recording, and Microphone. Print the status of each at startup.
- [x] Ctrl+C in the terminal quits cleanly (`AppHelper.installMachInterrupt()` before `AppHelper.runEventLoop()`; `installInterrupt=True` is skipped when `NSApp` already exists).

**Done when:** the icon appears, each menu item works, and Quit and Ctrl+C both exit.

## Phase 2: Hotkey detection

**Goal:** reliable start and stop callbacks for the combo.

- [x] `hotkey.py`: a `pynput.keyboard.Listener` that keeps a set of held left-modifier key codes.
- [x] Fire `on_activate` when all three are down and `on_deactivate` when any of them is released. Each fires once per transition (edge-triggered).
- [x] Send callbacks to the main thread through `AppHelper.callAfter`.
- [x] Watchdog: on the main thread, poll `NSEvent.modifierFlags()` during a session. If Ctrl, Shift, or Option is no longer down, force a stop, in case pynput missed a key release.
- [x] Respect the Enable/Disable toggle. Disabling during a session stops it.

**Done when:** logs show exactly one start and one stop per press and release. Right-side modifiers don't trigger a session.

## Phase 3: Light streak overlay

**Goal:** a glowing trail that follows the cursor on any screen.

- [x] `streak.py`: a `Trail` model holding `(x, y, t)` points in global Cocoa coordinates and dropping points older than 250 ms.
- [x] One borderless, transparent, click-through `NSWindow` per `NSScreen`, at `NSScreenSaverWindowLevel`, joining all Spaces.
- [x] A `StreakView` (NSView) that draws tapered segments in three passes (outer cyan glow, inner cyan, white core) with alpha based on each point's age.
- [x] A 120 Hz `NSTimer` samples `NSEvent.mouseLocation()` and redraws.
- [x] On stop: stop sampling, let the trail fade out, then close the windows.

**Done when:** the streak is smooth, fades in ~250 ms, works across displays, and doesn't block clicks.

## Phase 4: Screen capture and image post-processing

**Goal:** a JPEG every 500 ms, each handed to a stub post-processor.

- [x] `session.py`: create the session folder `captures/YYYYmmdd-HHMMSS/`.
- [x] `screen_capture.py`: a `ScreenCapturer` thread that captures the `CGDirectDisplayID` of the screen where the combo started. The first frame is taken immediately, then one every 500 ms on a drift-free schedule.
- [x] Encode JPEG through ImageIO (`CGImageDestination`, quality 0.85) to `frame_0001.jpg`, and so on.
- [x] `processing.py`: an `ImageProcessingWorker` (a `queue.Queue` and a daemon thread) that calls `process_image(frame)`. `process_image` does nothing for now.

**Done when:** holding the combo for 3 s produces about 7 frames. The streak is visible in them, the caption panel isn't, and the worker logs each frame.

## Phase 5: Audio capture and real-time audio processing

**Goal:** the mic is live only during a session. Chunks are streamed to a stub processor and saved to WAV.

- [x] `audio_capture.py`: an `AudioRecorder` thread that opens a PyAudio stream (paInt16, 1 channel, 16 kHz, 1600 frames) when the session starts and closes it on stop.
- [x] Convert each chunk to float32 (`np.frombuffer(data, np.int16).astype(np.float32) / 32768.0`, see the Specification, §9) and send it to the `AudioProcessingWorker` queue.
- [x] Write the raw int16 data to `audio.wav` using the `wave` module. When the session folder is reused (restart within `session_reuse_window_s`), write `audio_2.wav`, `audio_3.wav`, … per segment so nothing is overwritten.
- [x] `processing.py`: an `AudioProcessor` stub with `start_session()`, `process_chunk(chunk) -> str | None`, and `end_session() -> str | None`. All of them return `None` for now.

**Done when:** a 3 s session produces about 30 chunks and a playable `audio.wav` of about 3 s. The mic indicator turns off after release.

## Phase 6: Transcript caption panel

**Goal:** show transcribed text in a caption bar.

- [x] `caption.py`: a `CaptionPanel` (a non-activating, borderless `NSPanel` with an `NSVisualEffectView` in HUD style, rounded corners, and white text) at the bottom center of the screen where the combo started.
- [x] Show "Listening…" when a session starts, and update the text whenever the audio processor returns some (sent to the main thread).
- [x] Exclude the panel from screenshots (`NSWindowSharingNone`).
- [x] Hide the panel 2 s after the session stops. Store the final text as the "last transcript" for the menu item.

**Done when:** the panel appears and disappears correctly, and fake text (temporarily returned by the stub) shows up live.

## Phase 7: Integration and hardening

**Goal:** a stable end-to-end app.

- [ ] `SessionController` handles start and stop in order. Starting while already active or stopping while idle does nothing.
- [ ] Rapid press/release, holding for a long time (more than 60 s), and quitting during a session all clean up properly: threads joined, stream closed, windows closed, WAV finalized.
- [ ] Errors in one subsystem (for example, missing mic permission) are logged and don't break the others.
- [ ] Run the manual test checklist (see the Specification, §16).

**Done when:** every item on the checklist passes.

## Phases 8–13: Real transcription, gestures, and LLM (design agreed, not started)

Design details are in the Specification, §17. Summary of the flow: while the combo is held, audio is transcribed live in-process by cactus `Whistle.stream()` (committed and pending text in the caption), the cursor path is recorded and checked for **circle** and **underline** gestures, and frames are captured on events (not only on a timer). On release, the image job starts immediately while the transcript finishes in the background. Frames are grouped into **contexts** (visually similar frames), each rendered as one annotated overview with numbered boxes, and the transcript gets matching `[1]`, `[2]` markers. The result goes to a switchable LLM provider (GitHub Copilot SDK first), and the answer appears in a dismissable panel.

**Copilot SDK and images:** confirmed. `session.send(prompt, attachments=[{"type": "file", "path": "/abs/path.png"}])` accepts image files, so the annotated context images from Phase 11 are attached by path.

**Tunable values live in `config.py`, not in code.** Defaults are listed under [Resolved decisions](#resolved-decisions-phases-813) and are expected to be tuned from real sessions.

## Phase 8: Real-time transcription with cactus (Whistle) and model preload

**Goal:** fast, lightweight live transcription with word timestamps, ready seconds after launch.

ASR engine: **`cactus-compute`** (`cactus.agent.whistle.Whistle`), following the user's reference streaming script. The model is a 16 MB `whistle.cact` file plus a small native runtime, both fetched from Hugging Face on first use and cached in `~/.cache/huggingface/hub`. It runs **in-process**; no torch or NeMo, no worker process. Measured on this Mac:

| | |
|---|---|
| Import + model load | ~50 ms + ~280 ms |
| `stream()` pass per 1 s chunk of speech | 16–30 ms |
| Tail committed after audio ends | ~100 ms |
| Output per chunk | committed `text` + `words` (`word`, `start`, `end`, `probability`; seconds from stream start), unconfirmed `pending` tail, `language`, `received`, `pass_ms` |

- [x] `uv add cactus-compute`. Set `CACTUS_TELEMETRY=0` before import (the package sends anonymous usage counts by default; config `cactus_telemetry = False`).
- [x] Preload at app start on a background thread: create `Whistle()` (downloads weights on first run) and run one short warm-up `transcribe`. A disabled menu line shows "Loading speech model…", then "Speech model ready", or "Speech model unavailable" with the error logged.
- [x] Replace the `AudioProcessor` stub with a `CactusProcessor` on the `audio-worker` thread. Per session segment it runs one `Whistle.stream()` over a generator that **regroups the recorder's 100 ms chunks into ~1 s chunks**. The generator ends on the session's `end` message, which makes cactus commit the pending tail.
- [x] Caption shows committed text plus the dimmed `pending` tail, updated every ~1 s. The final transcript is the committed texts joined with spaces.
- [x] Word times are relative to the segment's audio start. Convert them to session time (add the recorder's start offset) so gestures and words share one clock.
- [x] Sessions before the model is ready are allowed: chunks queue (bounded) and are streamed once the model is loaded.
- [x] Config: `asr_language` (`"en"`; `None` = auto-detect among en/de/fr/es/it/nl/pl), `asr_keywords` (words or phrases for keyword biasing, empty by default), `asr_chunk_s` (1.0). `stream()` holds a process-wide lock, so only one stream runs at a time; sessions are sequential, so this is fine.

**Done when:** the menu line goes from loading to ready within about a second of launch (after the first-run download), speech shows live committed and pending text in the caption, and the final transcript has word timestamps on the session clock.

## Phase 9: Cursor path recording and gesture detection

**Goal:** know when the user circled or underlined something, and where.

- [x] Record the cursor path for each session `(t, x, y)` from the streak sampler (120 Hz) and save it as `path.json` in the session folder (also used to tune thresholds).
- [x] Segment the path into strokes at pauses: speed below `gesture_pause_speed_pt_s` for at least `gesture_pause_s`.
- [x] Classify strokes (all thresholds in config): **circle**: cumulative turning ≥ `circle_min_turn_deg`, end-to-start distance ≤ `circle_close_ratio` × bounding-box diagonal, aspect ≤ `circle_max_aspect`, size ≥ `gesture_min_size_pt`. **Underline**: vertical extent ≤ `underline_max_dy_ratio` × horizontal extent, width ≥ `underline_min_dx_pt`, at least `underline_min_reversals` direction reversals (↔). Everything else is ordinary cursor movement and is ignored.
- [x] Emit `Gesture(index, kind, t_start, t_end, bbox)` events during the session.
- [x] `.tmp/scripts` replay check: recorded paths go in, the expected gestures come out.
- [x] Debug images: with `STREAK_DEBUG=1` (`gesture_debug_images`), each gesture is drawn (path + numbered box) on the nearest saved frame as `debug_gesture_N.jpg` in the session folder.

**Done when:** deliberate circles and ↔ underlines are detected with correct bounding boxes and times, and normal cursor travel (including fast horizontal moves) produces no gestures.

## Phase 10: Event-driven frame capture

**Goal:** few frames, but the right ones.

- [x] Clean frames: exclude the streak overlay windows from capture (`NSWindowSharingNone`). The app draws its own annotations later.
- [x] Rolling in-memory buffer: capture every `frame_buffer_interval_s` and keep the last `frame_buffer_s` seconds as encoded JPEG bytes. Nothing is written to disk until a frame is chosen.
- [x] Triggers that persist a frame: session start, gesture start (the buffered frame just before `t_start`), gesture end, frontmost-app switch (`NSWorkspace` activation notification), release, and a safety net every `safety_net_interval_s`.
- [x] Dedup by **changed area**: grayscale thumbnails `dedup_thumb_width` px wide; a pixel counts as changed when it differs by more than `dedup_pixel_delta`; frames are duplicates when less than `dedup_changed_pct`% of pixels changed. Safety-net frames can be dropped; gesture frames are only merged, never dropped.
- [x] Write `frames.json` (frame → trigger, time, app, gesture refs).

**Done when:** a 10 s session with two gestures and one app switch leaves only a handful of frames on disk, none of them near-duplicates, and the gesture frames show the screen as it was when each gesture began.

## Phase 11: Contexts, annotation, and request assembly

**Goal:** the smallest input that still carries everything the LLM needs.

- [ ] Group frames into **contexts** by visual similarity (the same changed-area metric). Returning to a screen merges into its context.
- [ ] Render one **annotated overview per context**: numbered bounding boxes for every gesture in that context, drawn on that context's frame. No crops.
- [ ] Insert `[n]` markers into the transcript at each gesture's time, using word timestamps.
- [ ] On release, start the image job immediately and wait for the final transcript for up to `assembly_timeout_s`, with the caption showing "Transcribing…". If the model is still loading, wait up to `model_wait_timeout_s` ("Waiting for speech model…"). On timeout, use the text committed so far and log it. Transcription never blocks capture or the next session.
- [ ] Treat very short, low-confidence finals as no speech: Whistle can hallucinate words such as "you" on near-silent input (seen in Phase 8). Thresholds go in config.
- [ ] Edge cases: talking with no gestures sends the unique deduped frames. Gestures with no speech show a brief caption "No instructions heard" and nothing is sent.
- [ ] `max_images` (`None` = no cap) and `truncate_keep` (`"end"` by default, keeping the most recent contexts; or `"start"`).
- [ ] Save the assembled request (`request.json` plus the annotated images) in the session folder for debugging.

**Done when:** sample sessions produce the expected contexts, boxes, and markers, and a gesture-only session exits gracefully.

## Phase 12: LLM provider interface

**Goal:** send the request and get text back, with a provider that can be switched.

- [ ] Prerequisites: `uv add github-copilot-sdk` (Python 3.11+). Install the GitHub Copilot CLI (`copilot`), which the SDK runs in server mode, and run `copilot auth login` once. The SDK reuses those credentials (or `COPILOT_GITHUB_TOKEN`).
- [ ] `llm.py`: an `LLMProvider` interface (`respond(request) -> str`), with the implementation chosen by `llm_provider`.
- [ ] `CopilotProvider`: one `CopilotClient` started at app launch on a dedicated asyncio thread. One `create_session(model=llm_model)` per request (no cross-session memory), then `session.send(prompt, attachments=[{"type": "file", "path": ...}])` with the annotated context images from Phase 11. Timeout `llm_timeout_s`. `copilot_cli_path` for a non-PATH CLI. Errors are shown in the response panel.
- [ ] Prompt template that explains the numbered boxes and the `[n]` markers.

**Done when:** a real session produces an LLM response using the Copilot provider, and switching the provider in config works.

## Phase 13: Response panel

**Goal:** show the answer.

- [ ] `response_panel.py`: same dark HUD style as the caption, bottom-center of the session's screen, replacing the caption. Width `min(response_max_width_pt, response_width_fraction × screen)`. Grows with the text up to `response_max_height_fraction` of the screen, then scrolls.
- [ ] Renders markdown (`response_markdown`; `NSAttributedString` markdown init). Has a **Copy** button and a **✕** button. Accepts mouse events, but is non-activating so the user's app keeps focus. Excluded from screenshots.
- [ ] Dismissed by ✕, Esc (through the existing pynput listener, since the panel never takes focus), or the next session. Optional `response_auto_dismiss_s` (`None` = stays).
- [ ] Shows "Thinking…" while waiting for the LLM, then the response text.

**Done when:** responses are readable, long ones fit or scroll, and dismissing works.

## Resolved decisions (Phases 8–13)

All questions are answered. Every tunable value is a `config.py` field; the defaults below are starting points, to be tuned from real sessions (`path.json`, `frames.json`).

| Topic | Decision | Config (default) |
|---|---|---|
| ASR | cactus Whistle in-process, telemetry off | `asr_language` (`"en"`), `asr_keywords` (`()`), `asr_chunk_s` (`1.0`), `cactus_telemetry` (`False`) |
| Copilot images | Supported as file attachments by absolute path | — |
| Strokes | Split at pauses; speed over a short window; resampled before measuring turns | `gesture_pause_s` (`0.15`), `gesture_pause_speed_pt_s` (`60`), `gesture_speed_window_s` (`0.05`), `gesture_resample_pt` (`4`), `gesture_min_size_pt` (`30`) |
| Circle | Loop found anywhere inside a stroke (turning + closure + aspect); repeated loops merge | `circle_min_turn_deg` (`300`), `circle_close_ratio` (`0.35`), `circle_max_aspect` (`3.0`), `gesture_merge_overlap` (`0.6`, same stroke), `gesture_merge_iou` (`0.5`, across pauses) |
| Underline | Flat ↔ legs with reversals; short legs (overshoot fixes) folded away | `underline_max_dy_ratio` (`0.3`), `underline_min_dx_pt` (`60`), `underline_min_leg_pt` (`30`), `underline_min_reversals` (`1`); robust line-fit trim: `underline_band_pt` (`12`), `underline_max_slope` (`0.25`), `underline_max_gap_pts` (`6`) |
| Rolling buffer | Keep the 500 ms cadence; the stroke in progress is always kept | `frame_buffer_interval_s` (`0.5`), `frame_buffer_s` (`4.0`), `frame_buffer_max` (`60`), `safety_net_interval_s` (`2.0`), `app_switch_settle_s` (`0.3`), `frames_include_streak` (`False`) |
| Dedup / contexts | Changed area on grayscale thumbnails | `dedup_thumb_width` (`256`), `dedup_pixel_delta` (`24`), `dedup_changed_pct` (`3.0`) |
| Image cap | No cap; keep the most recent when capped | `max_images` (`None`), `truncate_keep` (`"end"`) |
| Waiting for transcript | Caption shows status; fall back to committed text | `assembly_timeout_s` (`5.0`), `model_wait_timeout_s` (`60.0`) |
| LLM | Copilot SDK, new session per request | `llm_provider` (`"copilot"`), `llm_model` (`"gpt-5.4"`), `llm_timeout_s` (`60.0`), `copilot_cli_path` (`None`) |
| Response panel | Bottom-center, scrolls, markdown, Copy and ✕, Esc | `response_max_width_pt` (`900`), `response_width_fraction` (`0.7`), `response_max_height_fraction` (`0.5`), `response_markdown` (`True`), `response_auto_dismiss_s` (`None`) |
