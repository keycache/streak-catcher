from AppKit import (
    NSAppearance,
    NSAppearanceNameVibrantDark,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSFontWeightMedium,
    NSLineBreakByTruncatingHead,
    NSPanel,
    NSStatusWindowLevel,
    NSTextAlignmentCenter,
    NSTextField,
    NSViewHeightSizable,
    NSViewWidthSizable,
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
from Foundation import NSMakeRect
from PyObjCTools import AppHelper

from log import log

MAX_WIDTH = 900.0
WIDTH_FRACTION = 0.7
HEIGHT = 56.0
BOTTOM_MARGIN = 60.0
CORNER_RADIUS = 14.0
FONT_SIZE = 20.0
H_PADDING = 18.0


class CaptionPanel:
    """Floating transcript bar at the bottom of a screen. Main thread only."""

    def __init__(self):
        self.panel = None
        self.label = None
        self._hide_token = 0

    def _build(self) -> None:
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, MAX_WIDTH, HEIGHT),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        panel.setSharingType_(NSWindowSharingNone)  # keep it out of screenshots
        # HUD material follows the system appearance; force dark so white text stays readable in Light Mode.
        panel.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameVibrantDark))

        effect = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, MAX_WIDTH, HEIGHT))
        effect.setMaterial_(NSVisualEffectMaterialHUDWindow)
        effect.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        effect.setState_(NSVisualEffectStateActive)
        effect.setWantsLayer_(True)
        effect.layer().setCornerRadius_(CORNER_RADIUS)
        effect.layer().setMasksToBounds_(True)
        effect.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        font = NSFont.systemFontOfSize_weight_(FONT_SIZE, NSFontWeightMedium)
        line_h = font.ascender() - font.descender() + font.leading() + 2
        label = NSTextField.labelWithString_("")
        label.setFont_(font)
        label.setTextColor_(NSColor.whiteColor())
        label.setAlignment_(NSTextAlignmentCenter)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setLineBreakMode_(NSLineBreakByTruncatingHead)
        label.cell().setTruncatesLastVisibleLine_(True)
        label.setFrame_(NSMakeRect(H_PADDING, (HEIGHT - line_h) / 2, MAX_WIDTH - 2 * H_PADDING, line_h))
        label.setAutoresizingMask_(NSViewWidthSizable)
        effect.addSubview_(label)

        panel.setContentView_(effect)
        self.panel, self.label = panel, label

    def show(self, visible_frame, text: str) -> None:
        """Show on the screen whose visibleFrame is given. Cancels any pending hide."""
        if self.panel is None:
            self._build()
        self._hide_token += 1
        vf = visible_frame
        width = min(MAX_WIDTH, WIDTH_FRACTION * vf.size.width)
        x = vf.origin.x + (vf.size.width - width) / 2
        y = vf.origin.y + BOTTOM_MARGIN
        self.panel.setFrame_display_(NSMakeRect(x, y, width, HEIGHT), True)
        self.label.setStringValue_(text)
        self.panel.orderFrontRegardless()
        log("caption", f"show at ({x:.0f},{y:.0f}) {width:.0f}x{HEIGHT:.0f}: {text!r}", debug=True)

    def set_text(self, text: str) -> None:
        if self.label is not None:
            self.label.setStringValue_(text)

    def is_visible(self) -> bool:
        return self.panel is not None and bool(self.panel.isVisible())

    def hide_after(self, delay_s: float) -> None:
        self._hide_token += 1
        token = self._hide_token
        AppHelper.callLater(delay_s, self._hide_if_current, token)

    def _hide_if_current(self, token: int) -> None:
        if token == self._hide_token and self.panel is not None:
            self.panel.orderOut_(None)
            log("caption", "hidden", debug=True)

    def close(self) -> None:
        self._hide_token += 1
        if self.panel is not None:
            self.panel.orderOut_(None)
