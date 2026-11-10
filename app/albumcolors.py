"""Pick the prominent colors of an album cover, tuned to look good on LEDs."""
from __future__ import annotations
import colorsys
import io

from PIL import Image


def extract(data: bytes, n: int = 4) -> list:
    """Return up to n RGB tuples: the cover's main colors, most prominent first.
    Near-black, near-grey and near-white areas count for less (they read as
    'off' or plain white on LEDs); chosen colors are kept distinct and given
    full LED brightness."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((80, 80))
    q = im.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[: 12 * 3]
    counts = sorted(q.getcolors() or [], reverse=True)
    total = sum(c for c, _ in counts) or 1
    scored = []
    for count, idx in counts:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        weight = count / total
        weight *= 0.15 + s * 1.2                      # colorful beats grey
        weight *= 0.2 if v < 0.15 else (0.6 if v < 0.3 else 1.0)
        scored.append((weight, (h, s, v)))
    scored.sort(reverse=True)
    picked = []
    for _, (h, s, v) in scored:
        if any(min(abs(h - ph), 1 - abs(h - ph)) < 0.06 and abs(s - ps) < 0.35 for ph, ps, _ in picked):
            continue                                   # too close to one we have
        picked.append((h, s, v))
        if len(picked) == n:
            break
    out = []
    for h, s, v in picked:
        s = min(1.0, s * 1.25) if s > 0.15 else s      # a bit more saturated
        v = max(0.75, v)                               # LEDs: bright, not murky
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        out.append((int(r * 255), int(g * 255), int(b * 255)))
    return out or [(255, 255, 255)]
