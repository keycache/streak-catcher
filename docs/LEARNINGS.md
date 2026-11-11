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
