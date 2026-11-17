"""GIF tools: black background (cut the character out) and dance-move finding.

Pure Pillow (the venv has no numpy), so the pixel work happens on a copy no
bigger than WORK px and the result is scaled back up.

black_background(): magic wand from the edges. It starts at the edge pixels
that have one of the edge's main colours and spreads to neighbours of nearly
the same colour, so it follows gradients and soft shading but stops at
outlines. Everything it reaches turns black. (Comparing frames to find a still
background was tried and dropped: it also ate the parts of a cartoon
character that don't move.) Busy photo backgrounds need a real segmentation
model; this is meant for plain, gradient and simple backgrounds.

find_moves(): finds the dance's own beat (tempo + where the beats fall), the
way you'd tap along to the GIF, from where moves land (fast motion stopping or
turning back). Each beat snaps to the frame where a pose really lands, and
dance_map() turns that into a time warp so each dance beat lands on a music
beat.
"""
from __future__ import annotations
import io
import math
from collections import deque

from PIL import Image, ImageChops, ImageFilter, ImageSequence

WORK = 200            # max side of the analysis copy for the background cut
MOVE_SIDE = 64        # max side for motion analysis


def _frames(img: Image.Image):
    """[(RGB frame, duration_s)] with transparency over black."""
    out = []
    for fr in ImageSequence.Iterator(img):
        d = max(0.02, (fr.info.get("duration") or 100) / 1000.0)
        f = fr.convert("RGBA")
        bg = Image.new("RGBA", f.size, (0, 0, 0, 255))
        out.append((Image.alpha_composite(bg, f).convert("RGB"), d))
    return out or [(img.convert("RGB"), 1.0)]


def _dist(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def _edge_colours(px, w, h, tol):
    """Main colours along the edge: (colour, share) for colours covering >= 6% of it."""
    edge = [px[x] for x in range(w)] + [px[(h - 1) * w + x] for x in range(w)]
    edge += [px[y * w] for y in range(h)] + [px[y * w + w - 1] for y in range(h)]
    groups = []          # [colour_sum, count, first colour]
    for c in edge:
        for g in groups:
            if _dist(c, g[2]) <= tol:
                g[0][0] += c[0]; g[0][1] += c[1]; g[0][2] += c[2]; g[1] += 1
                break
        else:
            groups.append([[c[0], c[1], c[2]], 1, c])
            if len(groups) > 40:          # very busy edge: stop collecting
                break
    n = len(edge)
    return [(tuple(v // g[1] for v in g[0]), g[1] / n) for g in groups if g[1] / n >= 0.06]


def _wand(px, w, h, step, far):
    """Background mask (bytearray, 1 = background) grown from the edge."""
    tol_group = max(8, step)
    mains = [c for c, _ in _edge_colours(px, w, h, tol_group)]
    bg = bytearray(w * h)
    if not mains:
        return bg
    q = deque()
    seedc = [None] * (w * h)
    edge_idx = [x for x in range(w)] + [(h - 1) * w + x for x in range(w)]
    edge_idx += [y * w for y in range(h)] + [y * w + w - 1 for y in range(h)]
    for i in edge_idx:
        if bg[i]:
            continue
        c = px[i]
        m = min(mains, key=lambda mc: _dist(c, mc))
        if _dist(c, m) <= tol_group:
            bg[i] = 1
            seedc[i] = m
            q.append(i)
    while q:
        i = q.popleft()
        c = px[i]
        s = seedc[i]
        y, x = divmod(i, w)
        for j, ok in ((i - 1, x > 0), (i + 1, x < w - 1), (i - w, y > 0), (i + w, y < h - 1)):
            if ok and not bg[j]:
                cj = px[j]
                # nearly the same as the neighbour, and not too far from where the fill started
                if _dist(cj, c) <= step and _dist(cj, s) <= far:
                    bg[j] = 1
                    seedc[j] = s
                    q.append(j)
    return bg


LINE_MODES = ("keep", "edges", "all")


def black_background(data: bytes, strength: float = 1.0, holes: bool = False,
                     lines: str = "keep", line_color=(255, 255, 255), trim: bool = True,
                     blacken: bool = True, square: str = "none") -> bytes:
    """Return GIF bytes of the picture with its background turned black.
    strength 0.5..2: how different a colour may be and still count as background.
    holes: also blacken closed-in patches of the background colour (between arms...).
    lines: black line art (drawn for a white background) would vanish on black:
      "keep" leaves it black, "edges" recolours dark pixels near the background
      (thin lines change completely, big black areas get a coloured rim),
      "all" recolours every dark pixel of the character. line_color = (r, g, b).
    trim: crop the empty black edges away (the same box for every frame) so
      the character fills the screen when it's scaled to fit.
    blacken False: only trim (the background is found the same way, but kept).
    square: "none"; "crop" = cut to a square around the character (as big as
      the picture allows); "pad" = add background around it to make a square
      (black, or the GIF's own edge colour when the background is kept)."""
    strength = max(0.3, min(2.5, float(strength)))
    lines = lines if lines in LINE_MODES else "keep"
    img = Image.open(io.BytesIO(data))
    frames = _frames(img)
    W, H = frames[0][0].size
    r = min(1.0, WORK / max(W, H))
    w, h = max(1, round(W * r)), max(1, round(H * r))
    step, far = round(10 * strength), round(70 * strength)
    smalls = [f.resize((w, h), Image.BOX) if (w, h) != (W, H) else f for f, _ in frames]
    band_px = max(1, round(max(w, h) / 50))            # how far "near the background" reaches
    out = []
    for (full, dur), small in zip(frames, smalls):
        px = list(small.getdata())
        bg = _wand(px, w, h, step, far)
        if holes:
            mains = [c for c, _ in _edge_colours(px, w, h, max(8, step))]
            for i, c in enumerate(px):
                if not bg[i] and any(_dist(c, m) <= step for m in mains):
                    bg[i] = 1
        small_mask = Image.frombytes("L", (w, h), bytes(255 if b else 0 for b in bg))
        small_mask = small_mask.filter(ImageFilter.MedianFilter(3))      # drop single specks
        mask = small_mask
        if (w, h) != (W, H):
            mask = mask.resize((W, H), Image.BILINEAR).point(lambda v: 255 if v >= 110 else 0)
        if lines == "keep":
            mask = mask.filter(ImageFilter.MaxFilter(3))              # eat the light halo at the edges
        if not blacken:
            ec = _edge_colours(px, w, h, max(8, step))
            fill = max(ec, key=lambda c: c[1])[0] if ec else (0, 0, 0)
            out.append((full, dur, mask, fill))
            continue
        black = Image.new("RGB", (W, H), (0, 0, 0))
        frame = Image.composite(black, full, mask)
        if lines != "keep":
            dark = full.convert("L").point(lambda v: 255 if v < 100 else 0)
            dark = ImageChops.subtract(dark, mask)                      # only the character's own pixels
            if lines == "edges":
                near = small_mask.filter(ImageFilter.MaxFilter(2 * band_px + 1))
                if (w, h) != (W, H):
                    near = near.resize((W, H), Image.BILINEAR).point(lambda v: 255 if v >= 60 else 0)
                dark = ImageChops.multiply(dark, near)
            frame = Image.composite(Image.new("RGB", (W, H), tuple(line_color)), frame, dark)
        out.append((frame, dur, mask, (0, 0, 0)))
    box = (0, 0, W, H)
    if trim or square in ("crop", "pad"):
        cbox = None                                      # where the character is, over all frames
        for _, _, m, _ in out:
            b = ImageChops.invert(m).getbbox()
            if b:
                cbox = b if cbox is None else (min(cbox[0], b[0]), min(cbox[1], b[1]),
                                               max(cbox[2], b[2]), max(cbox[3], b[3]))
        if cbox is None:
            cbox = (0, 0, W, H)
        if trim and (cbox[2] - cbox[0]) * (cbox[3] - cbox[1]) < 0.97 * W * H:
            pad = round(0.03 * max(cbox[2] - cbox[0], cbox[3] - cbox[1]))
            box = (max(0, cbox[0] - pad), max(0, cbox[1] - pad), min(W, cbox[2] + pad), min(H, cbox[3] + pad))
        bw, bh = box[2] - box[0], box[3] - box[1]
        if square in ("crop", "pad") and bw != bh:
            # centre the square on the character (not on the picture)
            cx, cy = (cbox[0] + cbox[2]) / 2, (cbox[1] + cbox[3]) / 2
            if square == "crop":
                side = min(max(bw, bh), W, H)
                x0 = round(min(max(cx - side / 2, 0), W - side))
                y0 = round(min(max(cy - side / 2, 0), H - side))
            else:
                side = max(bw, bh)
                # keep the trimmed box inside the square
                x0 = round(min(max(cx - side / 2, box[2] - side), box[0]))
                y0 = round(min(max(cy - side / 2, box[3] - side), box[1]))
            box = (x0, y0, x0 + side, y0 + side)
    if box != (0, 0, W, H):
        cropped = []
        for f, d, m, fill in out:
            if 0 <= box[0] and 0 <= box[1] and box[2] <= W and box[3] <= H:
                cropped.append((f.crop(box), d, m, fill))
            else:                                        # square bigger than the picture: pad
                c = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), fill)
                c.paste(f, (-box[0], -box[1]))
                cropped.append((c, d, m, fill))
        out = cropped
    out = [(f, d) for f, d, _, _ in out]
    buf = io.BytesIO()
    pal = [f.quantize(255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f, _ in out]
    durs = [round(d * 1000) for _, d in out]
    if len(pal) == 1:
        pal[0].save(buf, "GIF")
    else:
        pal[0].save(buf, "GIF", save_all=True, append_images=pal[1:], duration=durs,
                    loop=0, disposal=1, optimize=False)
    return buf.getvalue()


# ---------------------------------------------------------------- dance moves
def _landings(frames):
    """Per frame: how strongly a move lands there (0..1), and the motion speed.

    A move lands where fast motion stops (motion into the frame minus motion
    out of it) or turns back (the frames either side look alike while the
    steps to them don't: an arm swinging out and back)."""
    n = len(frames)
    W, H = frames[0][0].size
    r = min(1.0, MOVE_SIDE / max(W, H))
    size = (max(1, round(W * r)), max(1, round(H * r)))
    g = [f.convert("L").resize(size, Image.BOX).filter(ImageFilter.GaussianBlur(0.7)) for f, _ in frames]

    def diff(a, b):
        h = ImageChops.difference(a, b).histogram()
        return sum(v * c for v, c in enumerate(h) if v > 6)     # ignore compression noise

    step = [diff(g[i], g[(i + 1) % n]) for i in range(n)]       # frame i -> i+1
    mot = [step[i] / frames[i][1] for i in range(n)]            # per second
    pk = max(mot) or 1.0
    mot = [m / pk for m in mot]
    env = []
    for i in range(n):
        d1, d2 = step[i - 1], step[i]
        d3 = diff(g[i - 1], g[(i + 1) % n])
        turn = max(0.0, (d1 + d2 - d3) / (d1 + d2 + 1e-9))
        stop = max(0.0, mot[i - 1] - mot[i])
        env.append(stop + 0.5 * turn * max(mot[i - 1], mot[i]))
    pk = max(env) or 1.0
    return [e / pk for e in env], mot


def find_moves(frames):
    """The dance's own beat, like tapping along to the GIF.

    Tries every whole number of dance beats per loop (beats 0.25..1.3 s apart)
    and every starting frame, scoring how strongly moves land on that grid
    (favouring about 0.5 s, a usual dance tempo). Then each grid beat snaps to
    the frame where a move actually lands nearby. Returns a dict:
      total: loop length (s); beats: GIF times of the dance beats (first = 0
      after offset); offset: GIF time of dance beat 0; spacing: s between
      beats; frames: frame index shown on each beat; steady: 0..1 how clearly
      the GIF keeps a beat. None for stills / two-frame GIFs."""
    import bisect
    n = len(frames)
    if n < 3:
        return None
    env, _ = _landings(frames)
    starts, acc = [], 0.0
    for _, d in frames:
        starts.append(acc)
        acc += d
    total = acc
    md = sorted(d for _, d in frames)[n // 2]
    mean = sum(env) / n

    def at(t, win):
        """Strongest landing within win of time t (wraps), fading with distance."""
        t %= total
        best = 0.0
        for tt in (t - total, t, t + total):
            lo = bisect.bisect_left(starts, tt - win)
            hi = bisect.bisect_right(starts, tt + win)
            for i in range(lo, hi):
                dt = abs(starts[i] - tt)
                best = max(best, env[i] * (1 - 0.5 * dt / max(win, 1e-9)))
        return best

    best = None
    for N in range(1, 65):
        sp = total / N
        if sp < 0.25 or sp > 1.3:
            continue
        win = min(0.3 * sp, max(0.06, 1.05 * md))
        for i in range(n):
            sc = sum(at(starts[i] + k * sp, win) for k in range(N)) / N - mean
            prior = math.exp(-0.5 * (math.log(sp / 0.5) / math.log(2.2)) ** 2)
            if best is None or sc * prior > best[0]:
                best = (sc * prior, sc, N, sp, starts[i])
    if best is None:                       # very short loop: one beat per loop
        return {"total": total, "beats": [0.0], "offset": 0.0, "spacing": total,
                "frames": [0], "steady": 0.0}
    _, sc, N, sp, ph = best
    # beat 0: move the grid onto the strongest landing within a frame or so
    win = max(md * 1.05, 0.2 * sp)

    def near(i, t):                          # signed distance (wraps) from frame i to time t
        return (starts[i] - t + total / 2) % total - total / 2

    def snap(t):
        """Frame index of the clearest landing near time t, or None."""
        best_i, best_v = None, 0.0
        for i in range(n):
            dt = abs(near(i, t))
            v = env[i] * (1 - 0.4 * dt / win)
            if dt <= win and env[i] >= 0.25 and v > best_v:
                best_i, best_v = i, v
        return best_i

    i0 = snap(ph)
    if i0 is not None:
        ph = starts[i0]
    # other beats: the clearest landing near each grid point, kept in order
    times, idxs = [0.0], [i0 if i0 is not None else max(0, bisect.bisect_right(starts, ph) - 1)]
    for k in range(1, N):
        t = k * sp
        i = snap(ph + t)
        rel = None if i is None else (starts[i] - ph) % total
        if rel is not None and times[-1] + 0.3 * sp < rel < total - 0.3 * sp:
            times.append(rel)
            idxs.append(i)
        else:
            times.append(t)
            idxs.append(max(0, bisect.bisect_right(starts, (ph + t) % total) - 1))
    return {"total": total, "beats": times, "offset": ph, "spacing": sp, "frames": idxs,
            "steady": max(0.0, min(1.0, sc / 0.3))}


def dance_map(dance, period, bpl="auto"):
    """Fit the dance's beats to the music: (beats_per_loop, anchors, offset).

    anchors = [(music_beat, gif_time - offset)] from (0, 0) to (beats_per_loop,
    total); between anchors the GIF plays at an even speed. Normally one dance
    beat = one music beat; half or double time only when the GIF would
    otherwise run at less than ~0.6x or more than ~1.7x its own speed. A fixed
    bpl stretches the dance beats evenly over that many music beats."""
    total, beats = dance["total"], dance["beats"]
    N = len(beats)
    fixed = None
    if bpl not in ("auto", "loop"):
        try:
            fixed = max(1, min(32, int(bpl)))
        except (TypeError, ValueError):
            fixed = None
    if fixed:
        per = fixed / N
    else:
        def cost(m):
            speed = total / (N * m * period)          # how much faster than its own speed
            return abs(math.log(speed)) + (0.0 if m == 1 else 0.55)
        per = min([m for m in (0.5, 1, 2) if (N * m) == int(N * m) and 1 <= N * m <= 32] or [1], key=cost)
    nb = N * per
    anchors = [(k * per, t) for k, t in enumerate(beats)] + [(float(nb), total)]
    return (int(round(nb)) if not fixed else fixed), anchors, dance["offset"]


def warp(anchors, offset, total, beat_pos):
    """GIF time (s, within the loop) for a position in beats inside the loop."""
    for (b0, t0), (b1, t1) in zip(anchors, anchors[1:]):
        if beat_pos <= b1:
            f = 0.0 if b1 <= b0 else (beat_pos - b0) / (b1 - b0)
            return (offset + t0 + f * (t1 - t0)) % total
    return offset % total


def pose_strip(img, dance, cell=64):
    """PNG of the frame on each dance beat (what lands on the music's beats)."""
    frames = _frames(img)
    idxs = dance["frames"] if dance else [0]
    cols = min(len(idxs), 8)
    rows = (len(idxs) + cols - 1) // cols
    W, H = frames[0][0].size
    r = cell / max(W, H)
    cw, ch = max(1, round(W * r)), max(1, round(H * r))
    sheet = Image.new("RGB", (cols * (cw + 2), rows * (ch + 2)), (20, 20, 20))
    for k, i in enumerate(idxs):
        f = frames[i][0].resize((cw, ch), Image.LANCZOS)
        sheet.paste(f, ((k % cols) * (cw + 2), (k // cols) * (ch + 2)))
    buf = io.BytesIO()
    sheet.save(buf, "PNG")
    return buf.getvalue()


_CACHE = {}      # (path, mtime) -> find_moves() result (small dict)


def analyse(path, img):
    """find_moves() for a library file (PIL image), cached by path + modification time."""
    import os
    try:
        key = (path, os.path.getmtime(path))
    except (OSError, TypeError):
        key = None
    if key and key in _CACHE:
        return _CACHE[key]
    res = find_moves(_frames(img))
    # don't keep a big GIF's frames alive in the cache
    if key:
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = res
    return res
