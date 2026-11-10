"""Read shairport-sync's metadata pipe (Signinator as a silent AirPlay 2 speaker)
and feed track info + cover art to NowPlaying.

shairport-sync writes items like:
  <item><type>636f7265</type><code>6d696e6d</code><length>12</length>
  <data encoding="base64">
  SGVsbG8gd29ybGQh</data></item>
type/code are 4-character codes in hex ('core'/'minm' = title, 'ssnc'/'PICT' = art).
"""
from __future__ import annotations
import base64
import logging
import os
import re
import threading
import time

log = logging.getLogger("signinator.airplay")

_ITEM = re.compile(
    rb"<item><type>([0-9a-fA-F]{8})</type><code>([0-9a-fA-F]{8})</code>"
    rb"<length>(\d+)</length>\s*(?:<data encoding=\"base64\">\s*([A-Za-z0-9+/=\s]*?)</data>)?\s*</item>",
    re.S)
_MAX_BUF = 8 * 1024 * 1024   # cover art can be a few hundred KB of base64

# ssnc codes -> playback state
_STATE = {
    "pbeg": "playing", "prsm": "playing", "pres": "playing",
    "pfls": "paused", "paus": "paused",
    "pend": "stopped", "aend": "stopped",
}


def _code(h: bytes) -> str:
    try:
        return bytes.fromhex(h.decode()).decode("latin-1")
    except ValueError:
        return "????"


def parse(buf: bytes):
    """Yield (type, code, data_bytes) for complete items; return the unread rest
    via the generator's .rest attribute (see AirplayMetaReader._feed)."""
    out = []
    end = 0
    for m in _ITEM.finditer(buf):
        data = b""
        if m.group(4):
            try:
                data = base64.b64decode(re.sub(rb"\s+", b"", m.group(4)))
            except ValueError:
                data = b""
        out.append((_code(m.group(1)), _code(m.group(2)), data))
        end = m.end()
    return out, buf[end:]


class AirplayMetaReader:
    def __init__(self, nowplaying, get_path, on_volume=None):
        self.np = nowplaying
        self._on_volume = on_volume   # called with the phone's AirPlay volume (dB)
        self._get_path = get_path
        self.status = {"pipe": None, "connected": False, "items": 0, "error": None}
        self._pending = {}
        threading.Thread(target=self._loop, daemon=True, name="airplay-meta").start()

    def _loop(self):
        while True:
            path = self._get_path()
            self.status["pipe"] = path
            if not path or not os.path.exists(path):
                self.status["connected"] = False
                time.sleep(5)
                continue
            try:
                fd = os.open(path, os.O_RDONLY)   # a FIFO blocks here until shairport-sync opens it
            except OSError as e:
                self.status["error"] = str(e)
                time.sleep(5)
                continue
            self.status.update(connected=True, error=None)
            buf = b""
            try:
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break            # writer went away
                    buf += chunk
                    items, buf = parse(buf)
                    for it in items:
                        self._handle(*it)
                    if len(buf) > _MAX_BUF:
                        buf = buf[-1024:]
            except OSError as e:
                self.status["error"] = str(e)
            finally:
                os.close(fd)
                self.status["connected"] = False
            time.sleep(1)

    def _handle(self, typ: str, code: str, data: bytes):
        self.status["items"] += 1
        try:
            if typ == "core":
                key = {"minm": "title", "asar": "artist", "asal": "album"}.get(code)
                if key:
                    self._pending[key] = data.decode("utf-8", "replace")
                return
            if typ != "ssnc":
                return
            if code == "mden":           # end of a metadata bundle: send title/artist/album together
                if self._pending:
                    self.np.update("airplay", **self._pending)
                    self._pending = {}
            elif code == "PICT":
                if data:
                    self.np.update("airplay", art=data)
            elif code == "pvol" and self._on_volume:
                # "airplay_volume,volume,lowest,highest"; airplay_volume is
                # 0 (max) .. -30 (quietest), -144 = mute
                try:
                    self._on_volume(float(data.decode().split(",")[0]))
                except (ValueError, UnicodeError):
                    pass
            elif code in _STATE:
                self.np.update("airplay", state=_STATE[code])
        except Exception as e:  # noqa: BLE001
            log.warning("airplay metadata %s/%s: %s", typ, code, e)
