"""Talk to the WLED controller's JSON API: read its setup (incl. 2D geometry)
and keep a small online/offline status for the UI."""
from __future__ import annotations
import json
import re
import threading
import time
import urllib.request

# host name or IPv4, optional :port (the HTTP port, for testing against a fake)
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?$")


def valid_host(host: str) -> bool:
    return bool(host) and bool(HOST_RE.match(host))


def _get(host: str, path: str, timeout: float = 4.0):
    with urllib.request.urlopen("http://%s%s" % (host, path), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def read_wled(host: str) -> dict:
    """Return what Signinator needs to know about a WLED controller.

    geometry = the size the sign should render at:
      * WLED has a 2D matrix  -> its width x height, mapping "matrix"
        (host sends row-major from top-left; WLED applies the wiring, which
        needs "respect LED maps" on for realtime).
      * no 2D matrix          -> None (the sign keeps its own layout and the
        host does the panel mapping itself).
    """
    if not valid_host(host):
        return {"ok": False, "host": host, "error": "not a valid host name or IP"}
    try:
        info = _get(host, "/json/info")
        cfg = _get(host, "/json/cfg")
        try:
            state = _get(host, "/json/state")      # on / brightness, for the heartbeat
        except Exception:  # noqa: BLE001
            state = {}
    except Exception as e:  # noqa: BLE001 - report any failure to the UI
        msg = getattr(e, "reason", None) or str(e)
        return {"ok": False, "host": host, "error": str(msg)}
    leds = info.get("leds") or {}
    hw = ((cfg.get("hw") or {}).get("led") or {})
    live = ((cfg.get("if") or {}).get("live") or {})
    mx = leds.get("matrix") or None
    panels = []
    for p in ((hw.get("matrix") or {}).get("panels") or []):
        panels.append({"x": p.get("x", 0), "y": p.get("y", 0),
                       "w": p.get("w"), "h": p.get("h"),
                       "bottom": bool(p.get("b")), "right": bool(p.get("r")),
                       "vertical": bool(p.get("v")), "serpentine": bool(p.get("s"))})
    geometry = None
    if mx and mx.get("w") and mx.get("h"):
        geometry = {"width": int(mx["w"]), "height": int(mx["h"]), "mode": "matrix"}
    return {
        "ok": True, "host": host,
        "name": info.get("name"), "version": info.get("ver"), "ip": info.get("ip"),
        "count": leds.get("count"), "maxpwr": leds.get("maxpwr"),
        "live": bool(info.get("live")), "liveSource": info.get("lip") or "",
        "fps": leds.get("fps"), "uptime": info.get("uptime"),
        "realtime": bool(live.get("en")), "ledMaps": bool(live.get("rlm")),
        "dither": info.get("dither"),     # None = stock WLED (no temporal dithering)
        "photoPort": info.get("photoPort"),   # Matrixinator: TCP port for the photo stream
        "state": {"on": state.get("on"), "bri": state.get("bri")} if state else None,
        "realtimeGamma": not live.get("no-gc"),
        "matrix": mx, "panels": panels, "geometry": geometry,
        "outputs": [{"start": o.get("start"), "len": o.get("len"),
                     "pin": (o.get("pin") or [None])[0]}
                    for o in (hw.get("ins") or [])],
    }


class WledMonitor:
    """Background poll of the controller so the page can show ONLINE/OFFLINE.
    on_info(info) is called after every successful read."""

    def __init__(self, get_host, on_info=None, every: float = 15.0):
        self._get_host = get_host
        self._on_info = on_info
        self._every = every
        self._kick = threading.Event()
        self.status = {"online": None, "host": "", "checked": 0}
        threading.Thread(target=self._loop, daemon=True).start()

    def check_now(self):
        self._kick.set()

    def _loop(self):
        while True:
            host = self._get_host()
            if host:
                info = read_wled(host)
                self.status = {
                    "online": info["ok"], "host": host, "checked": time.time(),
                    "name": info.get("name"), "version": info.get("version"),
                    "error": info.get("error"), "matrix": info.get("matrix"),
                    "ledMaps": info.get("ledMaps"), "realtime": info.get("realtime"),
                    "fps": info.get("fps"), "count": info.get("count"),
                    "dither": info.get("dither"),
                }
                if info["ok"] and self._on_info:
                    try:
                        self._on_info(info)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                self.status = {"online": None, "host": "", "checked": time.time()}
            self._kick.wait(self._every)
            self._kick.clear()
