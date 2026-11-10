"""Original 90s-web-style pixel animations for the GIF library's defaults.

All drawn here in code (no copied artwork): spinning globe, "under
construction" barrier, NEW! starburst, flapping mail envelope, bouncing smiley,
Mystify-style lines, HOT! flames, spinning CD-ROM, busy hourglass, and a
dial-up modem's blinking lights. 32x32, looped GIFs.
"""
from __future__ import annotations
import io
import math
import random

from PIL import Image, ImageDraw

N = 32

# 3x5 pixel font for the few words we need
_FONT = {
    "N": ["101", "111", "111", "111", "101"], "E": ["111", "100", "110", "100", "111"],
    "W": ["101", "101", "111", "111", "101"], "!": ["010", "010", "010", "000", "010"],
    "H": ["101", "101", "111", "101", "101"], "O": ["111", "101", "101", "101", "111"],
    "T": ["111", "010", "010", "010", "010"], "@": ["111", "101", "111", "100", "111"],
}


def _text(d, x, y, s, color, scale=1):
    for ch in s:
        rows = _FONT.get(ch)
        if rows:
            for j, row in enumerate(rows):
                for i, bit in enumerate(row):
                    if bit == "1":
                        d.rectangle([x + i * scale, y + j * scale, x + i * scale + scale - 1, y + j * scale + scale - 1], fill=color)
        x += 4 * scale


def _gif(frames, ms):
    buf = io.BytesIO()
    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in frames]
    pal[0].save(buf, "GIF", save_all=True, append_images=pal[1:], duration=ms, loop=0, disposal=2, optimize=False)
    return buf.getvalue()


def globe():
    rng = random.Random(7)
    W, H = 64, 32   # longitude x latitude land map, made from blobs
    land = [[0] * W for _ in range(H)]
    for _ in range(9):
        cx, cy, r = rng.randrange(W), rng.randrange(5, H - 5), rng.uniform(3, 8)
        for y in range(H):
            for x in range(W):
                dx = min(abs(x - cx), W - abs(x - cx))
                if (dx / 1.3) ** 2 + (y - cy) ** 2 < r * r * rng.uniform(0.7, 1.0):
                    land[y][x] = 1
    frames = []
    R = 13.5
    for f in range(32):
        im = Image.new("RGB", (N, N), (0, 0, 0))
        px = im.load()
        for y in range(N):
            for x in range(N):
                dx, dy = (x - 15.5) / R, (y - 15.5) / R
                if dx * dx + dy * dy <= 1:
                    z = math.sqrt(1 - dx * dx - dy * dy)
                    lon = math.atan2(dx, z) / math.tau + f / 32
                    lat = math.asin(dy) / math.pi + 0.5
                    l = land[min(H - 1, int(lat * H))][int((lon % 1) * W)]
                    shade = 0.35 + 0.65 * max(0.0, z * 0.8 - dx * 0.3 - dy * 0.3)
                    base = (40, 170, 60) if l else (20, 70, 200)
                    if l and abs(dy) > 0.85:
                        base = (230, 240, 255)
                    px[x, y] = tuple(int(c * shade) for c in base)
        frames.append(im)
    return _gif(frames, 70)


def construction():
    frames = []
    for f in range(8):
        im = Image.new("RGB", (N, N), (0, 0, 0))
        d = ImageDraw.Draw(im)
        for x in range(-16, 48):        # hazard stripes, moving
            for y in range(12, 22):
                if ((x + y + f * 2) // 4) % 2 == 0:
                    im.putpixel((x, y), (255, 200, 0)) if 0 <= x < N else None
        d.rectangle([0, 12, 31, 21], outline=(255, 255, 255))
        d.rectangle([4, 22, 6, 30], fill=(180, 180, 180))
        d.rectangle([25, 22, 27, 30], fill=(180, 180, 180))
        on = f % 4 < 2
        for lx in (5, 26):
            d.ellipse([lx - 2, 6, lx + 2, 10], fill=(255, 120, 0) if on else (90, 40, 0))
            if on:
                d.point([(lx, 4), (lx - 4, 8), (lx + 4, 8)], fill=(255, 200, 80))
        frames.append(im)
    return _gif(frames, 120)


def new_star():
    frames = []
    for f in range(8):
        im = Image.new("RGB", (N, N), (0, 0, 0))
        d = ImageDraw.Draw(im)
        pts = []
        spikes, rot = 12, f / 8 * math.tau / 12
        rr = 15 if f % 2 == 0 else 14
        for k in range(spikes * 2):
            r = rr if k % 2 == 0 else 10
            a = rot + k * math.pi / spikes
            pts.append((15.5 + math.cos(a) * r, 15.5 + math.sin(a) * r))
        d.polygon(pts, fill=(230, 0, 30) if f % 4 < 2 else (255, 40, 60))
        _text(d, 5, 13, "NEW!", (255, 255, 0) if f % 2 == 0 else (255, 255, 255))
        frames.append(im)
    return _gif(frames, 110)


def mail():
    frames = []
    for f in range(16):
        im = Image.new("RGB", (N, N), (0, 0, 40))
        d = ImageDraw.Draw(im)
        bob = int(round(math.sin(f / 16 * math.tau) * 2))
        y0 = 11 + bob
        open_ = f >= 8
        if open_:                       # letter peeks out
            ly = y0 - min(7, (f - 8) * 2)
            d.rectangle([9, ly, 22, ly + 10], fill=(255, 255, 255))
            d.line([11, ly + 3, 20, ly + 3], fill=(120, 120, 255))
            d.line([11, ly + 5, 18, ly + 5], fill=(120, 120, 255))
        d.rectangle([5, y0, 26, y0 + 14], fill=(240, 230, 200), outline=(140, 110, 60))
        d.line([5, y0 + 14, 15, y0 + 7], fill=(180, 150, 100))
        d.line([26, y0 + 14, 16, y0 + 7], fill=(180, 150, 100))
        if open_:
            d.polygon([(5, y0), (26, y0), (15, y0 - 7)], fill=(225, 210, 170), outline=(140, 110, 60))
        else:
            d.polygon([(5, y0), (26, y0), (15, y0 + 8)], fill=(225, 210, 170), outline=(140, 110, 60))
        if f % 4 < 2:
            _text(d, 25, 2, "@", (255, 60, 60))
        frames.append(im)
    return _gif(frames, 100)


def smiley():
    frames = []
    for f in range(16):
        im = Image.new("RGB", (N, N), (0, 0, 0))
        d = ImageDraw.Draw(im)
        ph = f / 16
        y = 16 - abs(math.sin(ph * math.pi)) * 7
        squash = 1.0 if y < 15 else 0.85
        rx, ry = 10 / squash ** 0.5, 10 * squash
        cy = y + (10 - ry)
        d.ellipse([16 - rx, cy - ry + 4, 16 + rx, cy + ry + 4], fill=(255, 220, 0), outline=(200, 140, 0))
        ey = cy + 1
        d.rectangle([12, ey - 1, 13, ey + 2], fill=(0, 0, 0))
        d.rectangle([19, ey - 1, 20, ey + 2], fill=(0, 0, 0))
        d.arc([10, cy - 1, 22, cy + 10], 20, 160, fill=(0, 0, 0))
        d.line([4, 31, 27, 31], fill=(60, 60, 255))
        frames.append(im)
    return _gif(frames, 70)


def mystify():
    rng = random.Random(3)
    shapes = []
    for col in ((255, 60, 200), (60, 220, 255)):
        pts = [[rng.uniform(0, 31), rng.uniform(0, 31), rng.uniform(-1.6, 1.6), rng.uniform(-1.6, 1.6)] for _ in range(4)]
        shapes.append((col, pts, []))
    frames = []
    for f in range(60):
        im = Image.new("RGB", (N, N), (0, 0, 0))
        d = ImageDraw.Draw(im)
        for col, pts, trail in shapes:
            for p in pts:
                p[0] += p[2]
                p[1] += p[3]
                for i, lim in ((0, 31), (1, 31)):
                    if p[i] < 0 or p[i] > lim:
                        p[i + 2] = -p[i + 2]
                        p[i] = max(0, min(lim, p[i]))
            trail.append([(p[0], p[1]) for p in pts])
            del trail[:-4]
            for k, poly in enumerate(trail):
                a = (k + 1) / len(trail)
                d.polygon(poly, outline=tuple(int(c * a) for c in col))
        frames.append(im)
    return _gif(frames, 60)


def hot():
    rng = random.Random(5)
    frames = []
    heat = [[0.0] * N for _ in range(N)]
    for f in range(24):
        for x in range(N):
            heat[N - 1][x] = rng.uniform(0.6, 1)
        for y in range(N - 1):
            for x in range(N):
                v = (heat[y + 1][x] * 2 + heat[y + 1][max(0, x - 1)] + heat[y + 1][min(N - 1, x + 1)]) / 4.0
                heat[y][x] = max(0.0, v - rng.uniform(0, 0.09))
        im = Image.new("RGB", (N, N))
        px = im.load()
        for y in range(N):
            for x in range(N):
                v = heat[y][x]
                px[x, y] = (int(min(1, v * 1.6) * 255), int(max(0, min(1, v * 1.6 - 0.5)) * 255), int(max(0, v - 0.85) * 600))
        d = ImageDraw.Draw(im)
        _text(d, 3, 9, "HOT", (255, 255, 255), 2)
        _text(d, 27, 9, "!", (255, 255, 0), 2) if f % 2 == 0 else None
        if f >= 8:
            frames.append(im)
    return _gif(frames, 80)


def cdrom():
    frames = []
    for f in range(12):
        im = Image.new("RGB", (N, N), (0, 0, 0))
        px = im.load()
        for y in range(N):
            for x in range(N):
                dx, dy = x - 15.5, y - 15.5
                r = math.hypot(dx, dy)
                if 3.5 < r <= 14.5:
                    a = math.atan2(dy, dx) / math.tau + f / 12
                    h = (a * 2 + r * 0.03) % 1
                    rgb = [int(150 + 105 * math.cos(math.tau * (h + k / 3))) for k in range(3)]
                    px[x, y] = tuple(rgb)
                elif r <= 3.5 and r > 1.5:
                    px[x, y] = (200, 200, 210)
        frames.append(im)
    return _gif(frames, 70)


def hourglass():
    frames = []
    for f in range(20):
        im = Image.new("RGB", (N, N), (0, 0, 0))
        d = ImageDraw.Draw(im)
        flip = f >= 16
        k = min(f, 15) / 15
        top = [(8, 6), (24, 6), (16, 16)]
        bot = [(16, 16), (8, 26), (24, 26)]
        d.polygon(top, outline=(200, 200, 255))
        d.polygon(bot, outline=(200, 200, 255))
        sand = (255, 210, 90)
        th = int(9 * (1 - k))           # sand left on top
        if th > 0:
            d.polygon([(16 - th * 0.8, 16 - th), (16 + th * 0.8, 16 - th), (16, 16)], fill=sand)
        bh = int(9 * k)
        if bh > 0:
            d.polygon([(9, 25), (23, 25), (16, 25 - bh)], fill=sand)
        if 0 < k < 1:
            d.line([16, 16, 16, 24], fill=sand)
        d.rectangle([6, 4, 26, 5], fill=(160, 110, 60))
        d.rectangle([6, 27, 26, 28], fill=(160, 110, 60))
        if flip:
            im = im.rotate((f - 15) * 45, resample=Image.NEAREST)
        frames.append(im)
    return _gif(frames, 90)


def modem():
    rng = random.Random(9)
    frames = []
    for f in range(16):
        im = Image.new("RGB", (N, N), (0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([2, 10, 29, 24], radius=3, fill=(60, 60, 70), outline=(140, 140, 150))
        d.rectangle([4, 21, 27, 22], fill=(40, 40, 46))
        cols = [(255, 40, 40), (255, 200, 0), (0, 255, 80), (0, 255, 80), (0, 255, 80), (255, 40, 40)]
        for i, c in enumerate(cols):
            on = i < 2 or rng.random() < 0.55
            d.rectangle([5 + i * 4, 14, 6 + i * 4, 15], fill=c if on else tuple(x // 6 for x in c))
        for i in range(3):        # little sound waves
            if (f // 2 + i) % 3 == 0:
                d.arc([20 + i * 2, 1 + i, 30 + i * 2, 9 - i], 200, 340, fill=(120, 180, 255))
        frames.append(im)
    return _gif(frames, 110)


DEFAULTS = {
    "spinning-globe": globe,
    "under-construction": construction,
    "new-starburst": new_star,
    "youve-got-mail": mail,
    "bouncing-smiley": smiley,
    "mystify": mystify,
    "hot-flames": hot,
    "cd-rom": cdrom,
    "hourglass": hourglass,
    "dial-up-modem": modem,
}
