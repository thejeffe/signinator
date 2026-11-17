"""Beat tracker: the music's tempo and where its beats fall, for the beat clock.

The old clock reacted to single bass hits and took the median gap between
them; syncopated kicks, snares and missed hits threw the tempo around (on
test songs it was on the beat ~30% of the time). This one does what beat
trackers usually do:

  1. onset strength every ~11.6 ms: spectral flux (how much the log spectrum
     rises) of the 11 kHz mono signal, 23 ms windows;
  2. tempo every 0.5 s: autocorrelation of the last 8 s of onset strength, the
     repeat interval that lines the rhythm up best (plus a little of twice that
     interval, which favours the beat over eighth notes), weighted toward usual
     dance tempos (log-normal around 120 BPM);
  3. phase: the beat grid at that tempo that covers the recent onsets best
     (comb, recent beats count more);
  4. a continuous clock: new estimates pull the grid along gently, and beat
     numbers never jump, so a GIF following it doesn't lurch. A different
     tempo has to win twice in a row before the clock switches to it.

Pure Python (no numpy in the venv): about 2-3% of one core.
"""
from __future__ import annotations
import cmath
import math
import threading
from collections import deque

RATE = 44100
DECIM = 4
SUB = 128                       # decimated samples per onset frame (11.6 ms)
WIN = 256                       # decimated samples per FFT window (23 ms)
FPS = RATE / DECIM / SUB        # onset frames per second (~86)
HISTORY_S = 8.0
MIN_BPM, MAX_BPM = 60.0, 200.0
EST_EVERY = 0.5
# tuned on synthetic songs (house, hip-hop, rock, ballad, swing, drum & bass)
GAMMA = 50.0            # log compression of the spectrum (smaller = loud hits count more)
AC2, AC4 = 0.5, 0.25    # weight of the 2-beat and 4-beat (bar) repeat in the tempo score
PH_DECAY, PH_BEATS = 0.85, 16
CONFIRM_PHASE = True    # a half-beat phase jump must be seen twice before the clock takes it
GRID_PICK = 4           # choose among this many tempo candidates by how well their grid fits
PRIOR_POW = 0.5


def _fft(x):
    n = len(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            x[i], x[j] = x[j], x[i]
    size = 2
    while size <= n:
        w = cmath.exp(-2j * math.pi / size)
        half = size // 2
        for start in range(0, n, size):
            wk = 1
            for k in range(start, start + half):
                t = wk * x[k + half]
                x[k + half] = x[k] - t
                x[k] = x[k] + t
                wk *= w
        size *= 2
    return x


_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (WIN - 1)) for i in range(WIN)]
_NORM = 32768.0 * WIN / 4
# onset weight per FFT bin (43 Hz apart): kicks and bass notes mark the beat,
# hi-hats and off-beat chops mostly don't, so low bins count more
BASS_W = 4.0
_W = [BASS_W if (b * RATE / DECIM / WIN) < 220 else (2.0 if (b * RATE / DECIM / WIN) < 500 else 1.0)
      for b in range(1, WIN // 2)]


class BeatTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._dec = []                       # decimated samples not yet used
        self._prev = None                    # previous frame's log spectrum
        self._env = deque(maxlen=int(HISTORY_S * FPS))
        self._t_last = None                  # time of the newest onset frame
        self._next_est = 0.0
        # clock: beat number n0 falls at time t0, period seconds per beat
        self._clock = None                   # (t0, n0, period)
        self._cand = None                    # tempo waiting to be confirmed
        self._pjump = None                   # phase jump waiting to be confirmed
        self._silent = 0                     # onset frames of silence in a row
        self._old = None                     # (clock, until, confidence) of the song before a silence
        self.confidence = 0.0

    # ---- onset strength ----
    def feed(self, dec, t_end):
        """dec: decimated mono samples (11 kHz) that end at time t_end."""
        frames = []
        with self._lock:
            if self._t_last is not None and t_end - self._t_last > 1.0 + len(dec) / (RATE / DECIM):
                self._reset()                 # a gap (pause, new stream)
            buf = self._dec + list(dec)
            # each frame: the WIN samples ending at its end; step SUB
            n_new = (len(buf) - WIN) // SUB + 1 if len(buf) >= WIN else 0
            for k in range(n_new):
                frames.append(buf[k * SUB:k * SUB + WIN])
            used = n_new * SUB
            self._dec = buf[used:] if n_new else buf
            remain_after = len(buf) - (used + WIN - SUB) if n_new else 0
        dt = 1.0 / (RATE / DECIM)
        for k, fr in enumerate(frames):
            # silence (between songs, paused): start fresh once it has lasted 0.7 s
            if sum(abs(v) for v in fr[::8]) / (WIN / 8) < 40.0:
                self._silent += 1
                if self._silent == int(0.7 * FPS):
                    with self._lock:
                        # the speaker still plays the old song until the silence
                        # reaches it: keep its clock up to then
                        start = (self._t_last or t_end) - 0.7
                        old = (self._clock, start, self.confidence) if self._clock else None
                        self._reset()
                        self._old = old
                    self._silent = int(0.7 * FPS) + 1
                if self._silent > 0.7 * FPS:
                    continue
            else:
                self._silent = 0
            spec = _fft([complex(fr[i] * _HANN[i], 0) for i in range(WIN)])
            logm = [math.log1p(GAMMA * abs(spec[b]) / _NORM) for b in range(1, WIN // 2)]
            prev = self._prev
            self._prev = logm
            flux = 0.0 if prev is None else sum(w * (a - b) for w, a, b in zip(_W, logm, prev) if a > b)
            # time of the window's centre
            end_index_from_last = (len(frames) - 1 - k) * SUB + remain_after
            t = t_end - end_index_from_last * dt - WIN / 2 * dt
            with self._lock:
                self._env.append(flux)
                self._t_last = t
        if self._t_last is not None and t_end >= self._next_est:
            self._next_est = t_end + EST_EVERY
            self._estimate()

    def _reset(self):
        self._dec, self._prev = [], None
        self._env.clear()
        self._clock, self._cand, self._t_last = None, None, None
        self.confidence = 0.0

    # ---- tempo and phase ----
    def _estimate(self):
        with self._lock:
            env = list(self._env)
            t_last = self._t_last
        n = len(env)
        if n < FPS * 3:
            return
        # high-pass (subtract the local mean) and keep the rises
        w = 8
        acc = sum(env[:w])
        e = []
        for i in range(n):
            lo, hi = i - w, i + w
            # running mean over a window of up to 2w+1 frames
            s, c = 0.0, 0
            for j in range(max(0, lo), min(n, hi + 1)):
                s += env[j]
                c += 1
            e.append(max(0.0, env[i] - s / c))
        tot = sum(v * v for v in e)
        if tot <= 1e-9:
            self.confidence = 0.0
            return
        lmin = int(FPS * 60 / MAX_BPM)
        lmax = int(FPS * 60 / MIN_BPM) + 1

        def ac(L):
            if L >= n - 1:
                return 0.0
            return sum(e[i] * e[i - L] for i in range(L, n)) / (n - L)

        top = (4 if AC4 else 2) * lmax + 2
        r = {L: ac(L) for L in range(lmin, min(top, n - 1))}
        base = sum(r[L] for L in range(lmin, lmax + 1) if L in r) / max(1, lmax + 1 - lmin)

        def score(L):
            v = r.get(L, 0.0) + AC2 * r.get(2 * L, 0.0) + AC4 * r.get(4 * L, 0.0)
            bpm = 60 * FPS / L
            prior = math.exp(-0.5 * (math.log2(bpm / 120.0) / 0.9) ** 2)
            return v * prior

        cands = [L for L in range(lmin, lmax + 1) if L in r]
        if not cands:
            return

        def refine(L):
            """Sub-frame lag (parabola through the neighbours)."""
            y0 = score(L - 1) if L - 1 in r else score(L)
            y1 = score(L)
            y2 = score(L + 1) if L + 1 in r else score(L)
            den = y0 - 2 * y1 + y2
            frac = 0.5 * (y0 - y2) / den if den < 0 else 0.0
            return L + max(-0.5, min(0.5, frac))

        def grid(Lf, decay, beats):
            """Best phase for a beat every Lf frames: (score, frames back from the newest)."""
            best, best_ph = -1.0, 0
            for p in range(max(1, int(Lf))):
                s = wsum = 0.0
                k = 0
                pos = n - 1 - p
                while pos >= 0 and k < beats:
                    i = int(pos)
                    f = pos - i
                    v = e[i] * (1 - f) + (e[i + 1] * f if i + 1 < n else 0.0)
                    if i - 1 >= 0:                      # an onset a frame early or late counts too
                        v = max(v, 0.7 * e[i - 1])
                    if i + 1 < n:
                        v = max(v, 0.7 * e[i + 1])
                    w = decay ** k
                    s += v * w
                    wsum += w
                    pos -= Lf
                    k += 1
                s /= wsum or 1.0
                if s > best:
                    best, best_ph = s, p
            return best, best_ph

        if GRID_PICK:
            # the strongest few repeat intervals; keep the one whose beat grid
            # lands on onsets most consistently (a 1.5-beat or off-beat pattern
            # leaves every other grid tick empty; the real beat doesn't)
            peaks = [L for L in cands if score(L) >= score(L - 1 if L - 1 in r else L)
                     and score(L) >= score(L + 1 if L + 1 in r else L)]
            peaks = sorted(peaks, key=score, reverse=True)[:GRID_PICK]
            top_s = score(peaks[0]) if peaks else 0.0
            peaks = [L for L in peaks if score(L) >= 0.35 * top_s]

            def fit(L):
                Lf = refine(L)
                bpm = 60 * FPS / Lf
                prior = math.exp(-0.5 * (math.log2(bpm / 120.0) / 0.9) ** 2)
                return grid(Lf, 1.0, 999)[0] * prior ** PRIOR_POW

            L = max(peaks, key=fit) if peaks else max(cands, key=score)
        else:
            L = max(cands, key=score)
        Lf = refine(L)
        period = Lf / FPS
        self.confidence = r[L] / base if base > 0 else 0.0
        # phase: the grid that covers the recent onsets best (recent beats count more)
        _, best_ph = grid(Lf, PH_DECAY, PH_BEATS)
        # refine the phase with a parabola too
        tb = t_last - best_ph / FPS            # time of the latest beat
        self._update_clock(tb, period)

    def _update_clock(self, tb, period):
        with self._lock:
            c = self._clock
            if c is None:
                self._clock = (tb, 0, period)
                self._cand = None
                return
            t0, n0, p0 = c
            # half or double the current tempo is the same beat (counted
            # differently): keep counting ours, only use it for the phase
            ratio = period / p0
            if abs(ratio - 2) < 0.08 or abs(ratio - 0.5) < 0.02:
                period = p0 * (ratio / round(ratio * 2) * 2 if ratio > 1 else ratio * 2)
                self._cand = None
            if abs(period / p0 - 1) > 0.04:
                # a different tempo must win twice in a row before we switch
                if self._cand and abs(self._cand / period - 1) < 0.04:
                    k = n0 + round((tb - t0) / p0)
                    self._clock = (tb, k, period)
                    self._cand = None
                else:
                    self._cand = period
                return
            self._cand = None
            k = n0 + round((tb - t0) / p0)            # which beat number tb is
            pred = t0 + (k - n0) * p0                 # where the clock thinks it is
            err = tb - pred
            if abs(err) > 0.3 * p0:
                # far off (e.g. the grid locked onto off-beats): jump straight there
                # but keep counting beats from the nearest number
                if CONFIRM_PHASE and not (self._pjump is not None and abs(self._pjump - err) < 0.15 * p0):
                    self._pjump = err
                    return
                self._pjump = None
                self._clock = (tb, k, period)
                return
            self._pjump = None
            # gentle pull: move part of the way, tempo follows slowly
            new_p = p0 + 0.35 * (period - p0)
            self._clock = (pred + 0.4 * err, k, new_p)

    # ---- the clock ----
    def clock(self, want):
        """(position_in_beats, seconds_per_beat) at time want, or None."""
        with self._lock:
            c = self._clock
            t_last = self._t_last
            old = self._old
        if old is not None and want <= old[1] and old[2] >= 1.15:
            c, t_last = old[0], old[1]        # still hearing the previous song
        elif c is None or t_last is None or want - t_last > 4.0 or self.confidence < 1.15:
            return None
        t0, n0, p = c
        return n0 + (want - t0) / p, p
