import objc
from AppKit import (
    NSAlert,
    NSAlertSecondButtonReturn,
    NSPasteboard,
    NSPasteboardTypeString,
    NSScrollView,
    NSTextView,
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSWorkspace,
)
from Foundation import NSURL, NSMakeRect, NSObject
from PyObjCTools import AppHelper

import md_render
import permissions
from config import CONFIG
from controller import SessionController
from hotkey import HotkeyListener
from log import log, log_exc

ICON_IDLE = "sparkles"
ICON_ACTIVE = "record.circle.fill"


class AppDelegate(NSObject):
    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.hotkey_enabled = True
        self.last_transcript = None
        self.on_hotkey_toggled = None  # set by later phases: fn(enabled: bool)
        self.shutdown_hooks = []  # callables run once on quit
        self._shut_down = False
        return self

    # --- lifecycle -------------------------------------------------------

    def applicationDidFinishLaunching_(self, _notification):
        self._build_status_item()
        permissions.report()

        self.hotkey = HotkeyListener(CONFIG.combo_vks, self._on_activate, self._on_deactivate)
        self.controller = SessionController(self, self.hotkey)
        self.on_hotkey_toggled = self._on_hotkey_toggled
        self.shutdown_hooks += [self.controller.shutdown, self.hotkey.stop]
        self.hotkey.start()

        log("app", f"ready (captures: {CONFIG.captures_dir})")

    def _on_activate(self):
        try:
            self.controller.start()
        except Exception:
            log_exc("app", "session start failed")

    def _on_deactivate(self):
        try:
            self.controller.stop("released")
        except Exception:
            log_exc("app", "session stop failed")

    def _on_hotkey_toggled(self, enabled: bool):
        self.hotkey.enabled = enabled
        if not enabled:
            self.hotkey.active = False
            self.controller.stop("disabled")

    def applicationWillTerminate_(self, _notification):
        self.shutdown()

    def shutdown(self):
        if self._shut_down:
            return
        self._shut_down = True
        log("app", "shutting down")
        for hook in self.shutdown_hooks:
            try:
                hook()
            except Exception:
                log_exc("app", f"shutdown hook {hook!r} failed")
        log("app", "bye")

    # --- status item -----------------------------------------------------

    def _build_status_item(self):
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.set_active(False)

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.model_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Speech model: starting…", None, "")
        self.model_item.setEnabled_(False)
        menu.addItem_(self.model_item)
        self.llm_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("LLM: starting…", None, "")
        self.llm_item.setEnabled_(False)
        menu.addItem_(self.llm_item)
        menu.addItem_(NSMenuItem.separatorItem())
        self.hotkey_item = self._add_item(menu, "Hotkey Enabled", "toggleHotkey:")
        self.hotkey_item.setState_(1)
        self._add_item(menu, "Open Captures Folder", "openCaptures:")
        self._add_item(menu, "Show Last Transcript", "showTranscript:")
        self._add_item(menu, "Show Last Response", "showResponse:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add_item(menu, "Quit Streak Catcher", "quit:", "q")
        self.status_item.setMenu_(menu)

    def _add_item(self, menu, title, action, key=""):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        item.setTarget_(self)
        menu.addItem_(item)
        return item

    def set_model_status(self, text: str):
        """Update the disabled status line at the top of the menu. Main thread only."""
        self.model_item.setTitle_(text)

    def set_llm_status(self, text: str):
        self.llm_item.setTitle_(text)

    def set_active(self, active: bool):
        """Switch the menu bar icon between idle and recording. Main thread only."""
        button = self.status_item.button()
        name = ICON_ACTIVE if active else ICON_IDLE
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "Streak Catcher")
        if image is None:
            button.setImage_(None)
            button.setTitle_("●" if active else "✦")
            return
        image.setTemplate_(not active)
        button.setImage_(image)
        button.setTitle_("")

    # --- menu actions ----------------------------------------------------

    def toggleHotkey_(self, _sender):
        self.hotkey_enabled = not self.hotkey_enabled
        self.hotkey_item.setState_(1 if self.hotkey_enabled else 0)
        log("app", f"hotkey {'enabled' if self.hotkey_enabled else 'disabled'}")
        if self.on_hotkey_toggled:
            self.on_hotkey_toggled(self.hotkey_enabled)

    def openCaptures_(self, _sender):
        CONFIG.captures_dir.mkdir(parents=True, exist_ok=True)
        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(CONFIG.captures_dir)))
        log("app", f"opened {CONFIG.captures_dir}")

    def showTranscript_(self, _sender):
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Last Transcript")
        alert.setInformativeText_(self.last_transcript or "(no transcript yet)")
        alert.runModal()

    def showResponse_(self, _sender):
        r = getattr(getattr(self, "controller", None), "last_response", None)
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Last Response")
        if not r:
            alert.setInformativeText_("(no response yet)")
            alert.runModal()
            return
        alert.setInformativeText_(f"{r['provider']} / {r['model']} · {r['seconds']}s")
        body = f"**Error:** {r['error']}" if r["error"] else (r["text"] or "(empty response)")
        alert.setAccessoryView_(response_view(body))
        alert.addButtonWithTitle_("OK")
        alert.addButtonWithTitle_("Copy")
        if alert.runModal() == NSAlertSecondButtonReturn:
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(r["text"] or r["error"] or "", NSPasteboardTypeString)
            log("app", "response copied to clipboard")

    def quit_(self, _sender):
        NSApp.terminate_(None)


def response_view(markdown_text: str):
    """Scrollable, selectable text view with the rendered markdown, sized to its content."""
    width, max_h = CONFIG.response_view_width_pt, CONFIG.response_view_max_height_pt
    text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 10))
    text_view.setEditable_(False)
    text_view.setSelectable_(True)
    text_view.setDrawsBackground_(False)
    text_view.setTextContainerInset_((2, 4))
    text_view.textStorage().setAttributedString_(md_render.render(markdown_text))
    text_view.setVerticallyResizable_(True)
    text_view.setHorizontallyResizable_(False)
    text_view.textContainer().setWidthTracksTextView_(True)
    md_render.drop_wrapped_code_backgrounds(text_view)
    lm = text_view.layoutManager()
    lm.ensureLayoutForTextContainer_(text_view.textContainer())
    content_h = lm.usedRectForTextContainer_(text_view.textContainer()).size.height + 8
    height = min(max(content_h, 40), max_h)
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setHasVerticalScroller_(content_h > max_h)
    scroll.setDrawsBackground_(False)
    scroll.setBorderType_(0)
    text_view.setFrame_(NSMakeRect(0, 0, width, max(content_h, height)))
    scroll.setDocumentView_(text_view)
    return scroll


def run() -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    # runEventLoop skips this when NSApp already exists (as it does here).
    AppHelper.installMachInterrupt()
    try:
        AppHelper.runEventLoop()
    finally:
        # Ctrl+C may stop the loop without applicationWillTerminate_.
        delegate.shutdown()
