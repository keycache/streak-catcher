"""Debug images: each detected gesture drawn on the captured frame nearest its start."""
import threading
from pathlib import Path

import CoreText
import objc
import Quartz
from AppKit import NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName
from Foundation import NSAttributedString, NSURL

from log import log, log_exc
from screen_capture import write_jpeg

BOX_RGBA = (1.0, 0.15, 0.55, 1.0)  # magenta box
PATH_RGBA = (1.0, 0.85, 0.0, 0.9)  # yellow gesture path
LABEL_BG_RGBA = (1.0, 0.15, 0.55, 0.9)


def _load(path: Path):
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None) if src else None


def _draw_label(ctx, text: str, x: float, y: float, size: float) -> None:
    attrs = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(size),
             NSForegroundColorAttributeName: NSColor.whiteColor()}
    line = CoreText.CTLineCreateWithAttributedString(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
    w = CoreText.CTLineGetTypographicBounds(line, None, None, None)[0]
    pad = size * 0.3
    Quartz.CGContextSetRGBFillColor(ctx, *LABEL_BG_RGBA)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(x, y, w + 2 * pad, size + pad))
    Quartz.CGContextSetTextPosition(ctx, x + pad, y + pad * 0.8)
    CoreText.CTLineDraw(line, ctx)


def render(session, frames, out_dir: Path, quality: float) -> list[Path]:
    """Write debug_gesture_N.jpg for every gesture in `session`. Returns the written paths.

    frames: [(t_offset_s, path, display_id)] captured during the session.
    """
    written = []
    fx, fy = session.screen_frame.origin.x, session.screen_frame.origin.y
    fw = session.screen_frame.size.width
    for g in session.gestures:
        same_display = [f for f in frames if f[2] == session.display_id] or frames
        if not same_display:
            log("gesture", f"debug image #{g.index}: no captured frames (screen capture disabled?)")
            continue
        t_frame, frame_path, _ = min(same_display, key=lambda f: abs(f[0] - g.t_start))
        with objc.autorelease_pool():
            img = _load(frame_path)
            if img is None:
                continue
            w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
            s = w / fw  # image pixels per point
            ctx = Quartz.CGBitmapContextCreate(
                None, w, h, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
                Quartz.kCGImageAlphaNoneSkipFirst | Quartz.kCGBitmapByteOrder32Little,
            )
            Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)

            def px(x, y):  # global Cocoa point -> bitmap pixel (both y-up)
                return (x - fx) * s, (y - fy) * s

            pts = [p for p in session.path if g.t_start <= p[0] <= g.t_end]
            if len(pts) > 1:
                Quartz.CGContextSetRGBStrokeColor(ctx, *PATH_RGBA)
                Quartz.CGContextSetLineWidth(ctx, 2.5 * s)
                Quartz.CGContextSetLineJoin(ctx, Quartz.kCGLineJoinRound)
                Quartz.CGContextMoveToPoint(ctx, *px(pts[0][1], pts[0][2]))
                for _, x, y in pts[1:]:
                    Quartz.CGContextAddLineToPoint(ctx, *px(x, y))
                Quartz.CGContextStrokePath(ctx)

            bx, by, bw, bh = g.bbox
            x0, y0 = px(bx, by)
            Quartz.CGContextSetRGBStrokeColor(ctx, *BOX_RGBA)
            Quartz.CGContextSetLineWidth(ctx, 3 * s)
            Quartz.CGContextStrokeRect(ctx, Quartz.CGRectMake(x0, y0, bw * s, bh * s))
            label = f"#{g.index} {g.kind}  {g.t_start:.2f}-{g.t_end:.2f}s  (frame @ {t_frame:.2f}s)"
            size = 14 * s
            ly = y0 + bh * s + 2 * s if y0 + bh * s + size * 1.4 < h else max(0, y0 - size * 1.4)
            _draw_label(ctx, label, max(0, min(x0, w - size * len(label) * 0.55)), ly, size)

            out = out_dir / f"debug_gesture_{g.index}.jpg"
            if write_jpeg(Quartz.CGBitmapContextCreateImage(ctx), out, quality):
                written.append(out)
                inside = 0 <= x0 <= w and 0 <= y0 <= h
                log("gesture", f"debug image #{g.index} -> {out.name} (frame {frame_path.name} @ {t_frame:.2f}s"
                               + ("" if inside else ", box is off this display") + ")")
    return written


def render_async(session, frames, quality: float) -> None:
    """Render on a background thread so the main thread never waits on image work."""
    gestures = list(session.gestures)
    if not gestures:
        return

    def run():
        try:
            render(session, list(frames), session.folder, quality)
        except Exception:
            log_exc("gesture", "debug image rendering failed")

    threading.Thread(target=run, name="gesture-debug", daemon=True).start()
