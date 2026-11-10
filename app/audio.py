"""Audio analysis for music-reactive effects.

The relay drains shairport-sync's audio pipe (raw 44.1 kHz, 16-bit stereo)
and hands every chunk to AudioAnalyzer.feed(). Every ~46 ms we compute:
  level_l / level_r  RMS loudness per channel, 0..1 (auto-gained)
  bands              16 log-spaced spectrum bands, 0..1
  bass / mid / treble
  beat               True on a detected kick/beat
  wave               64-point waveform for scopes, -1..1
Features are timestamped. Effects ask for the frame at (now - delay), so the
lights line up with the speaker, which plays the relayed audio later.
"""
from __future__ import annotations
import array
import cmath
import math
import threading
import time
from collections import deque

RATE = 44100
HOP = 2048            # samples per analysis frame (~46 ms)
DECIM = 4             # spectrum on 11 kHz audio
NFFT = 512            # 512 points at 11 kHz = HOP / DECIM
NBANDS = 16
KEEP_S = 6.0          # history kept for the visual delay


def _fft(x):
    """In-place iterative radix-2 FFT on a list of complex numbers."""
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


_WINDOW = [0.5 - 0.5 * math.cos(2 * math.pi * i / (NFFT - 1)) for i in range(NFFT)]
# band edges (FFT bin indices), log spaced from ~40 Hz to ~5.5 kHz
_BIN_HZ = RATE / DECIM / NFFT
_EDGES = [max(1, int(round(40 * (5500 / 40) ** (b / NBANDS) / _BIN_HZ))) for b in range(NBANDS + 1)]
for _b in range(1, len(_EDGES)):
    _EDGES[_b] = max(_EDGES[_b], _EDGES[_b - 1] + 1)

SILENT = {"raw": 0.0, "level_l": 0.0, "level_r": 0.0, "level": 0.0, "bands": [0.0] * NBANDS, "bass": 0.0,
          "mid": 0.0, "treble": 0.0, "beat": False, "wave": [0.0] * 64, "t": 0.0, "live": False}


class AudioAnalyzer:
    def __init__(self, get_delay=lambda: 1.5):
        self._get_delay = get_delay
        self._lock = threading.Lock()
        self._pending = array.array("h")
        self._frames = deque()
        self._gain = 1.0 / 3000          # slow auto-gain for levels
        self._band_gain = [1.0 / 20000] * NBANDS
        self._bass_avg = 0.0
        self._last_beat = 0.0
        self._beats = deque(maxlen=48)   # beat times, for tempo and the beat clock

    def feed(self, data: bytes):
        a = array.array("h")
        a.frombytes(data[: len(data) - len(data) % 4])
        now = time.time()
        with self._lock:
            self._pending.extend(a)
            hops = []
            while len(self._pending) >= HOP * 2:
                hops.append(self._pending[: HOP * 2])
                del self._pending[: HOP * 2]
        n = len(hops)
        for i, h in enumerate(hops):
            # spread timestamps if several hops arrived in one read
            self._analyze(h, now - (n - 1 - i) * HOP / RATE)

    def _analyze(self, s, t):
        left = s[0::2]
        right = s[1::2]
        sl = sum(v * v for v in left) / HOP
        sr = sum(v * v for v in right) / HOP
        rl, rr = math.sqrt(sl), math.sqrt(sr)
        peak = max(rl, rr, 1.0)
        # auto-gain: follow loud passages quickly, quiet ones slowly
        target = 1.0 / max(peak * 1.4, 300.0)
        self._gain += (target - self._gain) * (0.3 if target < self._gain else 0.01)
        lvl_l, lvl_r = min(1.0, rl * self._gain), min(1.0, rr * self._gain)
        mono = [(left[i] + right[i]) * 0.5 for i in range(0, HOP)]
        dec = [sum(mono[i:i + DECIM]) / DECIM for i in range(0, HOP, DECIM)]
        spec = _fft([complex(dec[i] * _WINDOW[i], 0) for i in range(NFFT)])
        mags = [abs(c) for c in spec[: NFFT // 2]]
        bands = []
        for b in range(NBANDS):
            lo, hi = _EDGES[b], min(NFFT // 2, _EDGES[b + 1])
            v = max(mags[lo:hi]) if hi > lo else 0.0
            g = self._band_gain[b]
            tgt = 1.0 / max(v * 1.3, 2000.0)
            self._band_gain[b] = g + (tgt - g) * (0.25 if tgt < g else 0.008)
            bands.append(min(1.0, v * self._band_gain[b]))
        bass = sum(bands[:3]) / 3
        mid = sum(bands[4:10]) / 6
        treble = sum(bands[10:]) / 6
        beat = False
        if bass > self._bass_avg * 1.45 and bass > 0.35 and t - self._last_beat > 0.22:
            beat = True
            self._last_beat = t
            with self._lock:
                self._beats.append(t)
        self._bass_avg = self._bass_avg * 0.9 + bass * 0.1
        # scope: the first ~12 ms (a few waves of a typical note), 64 points
        wave = [max(-1.0, min(1.0, mono[i] * self._gain * 1.2)) for i in range(0, 512, 8)]
        f = {"raw": max(rl, rr) / 32768.0,       # un-gained loudness (sync test)
             "level_l": lvl_l, "level_r": lvl_r, "level": (lvl_l + lvl_r) / 2, "bands": bands,
             "bass": bass, "mid": mid, "treble": treble, "beat": beat, "wave": wave, "t": t, "live": True}
        with self._lock:
            self._frames.append(f)
            while self._frames and t - self._frames[0]["t"] > KEEP_S:
                self._frames.popleft()

    def at(self, now=None) -> dict:
        """Features for what the speaker is playing now (now - delay)."""
        want = (now or time.time()) - max(0.0, float(self._get_delay() or 0))
        with self._lock:
            if not self._frames:
                return SILENT
            best = None
            for f in reversed(self._frames):
                if f["t"] <= want:
                    best = f
                    break
            if best is None or want - best["t"] > 0.3:
                return SILENT
            # a beat counts if any frame since the previous call had one
            return best

    def beat_clock(self, now=None):
        """The music's beat, as heard at the speaker (now - delay):
        returns (position_in_beats, seconds_per_beat), or None when there is no
        steady beat to follow (no music, or no beat for 4 s)."""
        want = (now or time.time()) - max(0.0, float(self._get_delay() or 0))
        with self._lock:
            beats = [b for b in self._beats if b <= want]
        if len(beats) < 4 or want - beats[-1] > 4.0:
            return None
        # only the current run of beats: an earlier song (or a pause) has its
        # own phase and would throw the fit off
        start = len(beats) - 1
        while start > 0 and beats[start] - beats[start - 1] < 2.0:
            start -= 1
        beats = beats[start:]
        if len(beats) < 4:
            return None
        gaps = sorted(b - a for a, b in zip(beats[-9:], beats[-8:]) if b - a > 0.2)
        if len(gaps) < 3:
            return None
        period = gaps[len(gaps) // 2]          # median gap: rough tempo
        recent = beats[-16:]
        # refine: give each beat its number on the grid and fit a line through
        # them (beat times are only accurate to one analysis step, ~46 ms)
        ks = [round((b - recent[0]) / period) for b in recent]
        if len(set(ks)) >= 3:
            mk, mt = sum(ks) / len(ks), sum(recent) / len(recent)
            var = sum((k - mk) ** 2 for k in ks)
            if var > 0:
                period = sum((k - mk) * (b - mt) for k, b in zip(ks, recent)) / var
                t0 = mt - period * mk                 # fitted time of beat 0
            else:
                t0 = recent[0]
        else:
            t0 = recent[0]
        fold = 1.0
        while period * fold < 0.36:            # fold to 60..165 BPM
            fold *= 2
        while period * fold > 1.0:
            fold /= 2
        pos = (want - t0) / period / fold      # beats (at the folded tempo)
        return pos, period * fold

    def tempo(self):
        c = self.beat_clock()
        return round(60.0 / c[1]) if c else None

    def live(self) -> bool:
        with self._lock:
            return bool(self._frames) and time.time() - self._frames[-1]["t"] < 1.0
