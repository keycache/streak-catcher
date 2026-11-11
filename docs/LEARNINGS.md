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
