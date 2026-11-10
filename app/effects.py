"""WLED-style animated effects for the sign (our own implementations).

Every effect takes the same controls as WLED's effect page:
  speed 0-255, intensity 0-255, palette, and three colors (c1, c2, c3).
EffectScene renders one at the sign's size; frames are plain RGB images.
"""
from __future__ import annotations
import math
import random
import time

from PIL import Image

# Set by the engine: an audio.AudioAnalyzer for music-reactive effects.
AUDIO = None
# Set by NowPlaying when the album art changes: {"colors": [(r,g,b), ...], "version": id}
ALBUM = None
_SILENT = None


def audio_now():
    global _SILENT
    if AUDIO is not None:
        return AUDIO.at()
    if _SILENT is None:
        from .audio import SILENT
        _SILENT = SILENT
    return _SILENT


def _hex(s, default=(255, 255, 255)):
    try:
        s = (s or "").lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def _lerp(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)


def _hsv(h, s=1.0, v=1.0):
    h = (h % 1.0) * 6.0
    i = int(h)
    f = h - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r, g, b = ((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q))[i % 6]
    return (r * 255, g * 255, b * 255)


# ---- palettes: lists of (position 0..1, color) stops, turned into 256-entry tables ----
_STOPS = {
    "rainbow": None,   # computed from HSV
    "party": [(0, (90, 0, 255)), (.2, (180, 0, 180)), (.4, (255, 0, 80)), (.55, (255, 120, 0)),
              (.7, (255, 220, 0)), (.85, (0, 200, 120)), (1, (90, 0, 255))],
    "fire": [(0, (0, 0, 0)), (.3, (160, 0, 0)), (.6, (255, 90, 0)), (.85, (255, 200, 40)), (1, (255, 255, 200))],
    "lava": [(0, (20, 0, 0)), (.35, (180, 0, 0)), (.6, (255, 60, 0)), (.8, (255, 160, 0)), (1, (255, 230, 120))],
    "ocean": [(0, (0, 10, 40)), (.35, (0, 60, 140)), (.65, (0, 150, 200)), (.85, (60, 220, 230)), (1, (200, 255, 255))],
    "forest": [(0, (0, 30, 0)), (.4, (20, 120, 20)), (.7, (90, 180, 30)), (1, (200, 230, 80))],
    "sunset": [(0, (40, 0, 80)), (.35, (180, 0, 90)), (.6, (255, 70, 30)), (.85, (255, 170, 0)), (1, (255, 230, 120))],
    "ice": [(0, (0, 0, 40)), (.4, (0, 80, 200)), (.75, (120, 200, 255)), (1, (255, 255, 255))],
    "candy": [(0, (255, 40, 120)), (.33, (255, 255, 255)), (.66, (60, 200, 255)), (1, (255, 40, 120))],
    "retro": [(0, (255, 0, 255)), (.33, (0, 255, 255)), (.66, (255, 255, 0)), (1, (255, 0, 255))],
}
PALETTES = ["rainbow", "album", "colors", "party", "fire", "lava", "ocean", "forest", "sunset", "ice", "candy", "retro"]
PALETTE_NAMES = {"rainbow": "Rainbow", "album": "Album art colors", "colors": "My colors", "party": "Party", "fire": "Fire", "lava": "Lava",
                 "ocean": "Ocean", "forest": "Forest", "sunset": "Sunset", "ice": "Ice", "candy": "Candy",
                 "retro": "Retro neon"}


def album_colors():
    a = ALBUM
    return list(a["colors"]) if a and a.get("colors") else None


def palette_table(name, c1, c2, c3):
    if name == "album":
        cols = album_colors()
        if not cols:
            return [_hsv(i / 256) for i in range(256)]     # no cover yet: rainbow
        cols = cols + [cols[0]]                             # loop back smoothly
        stops = [(i / (len(cols) - 1), c) for i, c in enumerate(cols)]
    elif name == "rainbow" or name not in _STOPS and name != "colors":
        return [_hsv(i / 256) for i in range(256)]
    else:
        stops = _STOPS.get(name) if name != "colors" else [(0, c1), (.5, c2), (1, c3)]
    out = []
    for i in range(256):
        x = i / 255
        for (p0, a), (p1, b) in zip(stops, stops[1:]):
            if p0 <= x <= p1:
                out.append(_lerp(a, b, (x - p0) / (p1 - p0 or 1)))
                break
        else:
            out.append(stops[-1][1])
    return out


# ---- base ----
class Effect:
    label = "Effect"
    hint = ""
    palette = "rainbow"          # default palette
    music = False                # reacts to the music

    def __init__(self, w, h, p):
        self.w, self.h = w, h
        self.p = p
        self.speed = p["speed"] / 128.0          # 0..2
        self.inten = p["intensity"] / 255.0      # 0..1
        self.c1, self.c2, self.c3 = p["c1"], p["c2"], p["c3"]
        # album mode: palette and colors follow the current album cover
        self.album = bool(p.get("album")) or p["palette"] == "album"
        self._album_ver = object()
        self.pal = palette_table(p["palette"], self.c1, self.c2, self.c3)
        self._sync_album()
        self.buf = [[0.0, 0.0, 0.0] for _ in range(w * h)]
        self.rng = random.Random(1234)
        self.last = None
        self.setup()

    def setup(self):
        pass

    def _sync_album(self):
        if not self.album:
            return
        a = ALBUM
        ver = a.get("version") if a else None
        if ver == self._album_ver:
            return
        self._album_ver = ver
        cols = album_colors()
        if cols:
            cc = (cols * 3)[:3]
            self.c1, self.c2, self.c3 = cc[0], cc[1], cc[2]
            self.pal = palette_table("album", self.c1, self.c2, self.c3)
        else:
            self.c1, self.c2, self.c3 = self.p["c1"], self.p["c2"], self.p["c3"]
            pal = self.p["palette"] if self.p["palette"] != "album" else "rainbow"
            self.pal = palette_table(pal, self.c1, self.c2, self.c3)

    def color(self, x):      # x 0..1 (wraps) -> palette color
        return self.pal[int((x % 1.0) * 255.999)]

    def fade(self, f):
        for c in self.buf:
            c[0] *= f
            c[1] *= f
            c[2] *= f

    def add(self, x, y, col, a=1.0):
        x, y = int(x), int(y)
        if 0 <= x < self.w and 0 <= y < self.h:
            c = self.buf[y * self.w + x]
            c[0] = min(255.0, c[0] + col[0] * a)
            c[1] = min(255.0, c[1] + col[1] * a)
            c[2] = min(255.0, c[2] + col[2] * a)

    def put(self, x, y, col):
        x, y = int(x), int(y)
        if 0 <= x < self.w and 0 <= y < self.h:
            self.buf[y * self.w + x] = [col[0], col[1], col[2]]

    def step(self, t, dt):      # update self.buf
        raise NotImplementedError

    def frame(self, t):
        self._sync_album()       # new song -> new colors, without restarting the effect
        dt = 0.025 if self.last is None else max(0.0, min(0.2, t - self.last))
        self.last = t
        self.step(t * self.speed, dt * self.speed)
        out = bytearray(self.w * self.h * 3)
        i = 0
        for c in self.buf:
            out[i] = int(c[0]) if c[0] < 255 else 255
            out[i + 1] = int(c[1]) if c[1] < 255 else 255
            out[i + 2] = int(c[2]) if c[2] < 255 else 255
            i += 3
        return Image.frombytes("RGB", (self.w, self.h), bytes(out))


# ---- effects ----
class Solid(Effect):
    label = "Solid"
    palette = "colors"

    def step(self, t, dt):
        for c in self.buf:
            c[:] = self.c1


class Rainbow(Effect):
    label = "Rainbow"
    hint = "Intensity: how many rainbows fit across"

    def step(self, t, dt):
        n = 0.3 + self.inten * 3
        for y in range(self.h):
            for x in range(self.w):
                self.buf[y * self.w + x] = list(self.color(x / self.w * n - t * 0.25 + y * 0.01))


class Soap(Effect):
    label = "Soap"
    hint = "Smooth colors flowing like a soap film. Intensity: swirl detail"

    def setup(self):
        self.grid = [self.rng.random() for _ in range(64 * 64)]

    def _noise(self, x, y):
        xi, yi = int(math.floor(x)), int(math.floor(y))
        xf, yf = x - xi, y - yi
        g = self.grid

        def v(a, b):
            return g[(a & 63) + ((b & 63) << 6)]
        u = xf * xf * (3 - 2 * xf)
        w = yf * yf * (3 - 2 * yf)
        top = v(xi, yi) + (v(xi + 1, yi) - v(xi, yi)) * u
        bot = v(xi, yi + 1) + (v(xi + 1, yi + 1) - v(xi, yi + 1)) * u
        return top + (bot - top) * w

    def step(self, t, dt):
        sc = (0.06 + self.inten * 0.14)
        a, b = t * 0.35, t * 0.22
        for y in range(self.h):
            for x in range(self.w):
                n = self._noise(x * sc + a, y * sc - b) * 0.65 + self._noise(x * sc * 2.1 - b, y * sc * 2.1 + a) * 0.35
                self.buf[y * self.w + x] = list(self.color(n * 1.6 + t * 0.05))


class Plasma(Effect):
    label = "Plasma"
    hint = "Intensity: pattern size"

    def step(self, t, dt):
        k = 0.15 + self.inten * 0.45
        cx, cy = self.w / 2 + math.sin(t * 0.7) * self.w / 3, self.h / 2 + math.cos(t * 0.5) * self.h / 3
        for y in range(self.h):
            for x in range(self.w):
                v = (math.sin(x * k + t) + math.sin((y * k + t) * 0.8)
                     + math.sin(math.hypot(x - cx, y - cy) * k * 1.3 - t * 1.4)) / 3
                self.buf[y * self.w + x] = list(self.color(v * 0.5 + 0.5 + t * 0.03))


class BouncingBalls(Effect):
    label = "Bouncing balls"
    hint = "Intensity: number of balls"

    def setup(self):
        n = 1 + int(self.inten * 7)
        self.balls = []
        for i in range(n):
            self.balls.append([self.rng.uniform(0, self.w - 1), self.rng.uniform(0, self.h / 2),
                               self.rng.uniform(-6, 6), 0.0, self.rng.uniform(0.8, 1.6), i / n])

    def step(self, t, dt):
        self.fade(0.55)
        g = self.h * 3.5
        for b in self.balls:
            b[3] += g * dt
            b[0] += b[2] * dt
            b[1] += b[3] * dt
            if b[1] >= self.h - 1:
                b[1] = self.h - 1
                b[3] = -math.sqrt(2 * g * self.h * 0.9) * b[4] / 1.6 - self.rng.uniform(0, 2)
            if b[0] < 0 or b[0] > self.w - 1:
                b[2] = -b[2]
                b[0] = max(0, min(self.w - 1, b[0]))
            col = self.color(b[5])
            r = max(0.5, min(self.w, self.h) / 16)
            for dy in range(-int(r), int(r) + 1):
                for dx in range(-int(r), int(r) + 1):
                    if dx * dx + dy * dy <= r * r + 0.5:
                        self.put(b[0] + dx, b[1] + dy, col)


class Fire(Effect):
    label = "Fire"
    palette = "fire"
    hint = "Intensity: flame height"

    def setup(self):
        self.heat = [0.0] * (self.w * self.h)

    def step(self, t, dt):
        w, h, heat, r = self.w, self.h, self.heat, self.rng
        # flames reach about (35% + 60% x intensity) of the height
        cool = 2.2 / max(2.0, h * (0.35 + 0.6 * self.inten))
        for x in range(w):
            heat[(h - 1) * w + x] = r.uniform(0.6, 1.0) if r.random() < 0.8 else heat[(h - 1) * w + x]
        for y in range(h - 1):
            for x in range(w):
                a = heat[(y + 1) * w + x]
                l = heat[(y + 1) * w + max(0, x - 1)]
                rr = heat[(y + 1) * w + min(w - 1, x + 1)]
                b = heat[min(h - 1, y + 2) * w + x]
                v = (a * 2 + l + rr + b) / 5.0 - r.uniform(0, cool)
                heat[y * w + x] = v if v > 0 else 0.0
        for i, v in enumerate(heat):
            self.buf[i] = list(self.color(min(1.0, v) * 0.99))


class Matrix(Effect):
    label = "Matrix"
    palette = "colors"
    hint = "Color 1: rain color. Intensity: how much rain"

    def setup(self):
        self.drops = []

    def step(self, t, dt):
        self.fade(0.86)
        if self.rng.random() < 0.15 + self.inten * 0.8:
            self.drops.append([self.rng.randrange(self.w), -1.0, self.rng.uniform(8, 18)])
        keep = []
        for d in self.drops:
            d[1] += d[2] * dt
            if d[1] < self.h + 1:
                keep.append(d)
                self.put(d[0], d[1], (220, 255, 220))
                self.add(d[0], d[1] - 1, self.c1, 1.0)
        self.drops = keep


class Starfield(Effect):
    label = "Starfield"
    palette = "ice"
    hint = "Warp speed starfield. Intensity: number of stars"

    def setup(self):
        self.stars = [self._new(True) for _ in range(10 + int(self.inten * 70))]

    def _new(self, anyz=False):
        return [self.rng.uniform(-1, 1), self.rng.uniform(-1, 1), self.rng.uniform(0.1, 1) if anyz else 1.0]

    def step(self, t, dt):
        self.fade(0.5)
        cx, cy = self.w / 2, self.h / 2
        for s in self.stars:
            s[2] -= dt * 0.5
            if s[2] <= 0.02:
                s[:] = self._new()
            x, y = cx + s[0] / s[2] * cx, cy + s[1] / s[2] * cy
            if not (0 <= x < self.w and 0 <= y < self.h):
                s[:] = self._new()
                continue
            b = min(1.0, (1 - s[2]) * 1.5)
            self.add(x, y, self.color(1 - s[2]), b)


class Fireworks(Effect):
    label = "Fireworks"
    palette = "party"
    hint = "Intensity: how often rockets go up"

    def setup(self):
        self.parts = []
        self.rockets = []

    def step(self, t, dt):
        self.fade(0.88)
        if self.rng.random() < 0.01 + self.inten * 0.06:
            self.rockets.append([self.rng.uniform(self.w * .2, self.w * .8), self.h - 1.0,
                                 self.rng.uniform(-2, 2), -self.rng.uniform(self.h * .9, self.h * 1.3), self.rng.random()])
        g = self.h * 0.9
        keep = []
        for r in self.rockets:
            r[3] += g * dt
            r[0] += r[2] * dt
            r[1] += r[3] * dt
            if r[3] >= -self.h * 0.1:
                n = 18 + int(self.rng.random() * 18)
                for k in range(n):
                    a = k / n * math.tau
                    sp = self.rng.uniform(self.h * .25, self.h * .5)
                    self.parts.append([r[0], r[1], math.cos(a) * sp, math.sin(a) * sp, 1.0, r[4] + self.rng.uniform(-.05, .05)])
            else:
                keep.append(r)
                self.put(r[0], r[1], (255, 220, 150))
        self.rockets = keep
        keep = []
        for p in self.parts:
            p[3] += g * 0.4 * dt
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[4] -= dt * 0.7
            if p[4] > 0:
                keep.append(p)
                self.add(p[0], p[1], self.color(p[5]), p[4])
        self.parts = keep


class Twinkle(Effect):
    label = "Twinkle"
    palette = "party"
    hint = "Intensity: how many twinkles"

    def step(self, t, dt):
        self.fade(0.9)
        for _ in range(int(1 + self.inten * self.w * self.h / 40)):
            if self.rng.random() < 0.6:
                self.add(self.rng.randrange(self.w), self.rng.randrange(self.h), self.color(self.rng.random()), 1.0)


class Confetti(Effect):
    label = "Confetti"
    palette = "party"

    def step(self, t, dt):
        self.fade(0.94)
        for _ in range(int(1 + self.inten * 4)):
            self.put(self.rng.randrange(self.w), self.rng.randrange(self.h), self.color(t * 0.1 + self.rng.random() * 0.3))


class Lava(Effect):
    label = "Lava lamp"
    palette = "lava"
    hint = "Soft blobs that merge and split. Intensity: number of blobs"

    def setup(self):
        self.blobs = [[self.rng.uniform(0, 1), self.rng.uniform(0, 1), self.rng.uniform(.6, 1.4), self.rng.uniform(0, 6)]
                      for _ in range(3 + int(self.inten * 5))]

    def step(self, t, dt):
        w, h = self.w, self.h
        rr = (min(w, h) * 0.28) ** 2
        pos = [(w * (0.5 + 0.42 * math.sin(t * 0.3 * b[2] + b[3])), h * (0.5 + 0.45 * math.sin(t * 0.21 * b[2] + b[3] * 1.7)))
               for b in self.blobs]
        for y in range(h):
            for x in range(w):
                v = 0.0
                for (bx, by) in pos:
                    d = (x - bx) ** 2 + (y - by) ** 2 + 1
                    v += rr / d
                v = min(1.0, v * 0.35)
                self.buf[y * w + x] = list(self.color(v * 0.99))


class Life(Effect):
    label = "Game of Life"
    palette = "retro"
    hint = "Conway's Game of Life; starts over when it gets stuck. Intensity: starting density"

    def setup(self):
        self._seed()
        self.acc = 0.0

    def _seed(self):
        d = 0.2 + self.inten * 0.3
        self.cells = [1 if self.rng.random() < d else 0 for _ in range(self.w * self.h)]
        self.hist = []
        self.gen = 0

    def step(self, t, dt):
        self.acc += dt
        if self.acc >= 0.18:
            self.acc = 0
            w, h, c = self.w, self.h, self.cells
            new = [0] * (w * h)
            for y in range(h):
                for x in range(w):
                    n = 0
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            if dx or dy:
                                n += c[((y + dy) % h) * w + (x + dx) % w]
                    new[y * w + x] = 1 if n == 3 or (n == 2 and c[y * w + x]) else 0
            key = hash(tuple(new))
            self.gen += 1
            if key in self.hist or self.gen > 400:
                self._seed()
            else:
                self.hist = (self.hist + [key])[-6:]
                self.cells = new
        self.fade(0.6)
        for i, v in enumerate(self.cells):
            if v:
                y = i // self.w
                self.buf[i] = list(self.color(y / self.h * 0.5 + t * 0.02))


class Ripple(Effect):
    def _want_drop(self):
        return self.rng.random() < 0.03 + self.inten * 0.12

    label = "Ripple"
    palette = "ocean"
    hint = "Raindrops making rings. Intensity: how many drops"

    def setup(self):
        self.drops = []

    def step(self, t, dt):
        self.fade(0.7)
        if self._want_drop():
            self.drops.append([self.rng.uniform(0, self.w), self.rng.uniform(0, self.h), 0.0, self.rng.random()])
        keep = []
        for d in self.drops:
            d[2] += dt * max(self.w, self.h) * 0.45
            life = 1 - d[2] / (max(self.w, self.h) * 0.7)
            if life <= 0:
                continue
            keep.append(d)
            n = max(12, int(d[2] * 7))
            for k in range(n):
                a = k / n * math.tau
                self.add(d[0] + math.cos(a) * d[2], d[1] + math.sin(a) * d[2], self.color(d[3]), life)
        self.drops = keep


class Snow(Effect):
    label = "Snow"
    palette = "ice"
    hint = "Intensity: how hard it snows"

    def setup(self):
        self.flakes = []

    def step(self, t, dt):
        self.fade(0.3)
        if self.rng.random() < 0.1 + self.inten * 0.9:
            self.flakes.append([self.rng.uniform(0, self.w), -1.0, self.rng.uniform(3, 7), self.rng.uniform(0, 6)])
        keep = []
        for f in self.flakes:
            f[1] += f[2] * dt
            x = f[0] + math.sin(t * 1.5 + f[3]) * 1.2
            if f[1] < self.h:
                keep.append(f)
                self.add(x, f[1], self.color(0.6 + f[2] / 15), 1)
        self.flakes = keep


class Aurora(Effect):
    label = "Aurora"
    palette = "forest"
    hint = "Northern-lights curtains. Intensity: brightness of the bands"

    def step(self, t, dt):
        w, h = self.w, self.h
        for x in range(w):
            c1 = h * (0.45 + 0.25 * math.sin(x * 0.17 + t * 0.6) + 0.1 * math.sin(x * 0.41 - t))
            c2 = h * (0.35 + 0.2 * math.sin(x * 0.11 - t * 0.4 + 2))
            for y in range(h):
                a = math.exp(-((y - c1) / (h * 0.12)) ** 2) + 0.7 * math.exp(-((y - c2) / (h * 0.1)) ** 2)
                a = min(1.0, a * (0.5 + self.inten))
                col = self.color(0.3 + 0.6 * (y / h) + 0.1 * math.sin(t + x * 0.1))
                self.buf[y * w + x] = [col[0] * a, col[1] * a, col[2] * a]


class Scanner(Effect):
    label = "Scanner"
    palette = "colors"
    hint = "A light sweeping side to side, like a robot car. Color 1: light"

    def step(self, t, dt):
        self.fade(0.75 - self.inten * 0.4)
        x = (math.sin(t * 2.2) * 0.5 + 0.5) * (self.w - 1)
        for y in range(self.h):
            self.put(x, y, self.c1)
            self.add(x + 1, y, self.c1, 0.4)
            self.add(x - 1, y, self.c1, 0.4)


class Waves(Effect):
    label = "Ocean waves"
    palette = "ocean"
    hint = "Layered waves washing across. Intensity: wave height"

    def step(self, t, dt):
        w, h = self.w, self.h
        amp = 0.1 + self.inten * 0.3
        for x in range(w):
            for y in range(h):
                v = 0.0
                for k, (sp, fr, ph) in enumerate(((0.7, 0.21, 0), (-0.5, 0.33, 2), (0.9, 0.13, 4))):
                    sy = h * (0.55 + amp * math.sin(x * fr + t * sp + ph) - k * 0.12)
                    v += 1 / (1 + math.exp((sy - y) * 0.9))
                self.buf[y * w + x] = list(self.color(min(0.99, v / 3)))


class Swirl(Effect):
    label = "Swirl"
    hint = "A spinning spiral. Intensity: how tight the spiral is"

    def step(self, t, dt):
        w, h = self.w, self.h
        cx, cy = (w - 1) / 2, (h - 1) / 2
        k = 0.05 + self.inten * 0.3
        for y in range(h):
            for x in range(w):
                a = math.atan2(y - cy, x - cx) / math.tau
                r = math.hypot(x - cx, y - cy)
                self.buf[y * w + x] = list(self.color(a + r * k - t * 0.3))


class Rain(Effect):
    label = "Colored rain"
    palette = "party"
    hint = "Intensity: how hard it rains"

    def setup(self):
        self.drops = []

    def step(self, t, dt):
        self.fade(0.7)
        for _ in range(1 + int(self.inten * 3)):
            if self.rng.random() < 0.5:
                self.drops.append([self.rng.randrange(self.w), -1.0, self.rng.uniform(12, 24), self.rng.random()])
        keep = []
        for d in self.drops:
            d[1] += d[2] * dt
            if d[1] < self.h:
                keep.append(d)
                self.put(d[0], d[1], self.color(d[3]))
        self.drops = keep


class Pulse(Effect):
    label = "Breathe"
    palette = "colors"
    hint = "The whole sign gently breathing between colors"

    def step(self, t, dt):
        f = 0.5 - 0.5 * math.cos(t * 1.4)
        col = _lerp(self.c1, self.c2, f)
        k = 0.25 + 0.75 * (1 - self.inten * (1 - f))
        for c in self.buf:
            c[:] = [col[0] * k, col[1] * k, col[2] * k]


# ---- music-reactive effects (use the audio the relay passes through) ----
class VUMeter(Effect):
    label = "VU meter"
    music = True
    palette = "colors"
    hint = "Left and right loudness with peak hold, green to red. Intensity: peak hold time"

    def setup(self):
        self.peaks = [0.0, 0.0]
        self.hold = [0.0, 0.0]

    def step(self, t, dt):
        a = audio_now()
        w, h = self.w, self.h
        vertical = h >= w / 2.5
        n = h if vertical else w
        for c in self.buf:
            c[:] = [0.0, 0.0, 0.0]
        for ch, lvl in enumerate((a["level_l"], a["level_r"])):
            if lvl >= self.peaks[ch]:
                self.peaks[ch], self.hold[ch] = lvl, 0.3 + self.inten * 1.5
            else:
                self.hold[ch] -= dt
                if self.hold[ch] <= 0:
                    self.peaks[ch] = max(0.0, self.peaks[ch] - dt * 0.6)
            lit = int(round(lvl * n))
            pk = min(n - 1, int(self.peaks[ch] * n))
            for i in range(n):
                f = i / max(1, n - 1)
                if self.album and album_colors():
                    col = self.color(f * 0.75)
                else:
                    col = (0, 220, 60) if f < 0.6 else (255, 190, 0) if f < 0.85 else (255, 30, 20)
                on = i < lit or (i == pk and self.peaks[ch] > 0.02)
                if not on:
                    col = (col[0] * 0.06, col[1] * 0.06, col[2] * 0.06)
                if vertical:      # two columns side by side, bottom up
                    x0, x1 = (1, w // 2 - 1) if ch == 0 else (w // 2 + 1, w - 1)
                    for x in range(x0, x1):
                        self.put(x, h - 1 - i, col)
                else:             # two rows, left to right
                    y0, y1 = (0, h // 2) if ch == 0 else (h // 2, h)
                    for y in range(y0, y1):
                        self.put(i, y, col)


class Spectrum(Effect):
    label = "Spectrum"
    music = True
    hint = "Bars for bass (left) to treble (right), with falling peaks. Intensity: mirror the bars from the middle"

    def setup(self):
        self.peak = [0.0] * 16

    def step(self, t, dt):
        a = audio_now()
        w, h = self.w, self.h
        self.fade(0.5)
        mirror = self.inten > 0.5
        bands = a["bands"]
        for x in range(w):
            b = min(15, int(x / w * 16))
            v = bands[b]
            self.peak[b] = max(v, self.peak[b] - dt * 0.5)
            col = self.color(b / 16)
            if mirror:
                half = v * h / 2
                for y in range(h):
                    if abs(y - (h - 1) / 2) <= half:
                        self.put(x, y, col)
            else:
                top = h - int(round(v * h))
                for y in range(top, h):
                    self.put(x, y, col)
                py = h - 1 - min(h - 1, int(self.peak[b] * h))
                self.put(x, py, (255, 255, 255))


class Scope(Effect):
    label = "Oscilloscope"
    music = True
    palette = "colors"
    hint = "The waveform, like a scope. Color 1: trace. Intensity: trail length"

    def step(self, t, dt):
        a = audio_now()
        w, h = self.w, self.h
        self.fade(0.35 + self.inten * 0.55)
        wave = a["wave"]
        prev = None
        for x in range(w):
            v = wave[int(x / w * len(wave))]
            y = (h - 1) / 2 - v * (h - 1) / 2
            if prev is not None:
                lo, hi = sorted((prev, y))
                for yy in range(int(lo), int(hi) + 1):
                    self.add(x, yy, self.c1, 0.7)
            self.put(x, y, self.c1)
            prev = y


class Ambience(Effect):
    label = "Ambience"
    music = True
    palette = "retro"
    hint = "Psychedelic swirling colors that pulse and twist with the music (90s media player style). Intensity: how hard it reacts"

    def setup(self):
        self.ph = 0.0
        self.rot = 0.0
        self.hue = 0.0

    def step(self, t, dt):
        a = audio_now()
        k = 0.4 + self.inten * 1.6
        self.ph += dt * (0.6 + a["level"] * 3 * k)
        self.rot += dt * (0.2 + a["bass"] * 1.5 * k)
        if a["beat"]:
            self.hue += 0.13 * k
        w, h = self.w, self.h
        cx, cy = (w - 1) / 2, (h - 1) / 2
        zoom = (0.18 + 0.12 * a["bass"] * k) * 32 / max(w, h)
        cr, sr = math.cos(self.rot), math.sin(self.rot)
        for y in range(h):
            for x in range(w):
                dx, dy = (x - cx) * zoom, (y - cy) * zoom
                u, v = dx * cr - dy * sr, dx * sr + dy * cr
                r = math.hypot(u, v)
                val = (math.sin(u * 2.3 + self.ph) + math.sin(v * 2.9 - self.ph * 0.7)
                       + math.sin(r * 3.1 - self.ph * 1.3) + math.sin((u + v) * 1.7 + self.ph * 0.5)) / 4
                bright = 0.55 + 0.45 * min(1.0, a["level"] * 1.5) if a["live"] else 0.6
                col = self.color(val * 0.5 + 0.5 + self.hue)
                self.buf[y * w + x] = [col[0] * bright, col[1] * bright, col[2] * bright]


class Tunnel(Effect):
    label = "Beat tunnel"
    music = True
    palette = "party"
    hint = "Rings fly out of the middle on every beat. Intensity: ring speed"

    def setup(self):
        self.rings = []
        self.idle = 0.0

    def step(self, t, dt):
        a = audio_now()
        w, h = self.w, self.h
        self.fade(0.72)
        self.idle += dt
        if a["beat"] or (not a["live"] and self.idle > 1.2):
            self.rings.append([0.0, self.rng.random(), 1.0])
            self.idle = 0.0
        if a["live"] and a["level"] > 0.15 and self.rng.random() < a["treble"] * 0.3:
            self.rings.append([0.0, self.rng.random(), 0.45])
        cx, cy = (w - 1) / 2, (h - 1) / 2
        maxr = math.hypot(cx, cy) + 1
        sp = (0.5 + self.inten * 1.5) * maxr
        keep = []
        for r in self.rings:
            r[0] += dt * sp * (0.6 + a["level"])
            if r[0] > maxr:
                continue
            keep.append(r)
            n = max(8, int(r[0] * 7))
            col = self.color(r[1])
            for k in range(n):
                ang = k / n * math.tau
                self.add(cx + math.cos(ang) * r[0], cy + math.sin(ang) * r[0], col, r[2])
        self.rings = keep


class Kaleido(Effect):
    label = "Kaleidoscope"
    music = True
    palette = "candy"
    hint = "The spectrum folded into a turning six-way mirror. Intensity: turn speed"

    def setup(self):
        self.rot = 0.0

    def step(self, t, dt):
        a = audio_now()
        w, h = self.w, self.h
        self.rot += dt * (0.1 + self.inten * 0.8 + a["bass"] * 0.6)
        cx, cy = (w - 1) / 2, (h - 1) / 2
        maxr = math.hypot(cx, cy)
        bands = a["bands"] if a["live"] else [0.25 + 0.2 * math.sin(t + i) for i in range(16)]
        for y in range(h):
            for x in range(w):
                ang = (math.atan2(y - cy, x - cx) + self.rot) % (math.tau / 6)
                ang = min(ang, math.tau / 6 - ang) / (math.tau / 12)        # 0..1, mirrored
                r = math.hypot(x - cx, y - cy) / maxr
                b = min(15, int((r * 0.7 + ang * 0.3) * 16))
                v = bands[b]
                col = self.color(r * 0.6 + ang * 0.4 + t * 0.03)
                k = min(1.0, v * 1.4) ** 1.3
                self.buf[y * w + x] = [col[0] * k, col[1] * k, col[2] * k]


class BeatBurst(Effect):
    label = "Beat bursts"
    music = True
    palette = "party"
    hint = "A burst of sparks on every beat; sparkle follows the treble. Intensity: burst size"

    def setup(self):
        self.parts = []

    def step(self, t, dt):
        a = audio_now()
        w, h = self.w, self.h
        self.fade(0.8)
        if a["beat"] or (not a["live"] and self.rng.random() < 0.02):
            cx, cy = self.rng.uniform(w * .25, w * .75), self.rng.uniform(h * .25, h * .75)
            hue = self.rng.random()
            for _ in range(int(10 + self.inten * 40)):
                ang = self.rng.uniform(0, math.tau)
                sp = self.rng.uniform(0.3, 1.0) * max(w, h) * 0.8
                self.parts.append([cx, cy, math.cos(ang) * sp, math.sin(ang) * sp, 1.0, hue + self.rng.uniform(-.08, .08)])
        for _ in range(int(a["treble"] * 6)):
            self.add(self.rng.randrange(w), self.rng.randrange(h), self.color(self.rng.random()), 0.7)
        keep = []
        for p in self.parts:
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[2] *= 0.94
            p[3] *= 0.94
            p[4] -= dt * 1.2
            if p[4] > 0:
                keep.append(p)
                self.add(p[0], p[1], self.color(p[5]), p[4])
        self.parts = keep



class Waterfall(Effect):
    label = "Waterfall"
    music = True
    palette = "ocean"
    hint = "A scrolling spectrogram: new sound appears at the top and falls. Intensity: fall speed"

    def setup(self):
        self.acc = 0.0

    def step(self, t, dt):
        a = audio_now()
        w, h = self.w, self.h
        self.acc += dt * (8 + self.inten * 40)
        while self.acc >= 1:
            self.acc -= 1
            self.buf[w:] = self.buf[:-w]          # shift everything down a row
            self.buf[:w] = [[0.0, 0.0, 0.0] for _ in range(w)]
            for x in range(w):
                v = a["bands"][min(15, int(x / w * 16))]
                col = self.color(0.15 + v * 0.8)
                k = min(1.0, v * 1.3)
                self.buf[x] = [col[0] * k, col[1] * k, col[2] * k]


class CircleSpectrum(Effect):
    label = "Circle spectrum"
    music = True
    palette = "party"
    hint = "Spectrum bars arranged in a ring around the middle, slowly turning. Intensity: spin speed"

    def setup(self):
        self.rot = 0.0

    def step(self, t, dt):
        a = audio_now()
        w, h = self.w, self.h
        self.fade(0.55)
        self.rot += dt * (0.1 + self.inten * 1.2)
        cx, cy = (w - 1) / 2, (h - 1) / 2
        r0 = min(w, h) * 0.18
        rmax = min(w, h) * 0.5
        spokes = 32
        for k in range(spokes):
            b = k % 16 if k < 16 else 15 - (k % 16)     # mirror so the ring joins up
            v = a["bands"][b]
            ang = self.rot + k / spokes * math.tau
            col = self.color(k / spokes)
            length = r0 + v * (rmax - r0)
            r = r0
            while r <= length:
                self.put(cx + math.cos(ang) * r, cy + math.sin(ang) * r, col)
                r += 0.7
        glow = a["bass"]
        for y in range(h):
            for x in range(w):
                d = math.hypot(x - cx, y - cy)
                if d < r0 * 0.9:
                    c = self.color(t * 0.05)
                    self.add(x, y, c, glow * (1 - d / r0) * 0.8)


class BassPulse(Effect):
    label = "Bass pulse"
    music = True
    palette = "lava"
    hint = "A glowing heart in the middle that swells with the bass. Intensity: size"

    def setup(self):
        self.lvl = 0.0

    def step(self, t, dt):
        a = audio_now()
        target = a["bass"] if a["live"] else 0.3 + 0.2 * math.sin(t * 2)
        self.lvl += (target - self.lvl) * min(1.0, dt * (18 if target > self.lvl else 5))
        w, h = self.w, self.h
        cx, cy = (w - 1) / 2, (h - 1) / 2
        R = (0.25 + self.inten * 0.55) * math.hypot(cx, cy) * (0.35 + 0.65 * self.lvl)
        for y in range(h):
            for x in range(w):
                d = math.hypot(x - cx, y - cy) / max(0.5, R)
                k = max(0.0, 1 - d) ** 1.5
                col = self.color(0.2 + 0.7 * k + a["treble"] * 0.2)
                self.buf[y * w + x] = [col[0] * k, col[1] * k, col[2] * k]


class MusicFire(Fire):
    label = "Music fire"
    music = True
    hint = "Fire that roars with the music and flares on every beat. Intensity: flame height"

    def step(self, t, dt):
        a = audio_now()
        base = self.inten
        lvl = a["level"] if a["live"] else 0.4
        self.inten = min(1.0, 0.15 + base * 0.5 + lvl * 0.7 + (0.3 if a["beat"] else 0.0))
        try:
            super().step(t, dt)
        finally:
            self.inten = base


class Warp(Starfield):
    label = "Music warp"
    music = True
    hint = "A starfield that speeds up with the music and jumps on beats. Intensity: number of stars"

    def step(self, t, dt):
        a = audio_now()
        boost = 0.4 + a["level"] * 3 + (2.5 if a["beat"] else 0) if a["live"] else 1.0
        super().step(t, dt * boost)


class BeatRipples(Ripple):
    label = "Beat ripples"
    music = True
    hint = "A ripple drops on every beat, bigger for louder bass. Intensity: extra ripples from the treble"

    def _want_drop(self):
        a = self._a
        if not a["live"]:
            return super()._want_drop()       # no music: gentle random rain
        return a["beat"] or self.rng.random() < a["treble"] * self.inten * 0.15

    def step(self, t, dt):
        self._a = audio_now()
        super().step(t, dt * (0.8 + self._a["bass"]))


EFFECTS = {
    "soap": Soap, "bouncing": BouncingBalls, "fire": Fire, "plasma": Plasma, "rainbow": Rainbow,
    "matrix": Matrix, "starfield": Starfield, "fireworks": Fireworks, "lava": Lava, "life": Life,
    "ripple": Ripple, "twinkle": Twinkle, "confetti": Confetti, "snow": Snow, "aurora": Aurora,
    "waves": Waves, "swirl": Swirl, "rain": Rain, "scanner": Scanner, "breathe": Pulse, "solid": Solid,
    # music-reactive
    "vu": VUMeter, "spectrum": Spectrum, "scope": Scope, "ambience": Ambience, "tunnel": Tunnel,
    "kaleido": Kaleido, "beatburst": BeatBurst, "waterfall": Waterfall, "circle": CircleSpectrum,
    "basspulse": BassPulse, "musicfire": MusicFire, "warp": Warp, "beatripples": BeatRipples,
}


class SyncFlashScene:
    """Sync test: a white square whenever the (delayed) audio has a tick, a dim
    outline otherwise, so you can line the flash up with the tick you hear."""

    def __init__(self, w, h):
        self.w, self.h = w, h

    def render(self, now):
        from PIL import ImageDraw
        a = audio_now()
        img = Image.new("RGB", (self.w, self.h))
        d = ImageDraw.Draw(img)
        m = max(1, min(self.w, self.h) // 5)
        box = [m, m if self.h > 8 else 0, self.w - 1 - m, self.h - 1 - (m if self.h > 8 else 0)]
        if a["live"] and a.get("raw", 0) > 0.008:      # the tick, at any gain
            d.rectangle(box, fill=(255, 255, 255))
        else:
            d.rectangle(box, outline=(60, 60, 70))
        return img


def normalize(p: dict) -> dict:
    p = dict(p or {})
    fx = p.get("fx") if p.get("fx") in EFFECTS else "soap"
    cls = EFFECTS[fx]

    def num(k, d):
        try:
            return max(0, min(255, int(p.get(k, d))))
        except (TypeError, ValueError):
            return d
    pal = p.get("palette")
    if pal not in PALETTES:
        pal = cls.palette
    return {"fx": fx, "speed": num("speed", 128), "intensity": num("intensity", 128), "palette": pal,
            "c1": _hex(p.get("c1"), (0, 255, 120)), "c2": _hex(p.get("c2"), (255, 0, 160)),
            "c3": _hex(p.get("c3"), (40, 120, 255)),
            "album": bool(p["album"]) if "album" in p else bool(cls.music)}


def catalog() -> dict:
    return {
        "effects": [{"id": k, "name": v.label, "hint": v.hint, "palette": v.palette, "music": v.music}
                    for k, v in EFFECTS.items()],
        "palettes": [{"id": k, "name": PALETTE_NAMES[k]} for k in PALETTES],
    }


class EffectScene:
    """Scene interface (w, h, render(now)) for one effect."""

    def __init__(self, w, h, params: dict):
        self.w, self.h = w, h
        self.params = normalize(params)
        self.fx = EFFECTS[self.params["fx"]](w, h, self.params)
        self.start = time.monotonic()

    def render(self, now):
        return self.fx.frame(now - self.start)
