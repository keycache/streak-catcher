"""Markdown -> NSAttributedString for LLM responses (bold, italics, inline/fenced code, lists, headings).

HTML import must run on the main thread (AppKit's HTML importer uses WebKit).
"""
import re

import markdown
from AppKit import (
    NSAttributedString,
    NSBackgroundColorAttributeName,
    NSCharacterEncodingDocumentOption,
    NSColor,
    NSDocumentTypeDocumentOption,
    NSFontAttributeName,
    NSFontDescriptorTraitMonoSpace,
    NSForegroundColorAttributeName,
    NSHTMLTextDocumentType,
    NSParagraphStyleAttributeName,
    NSUTF8StringEncoding,
)
from AppKit import NSScrollView, NSTextView
from Foundation import NSData, NSMakeRange, NSMakeRect

from config import CONFIG

_CSS = """
body {{ font-family: -apple-system, 'Helvetica Neue'; font-size: {size}px; line-height: 1.35; }}
p, ul, ol, pre, h1, h2, h3, h4 {{ margin: 0 0 {gap}px 0; }}
li {{ margin: 0 0 {li_gap}px 0; }}
h1 {{ font-size: {h1}px; }} h2 {{ font-size: {h2}px; }} h3, h4 {{ font-size: {h3}px; }}
code {{ font-family: Menlo, monospace; font-size: {code}px; }}
pre {{ font-family: Menlo, monospace; font-size: {code}px; }}
"""


_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")


def normalize(md: str) -> str:
    """Insert the blank line Python-Markdown needs before a list that directly follows text.

    LLMs (and CommonMark) write "**Heading**\n- item"; Python-Markdown would merge it into the paragraph.
    Fenced code blocks are left untouched.
    """
    out, in_fence, prev = [], False, ""
    for line in (md or "").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and _LIST_ITEM.match(line) and prev.strip() and not _LIST_ITEM.match(prev):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def to_html(md: str, cfg=CONFIG) -> str:
    body = markdown.markdown(normalize(md), extensions=["fenced_code", "sane_lists", "tables"])
    size = cfg.response_font_pt
    # AppKit's HTML importer ignores margins on lists; a small spacer paragraph separates a list from what follows.
    spacer = f"<p style='margin:0; font-size:{round(size * 0.5)}px; line-height:{round(size * 0.5)}px'>&nbsp;</p>"
    body = body.replace("</ul>", "</ul>" + spacer).replace("</ol>", "</ol>" + spacer).replace("</pre>", "</pre>" + spacer)
    css = _CSS.format(size=size, gap=round(size * 0.6), li_gap=round(size * 0.25), code=round(size * 0.92),
                      h1=round(size * 1.35), h2=round(size * 1.2), h3=round(size * 1.08))
    return f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>{body}</body></html>"


def render_plain(text: str, cfg=CONFIG):
    """Unformatted text in the system font with the dynamic label color."""
    from AppKit import NSFont
    return NSAttributedString.alloc().initWithString_attributes_(
        text or "", {NSFontAttributeName: NSFont.systemFontOfSize_(cfg.response_font_pt),
                     NSForegroundColorAttributeName: NSColor.labelColor()})


def make_view(text: str, width: float, max_height: float, markdown_on: bool = True, cfg=CONFIG, code_bg=None):
    """Selectable text view in a scroll view, sized to its content up to max_height. Returns (scroll, content_h)."""
    tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 10))
    tv.setEditable_(False)
    tv.setSelectable_(True)
    tv.setDrawsBackground_(False)
    tv.setTextContainerInset_((2, 4))
    tv.textStorage().setAttributedString_(render(text, cfg, code_bg) if markdown_on else render_plain(text, cfg))
    tv.setVerticallyResizable_(True)
    tv.setHorizontallyResizable_(False)
    tv.textContainer().setWidthTracksTextView_(True)
    if markdown_on:
        drop_wrapped_code_backgrounds(tv)
    lm, tc = tv.layoutManager(), tv.textContainer()
    lm.ensureLayoutForTextContainer_(tc)
    content_h = lm.usedRectForTextContainer_(tc).size.height + 8
    height = min(max(content_h, 24), max_height)
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setHasVerticalScroller_(content_h > max_height)
    scroll.setAutohidesScrollers_(True)
    scroll.setDrawsBackground_(False)
    scroll.setBorderType_(0)
    tv.setFrame_(NSMakeRect(0, 0, width, max(content_h, height)))
    scroll.setDocumentView_(tv)
    return scroll, content_h


def render(md: str, cfg=CONFIG, code_bg=None):
    """Attributed string with dynamic (light/dark) text colors and a subtle background behind code."""
    raw = to_html(md, cfg).encode("utf-8")
    data = NSData.dataWithBytes_length_(raw, len(raw))
    opts = {NSDocumentTypeDocumentOption: NSHTMLTextDocumentType, NSCharacterEncodingDocumentOption: NSUTF8StringEncoding}
    result = NSAttributedString.alloc().initWithHTML_options_documentAttributes_(data, opts, None)
    attr = result[0] if isinstance(result, tuple) else result
    if attr is None:  # fall back to plain text
        return NSAttributedString.alloc().initWithString_(md or "")
    out = attr.mutableCopy()
    # Trim trailing newlines the importer adds after the last paragraph.
    text = str(out.string())
    end = len(text.rstrip("\n"))
    if end < len(text):
        out.deleteCharactersInRange_(NSMakeRange(end, len(text) - end))
    full = NSMakeRange(0, out.length())
    # Imported HTML hard-codes black; use the system label color so Dark Mode works.
    out.addAttribute_value_range_(NSForegroundColorAttributeName, NSColor.labelColor(), full)
    if code_bg is None:
        code_bg = NSColor.quaternarySystemFillColor() if hasattr(NSColor, "quaternarySystemFillColor") else NSColor.quaternaryLabelColor()

    def mark_code(font, rng, _stop):
        if font is not None and font.fontDescriptor().symbolicTraits() & NSFontDescriptorTraitMonoSpace:
            out.addAttribute_value_range_(NSBackgroundColorAttributeName, code_bg, rng)

    out.enumerateAttribute_inRange_options_usingBlock_(NSFontAttributeName, full, 0, mark_code)
    # List markers ("\t•\t") inherit the first child's font; keep the code background off them.
    text = str(out.string())
    for i, ch in enumerate(text):
        if ch in "\t•◦▪" or (ch.isdigit() and i + 1 < len(text) and text[i + 1] == "." and (i == 0 or text[i - 1] == "\t")):
            out.removeAttribute_range_(NSBackgroundColorAttributeName, NSMakeRange(i, 1))
    return out


def drop_wrapped_code_backgrounds(text_view) -> None:
    """After layout: remove the code background from spans that wrap across lines.

    AppKit fills a wrapped span's background from the line fragment's start, so on indented paragraphs
    (list items) the gray block bleeds into the margin. Wrapped code keeps its monospaced font.
    """
    storage, lm, tc = text_view.textStorage(), text_view.layoutManager(), text_view.textContainer()
    lm.ensureLayoutForTextContainer_(tc)
    wrapped = []

    def check(value, rng, _stop):
        if value is None:
            return
        para = storage.attribute_atIndex_effectiveRange_(NSParagraphStyleAttributeName, rng.location, None)[0]
        if para is None or para.headIndent() <= 0:
            return  # only indented paragraphs (list items) bleed; code blocks at the margin keep their background
        glyphs = lm.glyphRangeForCharacterRange_actualCharacterRange_(rng, None)[0]
        first = lm.lineFragmentRectForGlyphAtIndex_effectiveRange_(glyphs.location, None)[0]
        last = lm.lineFragmentRectForGlyphAtIndex_effectiveRange_(glyphs.location + max(0, glyphs.length - 1), None)[0]
        if abs(first.origin.y - last.origin.y) > 0.5:
            wrapped.append(rng)

    storage.enumerateAttribute_inRange_options_usingBlock_(
        NSBackgroundColorAttributeName, NSMakeRange(0, storage.length()), 0, check)
    for rng in wrapped:
        storage.removeAttribute_range_(NSBackgroundColorAttributeName, rng)

