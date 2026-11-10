"""Fit colors to what WLED can show steadily (Dimming "in WLED", dither firmware).

At low brightness each LED channel has only a few output levels, and with the flicker
limit WLED rounds to steps of 1/N of a level (N from /json/info "dither.levels"). Plain
rounding drops weak channels (orange turns red) and makes dim tails flicker between
levels. Here every input color is changed, before it is sent, to the nearest color WLED
can show exactly: same math as the firmware (16-bit gamma table, brightness x white
balance, the flicker-limit steps), choosing among the representable levels per channel
the combination that keeps the hue (like the "hue" dimming mode). WLED then has nothing
to dither: steady hues, no shimmer. When WLED dithers finely anyway (N >= 64) colors pass
through unchanged."""
from __future__ import annotations
import bisect


class ColorFit:
    MAX_MEMO = 60000

    def __init__(self):
        self._key = None
        self.active = False
        self._memo = {}

    def setup(self, bri16: int, gamma: float, wb, levels: int, dark: bool):
        key = (int(bri16), round(float(gamma), 3), tuple(int(w) for w in wb), int(levels), bool(dark))
        if key == self._key:
            return
        self._key = key
        self._memo = {}
        levels = max(1, min(256, int(levels)))
        self.active = levels < 64 and bri16 > 0
        if not self.active:
            return
        g = min(3.5, max(1.0, float(gamma)))
        lut_f = [65535.0 * (i / 255.0) ** g for i in range(256)]
        lut = [int(round(v)) for v in lut_f]                      # the firmware's table
        scale = [(int(bri16) * int(w) + 127) // 255 for w in wb]

        def rep(v):                                               # what the firmware shows for v (1/256 levels)
            level, frac = v >> 8, v & 0xFF
            if level == 0 and not dark:
                return 256 if frac >= 128 else 0
            k = (frac * levels + 128) >> 8
            if k >= levels:
                return min(level + 1, 255) * 256
            return level * 256 + (k << 8) // levels

        self._exact, self._reps, self._input = [], [], []
        for ch in range(3):
            s = scale[ch]
            self._exact.append([lut_f[c] * s / 65536.0 for c in range(256)])   # wanted output, 1/256 levels
            by_rep = {}
            for c in range(256):
                v = (lut[c] * s) >> 16
                r = rep(v)
                if r not in by_rep or abs(v - r) < abs(((lut[by_rep[r]] * s) >> 16) - r):
                    by_rep[r] = c                                   # the input that lands on r most surely
            reps = sorted(by_rep)
            self._reps.append(reps)
            self._input.append(by_rep)
        nz = [r for reps in self._reps for r in reps if r > 0]
        self._min_nz = min(nz) if nz else 256

    def _cands(self, ch, v):
        reps = self._reps[ch]
        i = bisect.bisect_left(reps, v)
        out = set()
        if i < len(reps):
            out.add(reps[i])
        if i > 0:
            out.add(reps[i - 1])
        return out

    def fit(self, r, g, b):
        key = (r << 16) | (g << 8) | b
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        t = (self._exact[0][r], self._exact[1][g], self._exact[2][b])
        n_t = (t[0] * t[0] + t[1] * t[1] + t[2] * t[2]) ** 0.5
        res = (0, 0, 0)
        if n_t >= self._min_nz * 0.5:
            best, best_cost = None, None
            w = 6.0 * n_t * n_t
            t_max = max(t)
            presence = 0.6 * (256.0 * 256.0)          # about a 0.8-level error, in (1/256 level)^2
            for cr in self._cands(0, t[0]):
                for cg in self._cands(1, t[1]):
                    for cb in self._cands(2, t[2]):
                        n_c = (cr * cr + cg * cg + cb * cb) ** 0.5
                        if n_c == 0:
                            continue
                        lum = (cr - t[0]) ** 2 + (cg - t[1]) ** 2 + (cb - t[2]) ** 2
                        hue = ((cr / n_c - t[0] / n_t) ** 2 + (cg / n_c - t[1] / n_t) ** 2 +
                               (cb / n_c - t[2] / n_t) ** 2)
                        cost = lum + w * hue
                        # keep the color's identity: don't drop a channel it clearly has (orange keeps
                        # some green, violet some red) or add one it doesn't
                        for want, got in ((t[0], cr), (t[1], cg), (t[2], cb)):
                            if got == 0 and want >= 0.08 * t_max:
                                cost += presence
                            elif got > 0 and want < 0.02 * t_max:
                                cost += presence
                        if best_cost is None or cost < best_cost:
                            best, best_cost = (cr, cg, cb), cost
            if best is not None:
                res = (self._input[0][best[0]], self._input[1][best[1]], self._input[2][best[2]])
        if len(self._memo) > self.MAX_MEMO:
            self._memo = {}
        self._memo[key] = res
        return res
