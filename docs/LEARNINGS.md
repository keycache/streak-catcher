# Learnings

Non-obvious technical gotchas hit while building Streak Catcher: surprising behavior of libraries, macOS, or tooling. Plan or documentation mismatches (stale checkboxes, outdated paths, project bookkeeping) are just fixed in place and not recorded here. Each entry has:

- **What**: the symptom.
- **Why**: the root cause.
- **Fix**: what was changed.
- **Avoid next time**: the check or habit that would have saved the detour.

---

## 1. Ctrl+C did not quit the app (Phase 1)

**What:** `uv run main.py` ignored SIGINT. The menu bar app kept running after Ctrl+C, while menu-driven Quit worked.

**Why:** `AppHelper.runEventLoop(installInterrupt=True)` only installs its SIGINT handler on the *first run*, which it detects as `NSApp() is None`. `app.py` calls `NSApplication.sharedApplication()` beforehand to set the accessory activation policy, so `NSApp` already existed and the handler was silently skipped. A plain Python `signal` handler doesn't help either, because Python signal handlers don't run while the Cocoa run loop is blocking.

**Fix:** call `AppHelper.installMachInterrupt()` explicitly right before `AppHelper.runEventLoop()` ([app.py](../app.py), `run()`). Shutdown logic lives in `AppDelegate.shutdown()`, which is idempotent and is called from `applicationWillTerminate_` and from a `finally` after the run loop, so both exit paths clean up the same way.

**Avoid next time:** when a library flag "does nothing", read the library source before guessing (`.venv/lib/python3.14/site-packages/PyObjCTools/AppHelper.py`). Also, test Ctrl+C automatically from the start by sending SIGINT from a subprocess harness (see `.tmp/scripts/sigint_check.py`), not only by hand.

---

## 2. Headless AppKit test hung with no output (Phase 1)

**What:** a scripted menu test (driving `NSMenu.performActionForItemAtIndex_` via `AppHelper.callLater`) froze after the "Open Captures Folder" step and printed nothing more.

**Why:** two things combined:

1. The test used `mock.patch.object(NSWorkspace, "sharedWorkspace")`. Patching or restoring attributes on an **Objective-C class** raises, unlike on a normal Python class.
2. An exception escaping a callback inside `AppHelper.runEventLoop()` is handled by `unexpectedErrorAlertPanel`, which shows a **modal `NSRunAlertPanel`** and blocks forever in a headless run. Output piped through `grep` was also block-buffered, so even the earlier prints didn't appear at first.

**Fix:** patch the *module-level name* instead (`mock.patch.object(app, "NSWorkspace", fake)`), wrap the test driver in `try/except` that prints the traceback and calls `NSApp.terminate_(None)`, and run with `python -u` / write output to a log file.

**Avoid next time:**

- Never monkeypatch PyObjC classes; patch the name in the importing module.
- In anything run inside the Cocoa event loop (tests *and* app callbacks), catch exceptions yourself and log them (`log_exc`), so an error can't turn into an invisible modal dialog.
- For background runs, use unbuffered output (`-u`, `flush=True`) and a log file under `.tmp/logs/`.
- Modal UI (`NSAlert.runModal`) can't be tested headlessly; leave it to the manual checklist.

---

## 3. macOS shell gaps when scripting checks

**What:** `timeout` is not available on macOS (`command not found`), and `uv` may not be on `PATH` in non-login shells (it lives in `~/.local/bin`).

**Fix:** use a Python `subprocess` harness with `communicate(timeout=...)` for time-limited runs, and call `uv` with `~/.local/bin` on `PATH` when needed.

**Avoid next time:** write check scripts in Python under `.tmp/scripts/` and run them with `uv run`, rather than relying on GNU shell tools.

---

## 4. Code after `AppHelper.runEventLoop()` never runs (Phase 2)

**What:** a scripted check stopped the app with `AppHelper.stopEventLoop()` and then printed a pass/fail summary after `runEventLoop()` returned. The summary never appeared, and the process exited with code 0 even when it should have reported failures.

**Why:** when `NSApp` is running, `stopEventLoop()` ends in `NSApp.terminate_()`, and `terminate_` calls `exit()` right after `applicationWillTerminate_`. Control never returns to the Python code after `runEventLoop()`, so `finally` blocks and trailing statements are skipped.

**Fix:** all app cleanup lives in `AppDelegate.shutdown()`, called from `applicationWillTerminate_` (the `finally` in `app.run()` only covers paths that do return). Check scripts report results from a hook appended to `delegate.shutdown_hooks` and exit with `os._exit(code)`.

**Avoid next time:** treat `applicationWillTerminate_` as the *only* reliable place for shutdown work in a Cocoa app, and never put assertions or summaries after `runEventLoop()`.

---

## 5. Two modules defining the same `NSObject` subclass name crash on import (Phase 3)

**What:** after adding `streak.py`, the app failed to start with `objc.error: _TimerTarget is overriding existing Objective-C class`.

**Why:** `controller.py` and `streak.py` each defined a private helper `class _TimerTarget(NSObject)`. Python treats them as separate classes, but PyObjC registers every `NSObject` subclass in the **single, process-wide Objective-C class namespace**, so the second definition clashes. The leading underscore doesn't make it module-private.

**Fix:** gave each a unique name (`WatchdogTimerTarget`, `StreakTimerTarget`).

**Avoid next time:** give every `NSObject` / `NSView` subclass a project-unique name, and run the earlier phase checks (`.tmp/scripts/phase*_check.py`) after adding a module, since that is what caught it.

---

## 6. `CGDisplayCreateImage` pixel buffer is larger than `height × bytesPerRow` (Phase 3)

**What:** reading a screenshot into numpy with `reshape(h, bytesPerRow)` failed: `cannot reshape array of size 30883840 into shape (2234,13824)`.

**Why:** the buffer returned by `CGDataProviderCopyData` has extra trailing bytes (1 KB here), and each row may also be padded past `width × 4`.

**Fix:** slice before reshaping: `buf[: h * bpr].reshape(h, bpr)[:, : w * 4].reshape(h, w, 4)`. Pixels are BGRA.

**Avoid next time:** never assume a CoreGraphics buffer is exactly `w × h × 4`; always use `CGImageGetBytesPerRow` and slice.

---

## 7. Bluetooth headset mic delivers only ~60% of the expected audio (Phase 5)

**What:** a 3 s session produced 18 audio chunks (1.8 s of WAV) instead of ~30. Chunks arrived every ~170 ms on average instead of every 100 ms, from the very first chunk, so it wasn't startup latency.

**Why:** the system default input was a pair of Bluetooth earbuds (`soundcore R50i VI`). A per-device probe (`.tmp/scripts/mic_rate_probe.py`) showed that device delivering 0.61–0.62× of its nominal 16 kHz, even at its own reported default rate. Every other input (built-in mic, virtual devices) ran at 0.98–1.00×. So it's the headset mic path, not the recorder. The user's `asr/main.py` gets the same short audio from that device.

**Fix:** the recorder logs the device name for each recording. `config.audio_input_device` selects a mic by name substring. After 1 s of audio, the recorder compares the audio received against wall time and warns below 0.9×.

**Avoid next time:** when audio counts are off, first log *which device* is recording and measure samples per second per device with a bare PyAudio loop, before suspecting the pipeline. Don't assume the default input is the built-in mic.

---

## 8. PortAudio caches the device list at `PyAudio()` init (Phase 5)

**What:** with one `PyAudio()` created at app start (the original spec), a headset connected or switched to after launch wouldn't be visible or used. The "default input" would stay whatever it was at launch.

**Why:** PortAudio enumerates devices and the default device once, in `Pa_Initialize` (`pyaudio.PyAudio()`), and never refreshes them.

**Fix:** each recording creates and terminates its own `PyAudio()` on the `audio-capture` thread. After the first init in a process, this costs about 7 ms.

**Avoid next time:** for long-running apps, don't hold a PortAudio instance across sessions when the user may change audio devices.

---

## 9. HUD `NSVisualEffectView` renders light gray in Light Mode (Phase 6)

**What:** the caption panel used `NSVisualEffectMaterialHUDWindow` with white text, as specified. All automated checks passed, but a cropped capture showed white text on a light gray background, which was hard to read.

**Why:** visual-effect materials take their tint from the window's effective appearance. The app is an accessory app with no explicit appearance, so in Light Mode the "HUD" material renders light, not the dark translucent look the name suggests.

**Fix:** `panel.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameVibrantDark))`.

**Avoid next time:** for any custom-styled window, actually look at it in the current system appearance (crop a capture). Since the caption is excluded from screenshots, temporarily set `NSWindowSharingReadOnly` to capture it. Functional checks can't catch contrast problems.

---

## 10. cactus sends telemetry by default (Phase 8)

**What:** `cactus.transcribe()` and `cactus.stream()` call an internal `track()` that POSTs anonymous usage counts (function name, package and engine version, OS, arch, Python version, an anonymous id) to a Supabase endpoint. No audio or text is sent. The user's reference script didn't show this.

**Why:** it's on by default in `cactus/_telemetry.py`. It's disabled by `CACTUS_TELEMETRY=0`, `DO_NOT_TRACK`, or `CI`, and the variable is read at **import time**.

**Fix:** `asr.py` sets `CACTUS_TELEMETRY` from `config.cactus_telemetry` (default `False`) before anything imports cactus.

**Avoid next time:** when adding an ML or SDK dependency, grep its package for `urllib`, `requests`, `telemetry`, `track`, and `analytics` before wiring it in.

---

## 11. Whistle transcribes near-silence as "you" (Phase 8)

**What:** a session that only recorded room noise produced the final transcript `'you'`.

**Why:** like Whisper-family models, Whistle can hallucinate a short common word on near-silent audio. Pure digital silence is skipped, but real mic noise isn't.

**Fix (Phase 11):** confidence does **not** help: in a real session, hallucinated "Thank you. … you." scored p = 0.89–1.0, higher than the real word "Try" (0.67). What separates them is **audio loudness**: hallucinated words sat on ~0.001–0.002 RMS (noise floor ~0.003), real speech on 0.019–0.044. Words whose surrounding 100 ms chunks are below `speech_min_rms` (0.008) are dropped.

**Avoid next time:** test ASR with real ambient mic noise, not only with silence or clean speech.

---

## 12. `print()` from several threads can merge lines (Phase 8)

**What:** log lines from `asr-stream`, `audio-worker`, and the main thread ran together on one line.

**Why:** `print(msg)` writes the message and the trailing newline as separate writes, so another thread's output can land between them.

**Fix:** `log()` does a single `sys.stdout.write(line + "\n")` followed by `flush()`.

**Avoid next time:** in multi-threaded code, log through one function that writes each line in a single call.

---

## 13. Real circling is a continuous spiral, not separate clean loops (Phase 9)

**What:** the first real session (circling one area while talking for 5 s) produced **3 circle gestures** instead of 1. Synthetic tests had passed.

**Why:** the user circled about 8 times **without ever pausing**, in a slowly shrinking spiral with a small kink loop near the end. The detector split it into loops correctly, but the merge rule (IoU ≥ 0.5) can't merge a small loop lying *inside* a big one: containment gives a low IoU.

**Fix:** within one continuous stroke, merge consecutive loops by **overlap ÷ smaller area** (`gesture_merge_overlap`). Across pauses, keep the stricter IoU rule. The recorded path is now a regression fixture (`.tmp/fixtures/path_spiral_173510.json`).

**Avoid next time:** synthetic gestures are too clean. Record a few real sessions (`path.json`), render them (`.tmp/scripts/plot_path.py`), and add them as fixtures before trusting thresholds.

---

## 14. Legacy screen capture APIs can stall for seconds and return `None` (Phase 9)

**What:** during a check run, every `CGDisplayCreateImage` call blocked for about **5 s** and returned `None`. `CGWindowListCreateImage` blocked for **30 s**. The session got 0 frames. At the same time, Screen Recording preflight returned `True`, the display was awake and unlocked, no permission dialog was on screen, and Apple's `screencapture` CLI (ScreenCaptureKit) worked in 0.27 s. Captures had worked from the same host app an hour earlier and 8 minutes earlier. The root cause is not known.

**Why (suspected):** the deprecated CoreGraphics capture path goes through a WindowServer service that can stall independently of ScreenCaptureKit.

**Update (Phase 11):** it recurred, and **ScreenCaptureKit** (`SCScreenshotManager`) also hung (30 s, no callback) from the same process, while the `screencapture` CLI still worked and the user's own runs from the VS Code terminal saved frames normally. The stalls hit processes launched under the Copilot agent runtime (a different responsible process for TCC), not the app as the user runs it. The Phase 11 check uses a synthetic display (patching `screen_capture.Quartz.CGDisplayCreateImage`) so it doesn't depend on the capture service; older capture-dependent checks still need a working capture service.

**Fix:** the capture log now reports the elapsed time and says "capture service stalled" when a `None` takes longer than 2 s, instead of blaming permissions. If it recurs, fall back to ScreenCaptureKit (`SCScreenshotManager`).

**Avoid next time:** when a capture returns `None`, log how long it took. A fast `None` means permission or display trouble; a slow one means a stalled service.

---

## 15. Approach travel gets absorbed into an underline (Phase 9)

**What:** a real underline came out as a 575×141 box starting 1.2 s early. The debug image showed the slanted approach move (from the bottom right up to the text line) included in the underline.

**Why:** the user moved to the text and started scribbling **without pausing**, so approach and underline were one stroke. The approach leg was 460 pt wide and about 140 pt tall, which is just inside the "flat enough" ratio (0.3), so it counted as the first underline leg.

**Fix:** after finding a candidate, fit the dominant near-horizontal line robustly (RANSAC plus a least-squares refit on inliers), keep only the longest run of points within a ±12 pt band, and re-validate. The box went to 398×23 and the start time to the real start of the scribble. The session is now a fixture (`.tmp/fixtures/path_underline_approach_181302.json`).

**Avoid next time:** per-leg shape tests are fooled by travel that happens to be shaped like a leg. When most points share one structure (a line, a loop), fit that structure robustly and measure the gesture from its inliers.

---

## 16. Decoded JPEGs are RGBA; display captures are BGRA (Phase 10)

**What:** pixel checks on saved frames reported a red panel as blue (and the reverse), and a magenta-box check found 0 magenta pixels, while the images themselves were correct.

**Why:** `CGDisplayCreateImage` returns 32-bit little-endian **BGRA**, but a JPEG decoded with `CGImageSourceCreateImageAtIndex` comes back **RGBA** (or RGBX). Reading `CGDataProviderCopyData` bytes with one fixed channel order silently swaps red and blue for the other source.

**Fix:** to inspect pixels, redraw the image into a `CGBitmapContext` with a known layout (`kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little` = BGRA), then read that buffer.

**Avoid next time:** never assume a `CGImage`'s byte layout. Normalize through a bitmap context, or check `CGImageGetBitmapInfo` and `CGImageGetAlphaInfo`, before indexing channels.

---

## 17. A colored annotation box can vanish on a same-hue background (Phase 11)

**What:** numbered gesture boxes were drawn correctly, but a magenta box on a red screen was nearly invisible, both to the eye and to a pixel check.

**Why:** a single-color stroke only stands out where the background contrasts with it. Real screens can be any color, and the LLM must see the box.

**Fix:** draw a wider contrasting halo (white, `annotation_halo_rgba`) under every box and badge, then the colored stroke on top.

**Avoid next time:** test overlays and annotations on several background colors, including ones close to the annotation's own color.

---

## 18. Copilot models: the configured default didn't exist, and most accept only one image (Phase 12)

**What:** the first real call failed with `Model "gpt-5.4" is not available`. `list_models()` showed that nearly every model supports vision but with `max_prompt_images = 1`; only Claude Sonnet and Haiku accept 5. A multi-context request (several annotated screenshots) would fail or be silently cut on most models.

**Why:** model availability is per account and changes over time, and per-model vision limits are part of each model's capabilities.

**Fix:** at startup the provider validates `llm_model` against `list_models()` (with a clear error naming the available models) and reads `capabilities.limits.vision.max_prompt_images`. Requests with more images are capped per `truncate_keep`, with a log line. Default model: `claude-sonnet-5.5`.

**Avoid next time:** before hardcoding a model from docs or examples, list what the account actually exposes, and read its limits.

---

## 19. The Copilot SDK is an agent runtime: sessions can run tools by default (Phase 12)

**What:** `create_session` accepts tools, permission handlers, MCP servers, and working directories. Out of the box, a session's model can call built-in tools (shell, file edits) on the user's machine.

**Why:** the SDK drives the same runtime as the Copilot CLI agent. We only need a text answer about screenshots.

**Fix:** every session is created with `available_tools=[]`.

**Avoid next time:** when using an agent SDK as a plain LLM call, disable tools explicitly. Don't rely on the prompt to keep it from acting.

---

## 20. Rendering LLM markdown in AppKit: three silent formatting traps (Phase 12)

**What:** the response showed raw `**bold**` and `` `code` `` in an `NSAlert`. After rendering via Markdown → HTML → `NSAttributedString`, three more problems showed up, found only by rendering the real response offscreen and looking at it:
1. Bulleted lists merged into the preceding paragraph ("**The trait** - `type Error;` … - `type Output`…").
2. No spacing after lists and code blocks, so the next heading touched the last bullet.
3. Gray code backgrounds bled into the bullet gutter and, for wrapped inline code, from the left margin.

**Why:**
1. Python-Markdown needs a blank line before a list. CommonMark, and every LLM, writes `**Heading**\n- item`.
2. AppKit's HTML importer ignores `margin` on `<ul>`, `<ol>` and `<pre>`.
3. List markers (`\t•\t`) inherit the first child's (monospaced) font, and AppKit fills a wrapped span's background from the line fragment's start, which is the margin on indented paragraphs.

**Fix:** `md_render.normalize()` adds the blank line (outside fenced code). Spacer paragraphs go after `</ul>`, `</ol>` and `</pre>`. Backgrounds are removed from markers, and after layout from inline code that wraps inside indented paragraphs. Text colors are replaced with `labelColor` for Dark Mode.

**Avoid next time:** render real LLM output (lists right after bold lines, wrapped inline code, fenced blocks) in both appearances and look at it; offscreen `cacheDisplayInRect` works without screen capture (`.tmp/scripts/render_response_preview.py`).
