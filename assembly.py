"""Phase 11: contexts, annotated overviews, transcript markers, and request assembly (one request per release)."""
import json
import threading
import time
from pathlib import Path

import CoreText
import objc
import Quartz
from AppKit import NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName
from Foundation import NSURL, NSAttributedString
from PyObjCTools import AppHelper

from config import CONFIG
from log import log, log_exc
from screen_capture import changed_pct, write_jpeg

# --- transcript ------------------------------------------------------------------


def filter_words(words, levels, cfg=CONFIG):
    """Split words into (kept, dropped): a word is dropped when the audio around it is near-silent.

    levels: [(t_end_of_chunk, rms)] on the session clock (100 ms chunks). Whistle can hallucinate
    words like "you" / "Thank you" on silence with probability ~1.0, so confidence can't be used.
    """
    if not levels:
        return list(words), []
    kept, dropped = [], []
    chunk_s = cfg.chunk_size / cfg.sample_rate
    for w in words:
        lo, hi = w["start"] - cfg.speech_rms_margin_s, w["end"] + cfg.speech_rms_margin_s
        around = [r for t, r in levels if t >= lo and t - chunk_s <= hi]
        loud = max(around, default=0.0)
        (kept if loud >= cfg.speech_min_rms else dropped).append(dict(w, rms=round(loud, 4)))
    return kept, dropped


def insert_markers(words, gestures):
    """Transcript text with [n] after the word whose midpoint is nearest each gesture's start."""
    if not words:
        return ""
    after: dict[int, list[int]] = {}
    for g in gestures:
        k = min(range(len(words)), key=lambda i: abs((words[i]["start"] + words[i]["end"]) / 2 - g.t_start))
        after.setdefault(k, []).append(g.index)
    out = []
    for i, w in enumerate(words):
        out.append(w["word"])
        out += [f"[{n}]" for n in sorted(after.get(i, []))]
    return " ".join(out)


# --- contexts ----------------------------------------------------------------------


def segment_frames(frames, t0, t1):
    """Kept frame records seen during [t0, t1]: captured then, or merged-into by a request then."""
    out = []
    for r in frames:
        if t0 <= r["t"] <= t1 or any(t0 <= m["t"] <= t1 for m in r["merged"]):
            out.append(r)
    return out


def build_contexts(records, gestures, cfg=CONFIG):
    """Greedy grouping by changed area (context_changed_pct), in time order. Returns a list of dicts."""
    contexts = []
    for r in sorted(records, key=lambda r: r["t"]):
        best, best_pct = None, 100.0
        for c in contexts:
            pct = changed_pct(r["thumb"], c["records"][0]["thumb"], cfg.dedup_pixel_delta)
            if pct < best_pct:
                best, best_pct = c, pct
        if best is not None and best_pct < cfg.context_changed_pct:
            best["records"].append(r)
        else:
            contexts.append({"records": [r]})
    by_index = {g.index: g for g in gestures}
    for c in contexts:
        idx = sorted({i for r in c["records"] for i in r["gestures"] if i in by_index})
        c["gestures"] = [by_index[i] for i in idx]
        if c["gestures"]:
            first = c["gestures"][0].index
            starts = [r for r in c["records"] if first in r["gestures"] and "gesture_start" in r["triggers"]]
            c["frame"] = (starts or [r for r in c["records"] if first in r["gestures"]])[0]
        else:
            c["frame"] = max(c["records"], key=lambda r: max([r["t"]] + [m["t"] for m in r["merged"]]))
        c["first_t"] = min(r["t"] for r in c["records"])
    contexts.sort(key=lambda c: c["first_t"])
    return contexts


# --- annotation ----------------------------------------------------------------------


def _load(path: Path):
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None) if src else None


def annotate(frame_path: Path, gestures, screen_frame, out_path: Path, cfg=CONFIG) -> bool:
    """Draw a numbered box per gesture on the frame and write it as JPEG."""
    with objc.autorelease_pool():
        img = _load(frame_path)
        if img is None:
            return False
        w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
        s = w / screen_frame.size.width
        ox, oy = screen_frame.origin.x, screen_frame.origin.y
        ctx = Quartz.CGBitmapContextCreate(
            None, w, h, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
            Quartz.kCGImageAlphaNoneSkipFirst | Quartz.kCGBitmapByteOrder32Little,
        )
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)
        r, g_, b, a = cfg.annotation_rgba
        pad = cfg.annotation_pad_pt
        for g in gestures:
            bx, by, bw, bh = g.bbox
            x0, y0 = (bx - pad - ox) * s, (by - pad - oy) * s  # global Cocoa -> bitmap pixels (both y-up)
            rw, rh = (bw + 2 * pad) * s, (bh + 2 * pad) * s
            rect = Quartz.CGRectMake(x0, y0, rw, rh)
            # Halo first (wider, contrasting) so the box stays visible on any background color.
            Quartz.CGContextSetRGBStrokeColor(ctx, *cfg.annotation_halo_rgba)
            Quartz.CGContextSetLineWidth(ctx, (cfg.annotation_line_pt + 2 * cfg.annotation_halo_pt) * s)
            Quartz.CGContextStrokeRect(ctx, rect)
            Quartz.CGContextSetRGBStrokeColor(ctx, r, g_, b, a)
            Quartz.CGContextSetLineWidth(ctx, cfg.annotation_line_pt * s)
            Quartz.CGContextStrokeRect(ctx, rect)
            # Badge "[n]" just above the box's top-left corner (inside the image).
            size = cfg.annotation_label_pt * s
            attrs = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(size),
                     NSForegroundColorAttributeName: NSColor.whiteColor()}
            line = CoreText.CTLineCreateWithAttributedString(
                NSAttributedString.alloc().initWithString_attributes_(f"[{g.index}]", attrs))
            tw = CoreText.CTLineGetTypographicBounds(line, None, None, None)[0]
            bp = size * 0.25
            lx = min(max(0.0, x0), w - tw - 2 * bp)
            ly = y0 + rh if y0 + rh + size + bp < h else max(0.0, y0 - size - bp)
            hp = cfg.annotation_halo_pt * s
            Quartz.CGContextSetRGBFillColor(ctx, *cfg.annotation_halo_rgba)
            Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(lx - hp, ly - hp, tw + 2 * bp + 2 * hp, size + bp + 2 * hp))
            Quartz.CGContextSetRGBFillColor(ctx, r, g_, b, a)
            Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(lx, ly, tw + 2 * bp, size + bp))
            Quartz.CGContextSetTextPosition(ctx, lx + bp, ly + bp * 0.9)
            CoreText.CTLineDraw(line, ctx)
        return write_jpeg(Quartz.CGBitmapContextCreateImage(ctx), out_path, cfg.jpeg_quality)


# --- assembly --------------------------------------------------------------------------


class Assembler(threading.Thread):
    """Builds the request for one released segment. The final transcript is delivered via deliver()."""

    def __init__(self, controller, session, segment: dict, cfg=CONFIG):
        super().__init__(name="assembler", daemon=True)
        self.controller = controller
        self.session = session
        self.seg = segment
        self.cfg = cfg
        self._final = threading.Event()
        self.text = ""
        self.words = None

    def deliver(self, text: str, words) -> None:
        """Final transcript for this segment (any thread)."""
        self.text, self.words = text, words
        self._final.set()

    def _status(self, text: str) -> None:
        def show():
            if self.controller.session is None:  # never clobber a newer session's caption
                self.controller.caption.show(self.session.visible_frame, text)
        AppHelper.callAfter(show)

    def run(self) -> None:
        try:
            self._run()
        except Exception:
            log_exc("assembly", f"request assembly failed for {self.session.id}")

    def _run(self) -> None:
        cfg, s, seg = self.cfg, self.session, self.seg
        t_start = time.monotonic()
        n = seg["segment"]
        suffix = "" if n == 1 else f"_{n}"

        # 1) Image job, immediately.
        gestures = [g for g in s.gestures if g.index >= seg["first_gesture"]]
        records = segment_frames(s.frames, seg["t0"], seg["t1"])
        contexts = build_contexts(records, gestures, cfg)

        # 2) Final transcript (may still be streaming or waiting for the model).
        speech = self.controller.speech
        loading = speech is not None and not speech.ready.is_set()
        timeout = cfg.model_wait_timeout_s if loading else cfg.assembly_timeout_s
        if not self._final.wait(cfg.assembly_status_after_s):
            self._status("Waiting for speech model…" if loading else "Transcribing…")
            if not self._final.wait(max(0.0, timeout - cfg.assembly_status_after_s)):
                self.text = self.controller.partial_text.get(s.id, "")
                log("assembly", f"WARNING final transcript not ready after {timeout:.0f}s; "
                                f"using committed text so far: {self.text!r}")

        words = [w for w in (self.words or []) if w["start"] >= seg["t0"] - 0.5]
        if self.words is not None:
            kept, dropped = filter_words(words, s.audio_levels, cfg)
            if dropped:
                log("assembly", f"dropped {len(dropped)} word(s) over near-silent audio: "
                                f"{[(w['word'], w['rms']) for w in dropped]}")
            text = " ".join(w["word"] for w in kept)
        else:  # fake processor or timeout: no word timings
            kept, dropped, text = [], [], self.text.strip()

        status = "ok"
        if not text:
            status = "no_speech" if gestures else "empty"

        # 3) Annotated overviews (one per context), capped.
        truncated = 0
        if cfg.max_images is not None and len(contexts) > cfg.max_images:
            truncated = len(contexts) - cfg.max_images
            contexts = contexts[-cfg.max_images:] if cfg.truncate_keep == "end" else contexts[: cfg.max_images]
        images = []
        if status == "ok":
            for k, c in enumerate(contexts, 1):
                out = s.folder / f"context{suffix}_{k}.jpg"
                if annotate(c["frame"]["path"], c["gestures"], s.screen_frame, out, cfg):
                    c["image"] = out
                    images.append(out)

        if kept:
            marked = insert_markers(kept, gestures)
        else:
            marked = text + ("" if not gestures or not text else
                             " " + " ".join(f"[{g.index}]" for g in gestures))  # no timings: list markers at the end
        request = {
            "session": s.id,
            "segment": n,
            "status": status,
            "transcript": text,
            "transcript_with_markers": marked,
            "words": kept,
            "dropped_words": dropped,
            "gestures": [dict(g.to_dict(), context=next((k for k, c in enumerate(contexts, 1) if g in c["gestures"]), None))
                         for g in gestures],
            "contexts": [{"id": k, "image": c.get("image").name if c.get("image") else None,
                          "frame": c["frame"]["file"], "frames": [r["file"] for r in c["records"]],
                          "app": c["frame"]["app"], "gestures": [g.index for g in c["gestures"]]}
                         for k, c in enumerate(contexts, 1)],
            "images": [str(p) for p in images],
            "truncated_contexts": truncated,
            "assembly_s": round(time.monotonic() - t_start, 3),
        }
        (s.folder / f"request{suffix}.json").write_text(json.dumps(request, indent=1))
        log("assembly", f"{s.id} segment {n}: {status}, {len(images)} image(s), {len(gestures)} gesture(s), "
                        f"{request['assembly_s']:.2f}s -> request{suffix}.json: {marked!r}")
        AppHelper.callAfter(self.controller.on_request, request)
