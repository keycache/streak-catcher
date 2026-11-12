"""Response panel (Phase 13): the LLM answer in a dark HUD panel that doesn't take focus. Main thread only."""
import objc
from AppKit import (
    NSAppearance,
    NSAppearanceNameVibrantDark,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSStatusWindowLevel,
    NSTextField,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSObject
from PyObjCTools import AppHelper

import md_render
from config import CONFIG
from log import log

PAD = 14.0
HEADER_H = 22.0
GAP = 8.0
CORNER_RADIUS = 14.0


class ResponsePanelActions(NSObject):
    """Target for the panel's buttons (ObjC class names are process-wide: keep this one unique)."""

    def initWithPanel_(self, owner):
        self = objc.super(ResponsePanelActions, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def copyText_(self, _sender):
        self.owner.copy()

    def closePanel_(self, _sender):
        self.owner.hide("✕")


class ResponsePanel:
    def __init__(self, config=CONFIG):
        self.cfg = config
        self.panel = None
        self.text = ""
        self._token = 0
        self._actions = ResponsePanelActions.alloc().initWithPanel_(self)

    # --- public API ------------------------------------------------------------------

    def show_thinking(self, visible_frame, model: str) -> None:
        self.text = ""
        self._layout(visible_frame, f"Asking {model}…", "_Thinking…_", copy_enabled=False)

    def show_response(self, visible_frame, record: dict) -> None:
        self.text = record.get("text") or ""
        if record.get("error"):
            body, header = f"**Error:** {record['error']}", f"{record['provider']} · error after {record['seconds']}s"
        else:
            body, header = self.text or "_(empty response)_", f"{record['model'] or record['provider']} · {record['seconds']}s"
        self._layout(visible_frame, header, body, copy_enabled=bool(self.text))
        if self.cfg.response_auto_dismiss_s:
            self._token += 1
            AppHelper.callLater(self.cfg.response_auto_dismiss_s, self._auto_hide, self._token)

    def hide(self, reason: str = "") -> None:
        self._token += 1
        if self.is_visible():
            self.panel.orderOut_(None)
            log("respons", f"dismissed{f' ({reason})' if reason else ''}")

    def is_visible(self) -> bool:
        return self.panel is not None and bool(self.panel.isVisible())

    def copy(self) -> None:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(self.text, NSPasteboardTypeString)
        self._copy_btn.setTitle_("Copied")
        AppHelper.callLater(1.2, lambda: self._copy_btn.setTitle_("Copy"))
        log("respons", "copied to clipboard")

    # --- internals ---------------------------------------------------------------------

    def _auto_hide(self, token: int) -> None:
        if token == self._token:
            self.hide("auto")

    def _build(self) -> None:
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 600, 200),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)  # clicks/scrolling work without stealing focus
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        panel.setSharingType_(NSWindowSharingNone)  # keep it out of screenshots
        panel.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameVibrantDark))

        effect = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, 200))
        effect.setMaterial_(NSVisualEffectMaterialHUDWindow)
        effect.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        effect.setState_(NSVisualEffectStateActive)
        effect.setWantsLayer_(True)
        effect.layer().setCornerRadius_(CORNER_RADIUS)
        effect.layer().setMasksToBounds_(True)
        panel.setContentView_(effect)

        header = NSTextField.labelWithString_("")
        header.setFont_(NSFont.systemFontOfSize_(11))
        header.setTextColor_(NSColor.secondaryLabelColor())
        effect.addSubview_(header)

        close = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 22, 22))
        close.setBordered_(False)
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_("xmark.circle.fill", "Close")
        if img is not None:
            close.setImage_(img)
        else:
            close.setTitle_("✕")
        close.setContentTintColor_(NSColor.secondaryLabelColor())
        close.setTarget_(self._actions)
        close.setAction_("closePanel:")
        close.setToolTip_("Dismiss (Esc)")
        effect.addSubview_(close)

        copy = NSButton.buttonWithTitle_target_action_("Copy", self._actions, "copyText:")
        copy.setControlSize_(1)  # small
        copy.setFont_(NSFont.systemFontOfSize_(11))
        copy.setToolTip_("Copy the response (markdown)")
        effect.addSubview_(copy)

        self.panel, self._effect, self._header, self._close_btn, self._copy_btn = panel, effect, header, close, copy
        self._scroll = None

    def _layout(self, visible_frame, header: str, body: str, copy_enabled: bool) -> None:
        if self.panel is None:
            self._build()
        cfg, vf = self.cfg, visible_frame
        width = min(cfg.response_max_width_pt, cfg.response_width_fraction * vf.size.width)
        max_h = cfg.response_max_height_fraction * vf.size.height
        text_w = width - 2 * PAD
        max_text_h = max_h - (PAD + HEADER_H + GAP + PAD)

        if self._scroll is not None:
            self._scroll.removeFromSuperview()
        code_bg = NSColor.colorWithCalibratedRed_green_blue_alpha_(*cfg.response_code_bg_rgba)
        scroll, _content_h = md_render.make_view(body, text_w, max_text_h, cfg.response_markdown, cfg, code_bg)
        text_h = scroll.frame().size.height
        height = PAD + text_h + GAP + HEADER_H + PAD
        x = vf.origin.x + (vf.size.width - width) / 2
        y = vf.origin.y + cfg.response_bottom_margin_pt

        self.panel.setFrame_display_(NSMakeRect(x, y, width, height), False)
        self._effect.setFrame_(NSMakeRect(0, 0, width, height))
        scroll.setFrameOrigin_((PAD, PAD))
        self._effect.addSubview_(scroll)
        self._scroll = scroll

        top = height - PAD - HEADER_H
        self._close_btn.setFrameOrigin_((width - PAD - 22, top))
        self._copy_btn.sizeToFit()
        cw = self._copy_btn.frame().size.width
        self._copy_btn.setFrameOrigin_((width - PAD - 22 - 8 - cw, top + (HEADER_H - self._copy_btn.frame().size.height) / 2))
        self._copy_btn.setEnabled_(copy_enabled)
        self._copy_btn.setTitle_("Copy")
        self._header.setStringValue_(header)
        self._header.setFrame_(NSMakeRect(PAD + 4, top + 3, width - 2 * PAD - cw - 40, HEADER_H - 6))

        self.panel.orderFrontRegardless()
        self.panel.display()
        log("respons", f"show {width:.0f}x{height:.0f} at ({x:.0f},{y:.0f}): {header}", debug=True)
