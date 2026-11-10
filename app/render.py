"""Scenes: turn content (text / image / emoji) into 64x8 frames over time."""
from __future__ import annotations
import colorsys
import math
import os
import random
import time
from io import BytesIO
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageSequence

from . import customglyphs

# Available text color effects (surfaced in the UI).
EFFECTS = ["solid", "two_color_words", "rainbow_fade", "rainbow_chars",
           "fade", "blink"]
# How text is sized on the screen (SizedTextScene).
TEXT_SIZES = ["normal", "fit", "stretch", "px"]
# How pictures are sized (ImageScene).
IMAGE_MODES = ["fit", "fill", "stretch", "scroll"]


def _resize(img, size):
    """Crisp nearest-neighbour when enlarging, smooth when shrinking."""
    size = (max(1, int(size[0])), max(1, int(size[1])))
    shrink = size[0] < img.width or size[1] < img.height
    return img.resize(size, Image.LANCZOS if shrink else Image.NEAREST)


def hsv(h, s=1.0, v=1.0):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(_BASE, "fonts")

# Preferred crisp pixel fonts (fetched at deploy), first that loads wins.
_FONT_CANDIDATES = [
    ("spleen-5x8.otb", 8),
    ("spleen-5x8.pcf", 8),
    ("spleen-5x8.bdf", 8),
    ("PixelOperator8.ttf", 8),
]
_EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"


def hex_to_rgb(s: str):
    s = (s or "#000000").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def fit_emoji_png(data: bytes, height: int = 8) -> bytes:
    """Resize an arbitrary image to `height` px tall (preserving aspect and
    transparency) and return PNG bytes. Shared by the upload API and the CLI
    resize tool. Raises on undecodable input."""
    img = Image.open(BytesIO(data))
    # Use the first frame of animated/multi-page inputs.
    try:
        img.seek(0)
    except Exception:
        pass
    img = img.convert("RGBA")
    if img.height != height:
        r = height / img.height
        img = img.resize((max(1, round(img.width * r)), height), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _load_text_font():
    for name, size in _FONT_CANDIDATES:
        p = os.path.join(FONTS_DIR, name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(8)
    except TypeError:
        return ImageFont.load_default()


def _load_emoji_font():
    """NotoColorEmoji is a fixed-strike color bitmap font. It only loads at its
    native size (~109px); we render there and downscale each glyph to 8px."""
    if not os.path.exists(_EMOJI_FONT):
        return None
    for size in (109, 128, 96, 64):
        try:
            return ImageFont.truetype(_EMOJI_FONT, size)
        except Exception:
            continue
    return None


TEXT_FONT = _load_text_font()
EMOJI_FONT = _load_emoji_font()


class FontSpec:
    """A font at one line height: .font (PIL), .h (line height in pixels),
    .y (where to draw so the text sits in the line), .mode ('1' = crisp, no
    anti-aliasing; 'L' = smooth edges)."""

    def __init__(self, font, h, y, mode="1"):
        self.font, self.h, self.y, self.mode = font, h, y, mode


_asc, _desc = TEXT_FONT.getmetrics()
PIXEL = FontSpec(TEXT_FONT, 8, max(0, (8 - (_asc + _desc)) // 2), "1")

_TTF = "/usr/share/fonts/truetype/"
# id -> (label, file). "pixel" is the built-in 5x8 pixel font (scaled in whole steps).
FONTS = {
    "pixel": ("Pixel 5×8", None),
    "sans": ("Sans", _TTF + "dejavu/DejaVuSans.ttf"),
    "sans-bold": ("Sans bold", _TTF + "dejavu/DejaVuSans-Bold.ttf"),
    "narrow": ("Narrow", _TTF + "liberation/LiberationSansNarrow-Regular.ttf"),
    "narrow-bold": ("Narrow bold", _TTF + "liberation/LiberationSansNarrow-Bold.ttf"),
    "serif": ("Serif", _TTF + "dejavu/DejaVuSerif.ttf"),
    "serif-bold": ("Serif bold", _TTF + "dejavu/DejaVuSerif-Bold.ttf"),
    "mono": ("Mono", _TTF + "dejavu/DejaVuSansMono.ttf"),
    "mono-bold": ("Mono bold", _TTF + "dejavu/DejaVuSansMono-Bold.ttf"),
}
FONTS = {k: v for k, v in FONTS.items() if v[1] is None or os.path.exists(v[1])}
_SPEC_CACHE = {}


def font_spec(font_id: str, height: int, smooth: bool = False) -> FontSpec:
    """FontSpec for a vector font whose capitals + descenders fill `height` px."""
    key = (font_id, height, smooth)
    if key in _SPEC_CACHE:
        return _SPEC_CACHE[key]
    path = FONTS.get(font_id, (None, None))[1]
    if not path:
        return PIXEL
    size = max(4, int(height * 1.6))
    f = ImageFont.truetype(path, size)
    while size > 4:
        f = ImageFont.truetype(path, size)
        b = f.getbbox("HÅgjpqy")
        if b[3] - b[1] <= height:
            break
        size -= 1
    b = f.getbbox("HÅgjpqy")
    spec = FontSpec(f, height, -b[1] + max(0, (height - (b[3] - b[1])) // 2),
                    "L" if smooth else "1")
    if len(_SPEC_CACHE) > 200:
        _SPEC_CACHE.clear()
    _SPEC_CACHE[key] = spec
    return spec

# variation selector + skin-tone modifiers that attach to the preceding emoji
_ATTACH = {0xFE0F, 0x200D} | set(range(0x1F3FB, 0x1F400))


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (
        0x1F300 <= o <= 0x1FAFF or 0x1F000 <= o <= 0x1F0FF or
        0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF or
        0x1F1E6 <= o <= 0x1F1FF or o in (0x2764, 0x2122, 0x2139) or
        0x2190 <= o <= 0x21FF or 0x2300 <= o <= 0x23FF
    )


def _emoji_glyph(seq: str, height: int = 8):
    """Render an emoji (possibly a ZWJ/modifier sequence) to an RGB image of
    the given height on black. Returns None if it can't be drawn."""
    if EMOJI_FONT is None:
        return None
    big = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    try:
        d.text((4, 4), seq, font=EMOJI_FONT, embedded_color=True)
    except Exception:
        return None
    bbox = big.getbbox()
    if not bbox:
        return None
    g = big.crop(bbox)
    r = height / g.height
    g = g.resize((max(1, round(g.width * r)), height), Image.LANCZOS)
    out = Image.new("RGB", (g.width, height), (0, 0, 0))
    out.paste(g, (0, 0), g)
    return out


# Map "smart"/typographic punctuation the pixel font lacks to ASCII it has.
_PUNCT = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'", 0x2032: "'",
    0x0060: "'", 0x00B4: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"', 0x2033: '"',
    0x2013: "-", 0x2014: "-", 0x2015: "-", 0x2212: "-",
    0x00A0: " ", 0x2007: " ", 0x202F: " ", 0x2009: " ",
    0x2022: "*",
}


def _normalize(s: str) -> str:
    return s.replace("…", "...").translate(_PUNCT)


def _text_run(s: str, fg, bg, fs=PIXEL):
    """Render a run of plain text in font `fs`, preserving advance/spaces."""
    if fs is PIXEL:
        s = _normalize(s)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    w = max(1, int(round(probe.textlength(s, font=fs.font))))
    img = Image.new("RGB", (w, fs.h), bg)
    d = ImageDraw.Draw(img)
    d.fontmode = fs.mode
    d.text((0, fs.y), s, font=fs.font, fill=fg)
    return img


class Scene:
    """Base scene. render(now) -> PIL RGB Image of size (W, H)."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.start = time.monotonic()

    def render(self, now: float) -> Image.Image:  # pragma: no cover
        raise NotImplementedError


def _segment(text: str):
    """Split into ('text', run), ('emoji', sequence) and ('glyph', token)
    segments. A ('glyph', token) is a known custom bitmap badge written as
    :token: (e.g. :usc:, :ucla:)."""
    segs = []
    i, n = 0, len(text)
    while i < n:
        if _is_emoji(text[i]):
            seq = text[i]; i += 1
            # absorb variation selectors, skin tones and ZWJ-joined emoji
            while i < n and (ord(text[i]) in _ATTACH or
                             (seq.endswith("‍") and _is_emoji(text[i]))):
                seq += text[i]; i += 1
            segs.append(("emoji", seq))
        elif text[i] == ":":
            m = customglyphs.TOKEN_RE.match(text, i)
            if m and customglyphs.has(m.group(1)):
                segs.append(("glyph", m.group(1)))
                i = m.end()
                continue
            # not a known glyph token: treat the ':' as ordinary text
            j = i + 1
            while j < n and text[j] != ":" and not _is_emoji(text[j]):
                j += 1
            segs.append(("text", text[i:j])); i = j
        else:
            j = i
            while j < n and text[j] != ":" and not _is_emoji(text[j]):
                j += 1
            segs.append(("text", text[i:j])); i = j
    return segs


def _has_glyph_token(text: str) -> bool:
    for m in customglyphs.TOKEN_RE.finditer(text):
        if customglyphs.has(m.group(1)):
            return True
    return False


def _run_word_cols_wrap(s: str, width: int, base: int, prev_space: bool, fs=PIXEL):
    """Per-column word index for a plain-text run of pixel width `width`.

    Words are whitespace-separated. The first visible word gets index `base`;
    each later word bumps by one. Whitespace columns keep the preceding word's
    index (or `base` for leading whitespace). `prev_space` says whether the char
    just before this run was whitespace, so a word split across runs isn't
    double-counted. Returns (cols[width], ended_in_space, started_a_word)."""
    if fs is PIXEL:
        s = _normalize(s)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    cols = [base] * width
    widx = base
    started = False
    x_prev = 0
    for k, ch in enumerate(s):
        x_next = int(round(probe.textlength(s[:k + 1], font=fs.font)))
        is_space = ch.isspace()
        if not is_space and prev_space:
            if started:
                widx += 1
            started = True
        for x in range(max(0, x_prev), min(width, x_next)):
            cols[x] = widx
        prev_space = is_space
        x_prev = x_next
    ended_in_space = bool(s) and s[-1].isspace()
    return cols, ended_in_space, started


def _fit_h(img, h):
    """Scale a badge/emoji image to line height h."""
    if img.height == h:
        return img
    return _resize(img, (max(1, round(img.width * h / img.height)), h))


def _render_text_strip(text, bg, emoji=False, want_words=False, fs=PIXEL):
    """Render text (+emoji) as WHITE text on bg, plus an L mask marking text
    pixels (emoji pixels are left out so effects don't recolor them).
    Returns (strip_rgb, mask_L, width). When want_words is True, also returns a
    4th element: a list (len == width) of per-column word indices, used by the
    two-color word-alternation effect. Whitespace-separated words, glyphs and
    emoji each advance the index so colors alternate across them."""
    bgc = hex_to_rgb(bg)
    white = (255, 255, 255)
    # Segment when emoji rendering is on, or whenever the text contains a known
    # custom glyph token (:usc:, :ucla:, ...) so badges work with emoji off too.
    want_seg = (emoji and EMOJI_FONT is not None) or _has_glyph_token(text or "")
    segs = _segment(text) if want_seg else [("text", text or " ")]
    parts = []  # (rgb_img, mask_L, word_cols|None)
    # Running word index, shared across parts so alternation continues over
    # glyph/emoji boundaries. `word_base` is where the next part's words start;
    # `last_was_space` lets a trailing space in one run merge with a leading
    # space in the next without spawning an empty word.
    word_base = 0
    seen_word = False
    last_was_space = True
    for kind, s in segs:
        if kind == "text":
            if not s:
                continue
            run = _text_run(s, white, (0, 0, 0), fs)
            mask = run.convert("L")
            cols = None
            if want_words:
                cols, ended_space, started = _run_word_cols_wrap(
                    s, run.width, word_base, last_was_space, fs)
                if started:
                    seen_word = True
                    word_base = (max(cols) + 1) if cols else word_base
                last_was_space = ended_space
            parts.append((run, mask, cols))
        elif kind == "glyph":
            g = customglyphs.glyph_image(s, bg=bgc)
            if g is not None:
                g = _fit_h(g, fs.h)
            if g is None:
                run = _text_run("?", white, (0, 0, 0), fs)
                cols = [word_base] * run.width if want_words else None
                parts.append((run, run.convert("L"), cols))
            else:
                cols = [word_base] * g.width if want_words else None
                parts.append((g, Image.new("L", g.size, 0), cols))  # no recolor
            if want_words:
                word_base += 1
                seen_word = True
                last_was_space = True
        else:  # emoji segment
            # Only font-render emoji when the toggle is on; otherwise (segmented
            # solely for a glyph token) fall back to plain text.
            g = _emoji_glyph(s, height=fs.h) if (emoji and EMOJI_FONT is not None) else None
            if g is None:
                run = _text_run(s if not (emoji and EMOJI_FONT) else "?",
                                white, (0, 0, 0), fs)
                cols = [word_base] * run.width if want_words else None
                parts.append((run, run.convert("L"), cols))
            else:
                cols = [word_base] * g.width if want_words else None
                parts.append((g, Image.new("L", g.size, 0), cols))  # no recolor
            if want_words:
                word_base += 1
                seen_word = True
                last_was_space = True
    if not parts:
        run = _text_run(" ", white, (0, 0, 0), fs)
        parts = [(run, run.convert("L"), ([0] * run.width if want_words else None))]
    gap = 1
    total = sum(p[0].width for p in parts) + gap * (len(parts) - 1)
    strip = Image.new("RGB", (max(1, total), fs.h), bgc)
    mask = Image.new("L", (max(1, total), fs.h), 0)
    words = [0] * max(1, total) if want_words else None
    x = 0
    for i, (img, m, cols) in enumerate(parts):
        if m.getbbox():          # text run: paste white pixels through the mask
            strip.paste(img, (x, 0), m)
        else:                    # emoji: paste the color glyph as-is
            strip.paste(img, (x, 0))
        mask.paste(m, (x, 0))
        if want_words and cols is not None:
            for c in range(img.width):
                if x + c < len(words):
                    words[x + c] = cols[c] if c < len(cols) else cols[-1]
        step = img.width + (gap if i < len(parts) - 1 else 0)
        # gap columns keep the color of the part just drawn
        if want_words and gap and i < len(parts) - 1:
            for c in range(img.width, step):
                if x + c < len(words):
                    words[x + c] = cols[-1] if cols else 0
        x += step
    if want_words:
        return strip, mask, strip.width, words
    return strip, mask, strip.width


def _space_width(fs=PIXEL):
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    return max(3, int(round(probe.textlength(" ", font=fs.font))))


class TextScene(Scene):
    def __init__(self, w, h, text, color="#00e5ff", bg="#000000",
                 speed=24, scroll=True, emoji=False, effect="solid",
                 gap_mode="screen", color2="#ff00ff", font=None):
        super().__init__(w, h)
        self.fs = fs = font or PIXEL
        self.base = hex_to_rgb(color)
        self.color2 = hex_to_rgb(color2)
        self.bg = hex_to_rgb(bg)
        self.speed = max(0.25, float(speed))
        self.scroll = scroll
        self.effect = effect if effect in EFFECTS else "solid"
        self.gap_mode = gap_mode
        yy = (h - fs.h) // 2
        want_words = (self.effect == "two_color_words")
        strip = _render_text_strip(text, bg, emoji=emoji, want_words=want_words, fs=fs)
        if want_words:
            self.s0, self.m0, self.tw, self._words = strip
        else:
            self.s0, self.m0, self.tw = strip
            self._words = None
        # "screen": full blank gap so text scrolls off before repeating.
        # "space": just a space between repeats, so the screen never clears.
        gap = _space_width(fs) if gap_mode == "space" else w
        self.period = self.tw + gap
        # Tile enough copies to cover any crop window (offset in [0,period) + w).
        ncopies = max(2, -(-(self.period + w) // self.period))
        loop_w = self.period * ncopies
        self.loop = Image.new("RGB", (loop_w, h), self.bg)
        self.maskloop = Image.new("L", (loop_w, h), 0)
        for k in range(ncopies):
            off = k * self.period
            self.loop.paste(self.s0, (off, yy))
            self.maskloop.paste(self.m0, (off, yy))
        # two_color_words: precompute a tiled color loop the same width as the
        # text loop, so scrolling just crops it. Each column takes color1 or
        # color2 by its word index parity; word indexing restarts per copy so
        # the same word is always the same color as it scrolls.
        self.colorloop = None
        if self.effect == "two_color_words" and self._words is not None:
            self.colorloop = Image.new("RGB", (loop_w, h), self.bg)
            cpx = self.colorloop.load()
            wlen = len(self._words)
            for k in range(ncopies):
                off = k * self.period
                for c in range(self.tw):
                    if c >= wlen:
                        break
                    col = self.base if (self._words[c] % 2 == 0) else self.color2
                    xx = off + c
                    if 0 <= xx < loop_w:
                        for yy2 in range(h):
                            cpx[xx, yy2] = col
        # rainbow_chars: each message copy gets its OWN random start hue, tied to
        # its copy index. Colors are locked to characters (never change while on
        # screen); a new random hue only arrives as the next copy scrolls in.
        self._seed = random.randrange(1 << 30)
        self._hues = {}

    def _copy_hue(self, ci):
        h = self._hues.get(ci)
        if h is None:
            h = random.Random(self._seed + ci * 2654435761).random()
            self._hues[ci] = h
        return h

    def _global_factor(self, t):
        if self.effect == "fade":
            return 0.15 + 0.85 * (0.5 + 0.5 * math.sin(2 * math.pi * t / 2.5))
        if self.effect == "blink":
            return 1.0 if (t % 0.9) < 0.55 else 0.0
        return 1.0

    def render(self, now):
        t = now - self.start
        # Scroll whenever the user asked for it, even if the text fits the sign.
        # (The scrolling branch's tiled loop + gap_mode already handle short text.)
        scrolling = self.scroll
        if scrolling:
            steps = int(t * self.speed)
            off = steps % self.period
            box = (off, 0, off + self.w, self.h)
            win = self.loop.crop(box)
            maskwin = self.maskloop.crop(box)
            abs0 = steps            # absolute scroll position of the left edge
        else:
            x = (self.w - self.tw) // 2 if not self.scroll else 0
            win = Image.new("RGB", (self.w, self.h), self.bg)
            maskwin = Image.new("L", (self.w, self.h), 0)
            win.paste(self.s0, (x, (self.h - self.fs.h) // 2))
            maskwin.paste(self.m0, (x, (self.h - self.fs.h) // 2))
            abs0 = -x

        if self.effect == "two_color_words" and self.colorloop is not None:
            if scrolling:
                layer = self.colorloop.crop((off, 0, off + self.w, self.h))
            else:
                layer = Image.new("RGB", (self.w, self.h), self.bg)
                lpx = layer.load()
                wlen = len(self._words)
                for wx in range(self.w):
                    c = wx - x
                    if 0 <= c < wlen:
                        col = self.base if (self._words[c] % 2 == 0) else self.color2
                        for wy in range(self.h):
                            lpx[wx, wy] = col
        elif self.effect == "rainbow_fade":
            layer = Image.new("RGB", (self.w, self.h), hsv(t * 0.33))
        elif self.effect == "rainbow_chars":
            # color by absolute position: each copy (cycle index) has its own
            # random start hue, so colors ride with the text and only change as
            # a fresh copy scrolls in from the right.
            span = max(self.fs.h, self.tw)
            layer = Image.new("RGB", (self.w, self.h))
            px = layer.load()
            for wx in range(self.w):
                a = abs0 + wx
                ci = a // self.period
                local = a - ci * self.period
                col = hsv(self._copy_hue(ci) + local / span)
                for wy in range(self.h):
                    px[wx, wy] = col
        else:
            layer = Image.new("RGB", (self.w, self.h), self.base)

        out = Image.composite(layer, win, maskwin)
        f = self._global_factor(t)
        if f <= 0.001:
            return Image.new("RGB", (self.w, self.h), self.bg)
        if f < 0.999:
            out = ImageEnhance.Brightness(out).enhance(f)
        return out


ALIGNS = ["left", "center", "right"]


class SizedTextScene(Scene):
    """Text at a chosen font, size and alignment.

    Scrolling: one line (line breaks become spaces) that scrolls across.
    Still: every line drawn on its own, aligned left/center/right, the block
    centered top to bottom.

    size "fit":     as big as fits the sign (all lines, both ways).
    size "stretch": fills the screen (letters change shape).
    size "px":      `height` pixels per line.
    size "normal":  old setting = "px" at 8.
    The pixel font is drawn at 8 px and scaled in whole steps (crisp blocks)
    when it can be; the other fonts are drawn directly at the target height.
    Scroll speed stays in screen pixels per second at every size."""

    LINE_GAP = 1   # blank pixel rows between lines

    def __init__(self, w, h, text, size="normal", speed=24, scroll=True,
                 bg="#000000", font="pixel", height=0, smooth=False,
                 align="center", **kw):
        super().__init__(w, h)
        self.bg = hex_to_rgb(bg)
        self.scroll = bool(scroll)
        self.align = align if align in ALIGNS else "center"
        size = size if size in TEXT_SIZES else "normal"
        if size == "normal":
            size, height = "px", 8
        height = max(4, min(max(4, h), int(height or h)))
        font = font if font in FONTS else "pixel"
        self.lines = None
        text = (text or "").replace("\r", "")
        if self.scroll:
            self._setup_scroll(" ".join(t for t in text.split("\n")), size, height,
                               font, smooth, speed, bg, kw)
        else:
            self._setup_still(text.split("\n"), size, height, font, smooth, bg, kw)

    # -- scrolling: one line --
    def _setup_scroll(self, text, size, height, font, smooth, speed, bg, kw):
        w, h = self.w, self.h
        self.direct = False
        args = dict(bg=bg, scroll=True, **kw)
        if font != "pixel":
            th = h if size in ("fit", "stretch") else height
            self.direct = True
            self.inner = TextScene(w, h, text, speed=speed, font=font_spec(font, max(4, th), smooth), **args)
            return
        if size == "px":
            sx = sy = max(1, height // 8)
        elif size == "stretch":
            sy = h / 8
            sx = max(1, round(sy))
        else:
            sy = sx = max(1, h // 8)
        if sx == 1 and sy == 1:
            self.direct = True
            self.inner = TextScene(w, h, text, speed=speed, **args)
            return
        self.sx, self.sy = sx, sy
        self.inner = TextScene(max(1, math.ceil(w / sx)), 8, text, speed=speed / sx, **args)

    # -- still: lines, aligned --
    def _setup_still(self, lines, size, height, font, smooth, bg, kw):
        w, h, gap = self.w, self.h, self.LINE_GAP
        n = max(1, len(lines))
        emoji = kw.get("emoji", False)
        avail = max(4, (h - (n - 1) * gap) // n)       # line height that fits n lines

        def widths(fs):
            return [max(1, _render_text_strip(t or " ", bg, emoji=emoji, fs=fs)[2]) for t in lines]

        if font != "pixel":
            th = avail if size in ("fit", "stretch") else min(height, h)
            fs = font_spec(font, th, smooth)
            ws = widths(fs)
            if size == "fit" and max(ws) > w:
                fs = font_spec(font, max(4, int(fs.h * w / max(ws))), smooth)
                ws = widths(fs)
            sx = sy = 1.0
            if size == "stretch":
                sx = w / max(ws)
                sy = (h - (n - 1) * gap) / (n * fs.h)
        else:
            fs = PIXEL
            ws = widths(fs)
            if size == "px":
                sx = sy = max(1, height // 8)
            elif size == "stretch":
                sx = w / max(ws)
                sy = (h - (n - 1) * gap) / (n * 8)
            else:
                f = min(avail / 8, w / max(ws))
                sx = sy = math.floor(f) if f >= 1 else f
        self.sx, self.sy = sx, sy
        self.lines = []
        words = 0   # two-color effect: keep alternating across lines
        for t, lw in zip(lines, ws):
            lkw = dict(kw)
            if kw.get("effect") == "two_color_words" and words % 2:
                lkw["color"], lkw["color2"] = kw.get("color2", "#ff00ff"), kw.get("color", "#00e5ff")
            words += len((t or "").split())
            sc = TextScene(lw, fs.h, t or " ", bg=bg, scroll=False, font=fs, **lkw)
            sc.start = self.start        # blink/breathe in step on every line
            self.lines.append(sc)
        self.lh = max(1, round(fs.h * sy))

    def render(self, now):
        if self.lines is None:
            v = self.inner.render(now)
            if self.direct:
                return v
            ow, oh = max(1, round(v.width * self.sx)), max(1, round(v.height * self.sy))
            out = Image.new("RGB", (self.w, self.h), self.bg)
            out.paste(_resize(v, (ow, oh)), (0, (self.h - oh) // 2))
            return out
        out = Image.new("RGB", (self.w, self.h), self.bg)
        n, gap = len(self.lines), self.LINE_GAP
        y = (self.h - (n * self.lh + (n - 1) * gap)) // 2
        for sc in self.lines:
            v = sc.render(now)
            ow = max(1, round(v.width * self.sx))
            img = _resize(v, (ow, self.lh)) if (ow, self.lh) != v.size else v
            if self.align == "left":
                x = 0
            elif self.align == "right":
                x = self.w - ow
            else:
                x = (self.w - ow) // 2
            out.paste(img, (x, y))
            y += self.lh + gap
        return out


def prepare_for_leds(img, w, h, mode="fill", enhance=True, bri=255, gamma=2.2,
                     lift=True, steps=1.0):
    """Turn a photo (e.g. album art) into a w x h frame that reads well on LEDs.

    mode: fill (crop to the sign's shape), fit (whole picture, black bars) or
    stretch. enhance: auto-contrast, smooth area-averaged downscale, more
    saturation and contrast, a light sharpen at the final size; tiny LED
    pictures look grey and soft otherwise. lift: at low brightness, raise dark
    tones just enough that they still light an LED (with gamma and WLED's
    brightness scaling, anything darker than about one output step goes black)."""
    from PIL import ImageOps, ImageFilter
    img = img.convert("RGB")
    if mode == "fill":
        r = max(w / img.width, h / img.height)
        cw, ch = min(img.width, round(w / r)), min(img.height, round(h / r))
        x, y = (img.width - cw) // 2, (img.height - ch) // 2
        img = img.crop((x, y, x + cw, y + ch))
        size = (w, h)
    elif mode == "fit":
        r = min(w / img.width, h / img.height)
        size = (max(1, round(img.width * r)), max(1, round(img.height * r)))
    else:
        size = (w, h)
    if enhance:
        img = ImageOps.autocontrast(img, cutoff=1)
    small = img.resize(size, Image.LANCZOS, reducing_gap=3.0) if (
        size[0] < img.width or size[1] < img.height) else img.resize(size, Image.NEAREST)
    if enhance:
        small = ImageEnhance.Color(small).enhance(1.45)
        small = ImageEnhance.Contrast(small).enhance(1.15)
        small = small.filter(ImageFilter.UnsharpMask(radius=0.8, percent=70, threshold=0))
    if lift and bri < 200:
        g = gamma if gamma and gamma > 0 else 1.0
        # input level that lights an LED: one output step, or a fraction of
        # one when dithering (the sign can then show in-between levels)
        floor = 255 * (max(0.05, steps) / max(bri, 1)) ** (1 / g)
        k = min(0.85, max(0.0, floor / 255 * 1.2))
        lut = [0 if v < 6 else round(255 * (k + (1 - k) * (v / 255) ** 0.9)) for v in range(256)]
        small = small.point(lut * 3)
    if small.size == (w, h):
        return small
    out = Image.new("RGB", (w, h), (0, 0, 0))
    out.paste(small, ((w - small.width) // 2, (h - small.height) // 2))
    return out


class ImageScene(Scene):
    """Static/animated image, with fit/stretch/scroll modes."""

    def __init__(self, w, h, img: Image.Image, mode="fit", speed=24, bg="#000000",
                 rate=1.0):
        super().__init__(w, h)
        self.rate = max(0.1, min(8.0, float(rate or 1.0)))   # GIF playback speed
        self.mode = mode if mode in IMAGE_MODES else "fit"
        self.speed = max(1, int(speed))
        self.bg = hex_to_rgb(bg)
        self.frames = []       # list of (img64x8, duration_s)
        self._prep(img)
        self.total = sum(d for _, d in self.frames) or 1.0

    def _fit_one(self, frame: Image.Image) -> Image.Image:
        frame = frame.convert("RGB")
        if self.mode == "stretch":
            return _resize(frame, (self.w, self.h))
        if self.mode == "scroll":
            # scale to height, keep width; caller handles horizontal scroll
            r = self.h / frame.height
            return _resize(frame, (max(1, int(frame.width * r)), self.h))
        if self.mode == "fill":
            # cover the whole screen, crop what sticks out (centered)
            r = max(self.w / frame.width, self.h / frame.height)
            nw, nh = max(self.w, round(frame.width * r)), max(self.h, round(frame.height * r))
            big = _resize(frame, (nw, nh))
            x, y = (nw - self.w) // 2, (nh - self.h) // 2
            return big.crop((x, y, x + self.w, y + self.h))
        # fit (contain), centered
        r = min(self.w / frame.width, self.h / frame.height)
        nw, nh = max(1, int(frame.width * r)), max(1, int(frame.height * r))
        scaled = _resize(frame, (nw, nh))
        canvas = Image.new("RGB", (self.w, self.h), self.bg)
        canvas.paste(scaled, ((self.w - nw) // 2, (self.h - nh) // 2))
        return canvas

    def _prep(self, img):
        is_anim = getattr(img, "is_animated", False)
        if is_anim:
            for fr in ImageSequence.Iterator(img):
                dur = max(0.03, fr.info.get("duration", 100) / 1000.0) / self.rate
                self.frames.append((self._fit_one(fr.copy()), dur))
        else:
            self.frames.append((self._fit_one(img), 1.0))
        # For scroll mode build a single wide loop strip instead
        if self.mode == "scroll":
            wide = self.frames[0][0]
            if wide.width > self.w:
                gap = self.w
                period = wide.width + gap
                loop = Image.new("RGB", (period * 2, self.h), self.bg)
                loop.paste(wide, (0, 0)); loop.paste(wide, (period, 0))
                self._scroll_loop = (loop, period)
            else:
                self.mode = "fit"
                self.frames = [(self._fit_one(wide), 1.0)]

    def render(self, now):
        if self.mode == "scroll" and hasattr(self, "_scroll_loop"):
            loop, period = self._scroll_loop
            offset = int((now - self.start) * self.speed) % period
            return loop.crop((offset, 0, offset + self.w, self.h))
        # animated / static: pick frame by elapsed time
        t = (now - self.start) % self.total
        acc = 0.0
        for frame, dur in self.frames:
            acc += dur
            if t < acc:
                return frame
        return self.frames[-1][0]


class BeatGifScene(ImageScene):
    """A GIF that dances to the music: one loop takes a whole number of beats
    (beats_per_loop, or "auto" = the power of two closest to the GIF's own
    speed), and the playhead is pulled into line with the beat. With no
    steady beat it plays at its normal speed (x rate)."""

    def __init__(self, w, h, img, mode="fit", rate=1.0, beats_per_loop="auto"):
        super().__init__(w, h, img, mode=mode, rate=1.0)
        self.free_rate = max(0.1, min(8.0, float(rate or 1.0)))
        self.bpl = beats_per_loop
        self.natural = sum(d for _, d in self.frames) or 1.0   # one loop at normal speed
        self.cum = []
        acc = 0.0
        for _, d in self.frames:
            self.cum.append(acc)
            acc += d
        self.loop = 0.0        # playhead, in loops (fraction = where in the GIF)
        self.prev = None
        self.locked = False

    def _beats_per_loop(self, period):
        if self.bpl != "auto":
            try:
                return max(1, min(16, int(self.bpl)))
            except (TypeError, ValueError):
                pass
        want = self.natural / period          # beats one natural loop lasts
        best = min((1, 2, 4, 8, 16), key=lambda n: abs(math.log(n / max(0.25, want))))
        return best

    def render(self, now):
        if self.mode == "scroll" and hasattr(self, "_scroll_loop"):
            return super().render(now)
        dt = 0.0 if self.prev is None else max(0.0, min(0.25, now - self.prev))
        self.prev = now
        from . import effects
        clock = effects.AUDIO.beat_clock() if effects.AUDIO is not None else None
        if clock is None or len(self.frames) < 2:
            self.locked = False
            self.loop += dt * self.free_rate / self.natural
        else:
            pos, period = clock
            n = self._beats_per_loop(period)
            target = pos / n                    # where the beat says we should be
            self.loop += dt / (period * n)      # move at the music's tempo...
            err = (target - self.loop) % 1.0
            if err > 0.5:
                err -= 1.0
            if not self.locked or abs(err) > 0.35:
                self.loop += err                # ...jump into place when (re)locking
            else:
                self.loop += err * min(1.0, dt * 3.0)   # ...or ease into the beat
            self.locked = True
        t = (self.loop % 1.0) * self.natural
        idx = 0
        for i, start in enumerate(self.cum):
            if start <= t:
                idx = i
            else:
                break
        return self.frames[idx][0]


class SolidScene(Scene):
    def __init__(self, w, h, color="#000000"):
        super().__init__(w, h)
        self.frame = Image.new("RGB", (w, h), hex_to_rgb(color))

    def render(self, now):
        return self.frame


class TestPatternScene(Scene):
    """Calibration patterns to figure out physical wiring."""

    def __init__(self, w, h, kind="border"):
        super().__init__(w, h)
        self.kind = kind

    def render(self, now):
        img = Image.new("RGB", (self.w, self.h), (0, 0, 0))
        d = ImageDraw.Draw(img)
        if self.kind == "origin":
            img.putpixel((0, 0), (255, 0, 0))          # top-left = red
            if self.w > 1:
                img.putpixel((1, 0), (0, 255, 0))      # +x = green
            if self.h > 1:
                img.putpixel((0, 1), (0, 0, 255))      # +y = blue
        elif self.kind == "rows":
            colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
                      (255, 0, 255), (0, 255, 255), (255, 255, 255), (128, 128, 128)]
            for y in range(self.h):
                d.line([(0, y), (self.w - 1, y)], fill=colors[y % len(colors)])
        elif self.kind == "sweep":
            x = int((now - self.start) * 20) % self.w
            d.line([(x, 0), (x, self.h - 1)], fill=(255, 255, 255))
        else:  # border
            d.rectangle([0, 0, self.w - 1, self.h - 1], outline=(0, 229, 255))
            img.putpixel((0, 0), (255, 0, 0))
        return img


def load_image_bytes(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data))
