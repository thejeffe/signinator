"""Custom bitmap glyphs for the LED matrix, addressed by :token: in text.

These are small pixel-art *color-lettering* badges -- the school abbreviation in
its team colors on a colored tile (USC = gold on cardinal, UCLA = gold on blue).
They are plain lettering, NOT reproductions of any school's trademarked logo
artwork. Each glyph is 8px tall and as wide as the word needs. They render
as-is (no effect recoloring), the same way emoji glyphs do.
"""
from __future__ import annotations
import os
import re
from PIL import Image

# school colors
CARD = (153, 27, 30)          # USC cardinal
GOLD = (255, 204, 0)          # USC gold
BLUE = (39, 116, 174)         # UCLA blue
UGOLD = (255, 209, 0)         # UCLA gold
OGREEN = (0, 79, 39)          # Oregon green
OYELLOW = (254, 225, 35)      # Oregon yellow

# 3x5 micro-font for the letters we need.
_LETTERS = {
    "U": ["X.X", "X.X", "X.X", "X.X", "XXX"],
    "S": ["XXX", "X..", "XXX", "..X", "XXX"],
    "C": ["XXX", "X..", "X..", "X..", "XXX"],
    "L": ["X..", "X..", "X..", "X..", "XXX"],
    "A": ["XXX", "X.X", "XXX", "X.X", "X.X"],
    "O": ["XXX", "X.X", "X.X", "X.X", "XXX"],
}


def _letter_badge(word, fg, bg, pad=1, gap=1, height=8):
    """Build an 8px-tall color grid: `word` in `fg` on a solid `bg` tile."""
    lw = 3
    gw = len(word) * lw + (len(word) - 1) * gap
    w = gw + pad * 2
    grid = [[bg for _ in range(w)] for _ in range(height)]
    y0 = (height - 5) // 2
    x = pad
    for ch in word:
        pat = _LETTERS[ch]
        for r, row in enumerate(pat):
            for c, v in enumerate(row):
                if v == "X":
                    grid[y0 + r][x + c] = fg
        x += lw + gap
    return grid


# --- USC: gold letters on a cardinal tile ---
USC = _letter_badge("USC", GOLD, CARD)

# --- UCLA: gold letters on a blue tile ---
UCLA = _letter_badge("UCLA", UGOLD, BLUE)

# --- Oregon: yellow letters on a green tile ---
OREGON = _letter_badge("UO", OYELLOW, OGREEN)


# Built-in token -> pixel grid (list of rows of (r,g,b) or None).
BUILTIN = {
    "usc": USC,
    "ucla": UCLA,
    "oregon": OREGON,
}

# :token: with a lowercase/digit/underscore name
TOKEN_RE = re.compile(r":([a-z0-9_]+):")

# Valid user-supplied emoji names (also used for the on-disk PNG filename).
NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# ---------------------------------------------------------------------------
# User-uploaded custom emoji.
#
# Users add their OWN images (things they have the rights to use). Each is a
# PNG stored in <store_dir>/<name>.png that already contains an alpha channel;
# transparent pixels fall through to the strip background. This module never
# fetches or generates any third-party artwork -- it only loads files the user
# supplied and resizes them to the 8px panel height.
# ---------------------------------------------------------------------------
_STORE_DIR = None            # set via set_store_dir()
_USER_CACHE = {}             # name -> (mtime, RGBA Image at panel height)
HEIGHT = 8


def set_store_dir(path: str) -> None:
    """Point the user-emoji store at a directory (created if missing)."""
    global _STORE_DIR
    _STORE_DIR = path
    if path:
        os.makedirs(path, exist_ok=True)


def _user_path(name: str):
    if not _STORE_DIR or not NAME_RE.match(name or ""):
        return None
    return os.path.join(_STORE_DIR, f"{name}.png")


def _load_user(name: str):
    """Load a user PNG as an RGBA image scaled to HEIGHT, with mtime caching."""
    p = _user_path(name)
    if not p or not os.path.exists(p):
        return None
    mtime = os.path.getmtime(p)
    cached = _USER_CACHE.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        img = Image.open(p).convert("RGBA")
    except Exception:
        return None
    if img.height != HEIGHT:
        r = HEIGHT / img.height
        img = img.resize((max(1, round(img.width * r)), HEIGHT), Image.LANCZOS)
    _USER_CACHE[name] = (mtime, img)
    return img


def list_user() -> list:
    """Sorted list of user emoji names currently on disk."""
    if not _STORE_DIR or not os.path.isdir(_STORE_DIR):
        return []
    names = []
    for fn in os.listdir(_STORE_DIR):
        if fn.endswith(".png"):
            n = fn[:-4]
            if NAME_RE.match(n):
                names.append(n)
    return sorted(names)


def delete_user(name: str) -> bool:
    p = _user_path(name)
    if p and os.path.exists(p):
        os.remove(p)
        _USER_CACHE.pop(name, None)
        return True
    return False


def all_tokens() -> dict:
    """{name: 'builtin'|'user'} for every renderable token (for the UI)."""
    out = {n: "builtin" for n in BUILTIN}
    for n in list_user():
        out[n] = "user"
    return out


def has(token: str) -> bool:
    t = (token or "").lower()
    return t in BUILTIN or _user_path(t) is not None and os.path.exists(_user_path(t))


def glyph_image(token: str, bg=(0, 0, 0)):
    """Return an RGB Image (height HEIGHT) for token, or None if unknown.
    Transparent pixels are filled with bg so the glyph sits on the strip bg.
    Built-in badges win over a same-named user upload."""
    t = (token or "").lower()
    grid = BUILTIN.get(t)
    if grid is not None:
        h, w = len(grid), len(grid[0])
        img = Image.new("RGB", (w, h), bg)
        px = img.load()
        for y, row in enumerate(grid):
            for x, c in enumerate(row):
                if c is not None:
                    px[x, y] = c
        return img
    u = _load_user(t)
    if u is not None:
        out = Image.new("RGB", u.size, bg)
        out.paste(u, (0, 0), u)   # alpha -> bg
        return out
    return None
