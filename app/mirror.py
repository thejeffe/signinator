"""Mirror screens: extra displays (e.g. Matrixinator on a touch screen) that show
the same picture as the sign. They answer like WLED (/json/info, /json/state,
DDP on 4048), but get the plain frame: no LED gamma, white balance, dithering or
dimming (a screen shows sRGB). Their backlight follows the sign's brightness and
power through /json/state, and they get the song for their tap-to-show overlay.

Config: mirror: {hosts: [...], look: "led" | "hires", size: 120, fps: 20}
  look "led" (default): the sign's own frames, drawn as round LED dots. A screen
    reports its own matrix size; when it differs from the sign, the frame is
    scaled to fit (nearest neighbour keeps the LED look).
  look "hires": a second copy of the content rendered at size x size just for the
    screens (smooth text, the original album art, GIF detail, finer effects),
    drawn as LED dots like the sign. It runs on its own thread at `fps`, so the sign's
    frame rate is unaffected."""
from __future__ import annotations
import json
import logging
import socket
import struct
import threading
import time
import urllib.request

from PIL import Image

from .matrix import DDPSender
from .wled import read_wled

log = logging.getLogger("signinator.mirror")
MAX_HOSTS = 4
HIRES_SIZES = (60, 80, 96, 120, 160, 240)  # 480 / size = whole screen pixels per pixel
FPS_SETTLE_S, FPS_PROBE_S = 6.0, 120.0      # auto frame rate: let a change settle; try the maximum again
TEXT_KEYS = ("color", "bg", "speed", "scroll", "emoji", "effect", "gap_mode", "color2",
             "size", "font", "height", "smooth", "align")


def _post(host, path, body, timeout=3.0):
    req = urllib.request.Request("http://%s%s" % (host, path), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=timeout).read()


class Mirrors:
    POLL_S = 5.0

    def __init__(self, engine):
        self.e = engine
        self._lock = threading.Lock()
        self._targets = {}        # host -> {"sender", "w", "h", "online", "fps", "error", "sent": {...}}
        self._kick = threading.Event()
        self._hi_key = None
        self._hi_scene = None
        self.hires_fps = 0.0
        self.sent_fps = 0.0                  # frames per second actually sent to the screens
        self._last_send = 0.0
        self._fps_target = None              # auto frame rate (None = the maximum)
        self._fps_changed = time.monotonic()
        self._fps_probe = 0.0
        self.photo_active = False            # album art / GIF / picture: sent as full-resolution photos
        self._ph_key = self._ph_scene = self._ph_jpeg = None
        self._ph_static, self._ph_last = False, 0.0
        self._ph_size = 480
        self.sync_hosts()
        threading.Thread(target=self._loop, daemon=True, name="mirrors").start()
        threading.Thread(target=self._hires_loop, daemon=True, name="mirrors-hires").start()

    def cfg(self) -> dict:
        c = dict(self.e.cfg.get("mirror") or {})
        look = c.get("look") if c.get("look") in ("led", "hires") else "led"
        size = int(c.get("size") or 120)
        size = size if size in HIRES_SIZES else 120
        fps = max(5, min(60, int(c.get("fps") or 30)))
        return {"hosts": self.hosts(), "look": look, "size": size, "fps": fps,
                "fps_auto": c.get("fps_auto", True) is not False,
                "photos": c.get("photos", True) is not False,
                "enabled": c.get("enabled", True) is not False}      # screens on/off (dark when off)

    def current_fps(self) -> float:
        c = self.cfg()
        if c["fps_auto"] and self._fps_target:
            return max(5.0, min(float(c["fps"]), self._fps_target))
        return float(c["fps"])

    def _adapt_fps(self, screen_fps):
        """Auto frame rate: send about what the screen manages to draw (it reports its own fps)."""
        c = self.cfg()
        now = time.monotonic()
        if not c["fps_auto"]:
            self._fps_target = None
            return
        # only for LED content: photos pace themselves (the screen acknowledges each one), and
        # still photos (one a second) would teach it the screen is slow
        if self.photo_active or self._ph_key is not None:
            return
        if screen_fps is None or self.sent_fps < 3 or now - self._fps_changed < FPS_SETTLE_S:
            return
        if screen_fps < self.sent_fps * 0.85:
            nv = max(5.0, round(screen_fps * 0.95))
            if nv != self._fps_target:
                log.info("mirror: screen draws %s of %.0f fps sent; sending %s", screen_fps, self.sent_fps, nv)
                self._fps_target, self._fps_changed = nv, now
            self._fps_probe = now + FPS_PROBE_S
        elif self._fps_target and self._fps_target < c["fps"] and now >= self._fps_probe:
            log.info("mirror: trying the maximum frame rate again")
            self._fps_target, self._fps_changed = None, now

    def hosts(self) -> list:
        hs = (self.e.cfg.get("mirror") or {}).get("hosts") or []
        return [str(h) for h in hs][:MAX_HOSTS]

    def sync_hosts(self):
        """Settings changed: add/remove targets."""
        want = self.hosts()
        with self._lock:
            for h in list(self._targets):
                if h not in want:
                    self._targets.pop(h)["sender"].close()
            for h in want:
                if h not in self._targets:
                    self._targets[h] = {"sender": DDPSender(h.split(":")[0], 4048), "w": None, "h": None,
                                        "online": None, "fps": None, "error": None, "name": None, "sent": {}}
        self._kick.set()

    def status(self) -> list:
        with self._lock:
            return [{"host": h, "online": t["online"], "name": t["name"], "width": t["w"], "height": t["h"],
                     "fps": t["fps"], "error": t["error"] or t["sender"].error,
                     "hires_fps": round(self.hires_fps, 1), "sent_fps": round(self.sent_fps, 1),
                     "fps_target": round(self.current_fps(), 1), "photo": self.photo_active}
                    for h, t in self._targets.items()]

    # ---- frames (called from the render loop) ----
    def follows_sign(self) -> bool:
        """The screen can take the sign's own frames when both show the same thing."""
        e = self.e
        if not e.power or e.released:
            return False                     # the sign is off (or WLED's): the screen renders its own
        return e.shows("screen") == e.shows("sign")

    def send(self, frame: Image.Image):
        c = self.cfg()
        if not c["enabled"] or c["look"] != "led" or self.photo_active or not self.follows_sign():
            return                           # the hires thread sends instead
        if time.monotonic() - self._last_send < 1.0 / self.current_fps() - 0.002:
            return                           # the screen gets its own (lower) frame rate
        self._send(frame)

    def _count_sent(self):
        now = time.monotonic()
        if self._last_send:
            self.sent_fps = self.sent_fps * 0.9 + (1.0 / max(1e-3, now - self._last_send)) * 0.1
        self._last_send = now

    def _send(self, frame: Image.Image):
        self._count_sent()
        with self._lock:
            targets = list(self._targets.values())
        for t in targets:
            if t["online"] is False:
                continue                     # don't spray packets at a screen that's away
            img = frame
            if self.cfg()["look"] == "led" and t["w"] and t["h"] and (t["w"], t["h"]) != frame.size:
                img = frame.resize((t["w"], t["h"]), Image.NEAREST)
            t["sender"].send(img.tobytes())

    # ---- state: geometry, backlight, song ----
    def _loop(self):
        while True:
            with self._lock:
                items = list(self._targets.items())
            e = self.e
            c = self.cfg()
            # the screen's own toggle (LCD screen) decides; the LED sign's power doesn't
            want_state = {"on": bool(c["enabled"] and e.screen_awake() and e.shows("screen") != "dark"),
                          "bri": int(e.brightness)}
            # full resolution is drawn as LED dots too (the LED feel at any size)
            want_look = {"look": "pixels", "w": c["size"], "h": c["size"], "shape": "dots"} \
                if c["look"] == "hires" else {"look": "dots"}
            np = e.nowplaying.status() if getattr(e, "nowplaying", None) else {}
            want_song = {"title": np.get("title") or "", "artist": np.get("artist") or ""} \
                if np.get("state") == "playing" else {"title": "", "artist": ""}
            if time.monotonic() - self._last_send > 2.0:
                self.sent_fps = 0.0              # not sending (sign off, released)
            # ignore a screen that just restarted (its count starts low)
            fps_seen = [t["fps"] for _, t in items if t["online"] and t["fps"] is not None
                        and t.get("uptime", 0) >= 20]
            self._adapt_fps(min(fps_seen) if fps_seen else None)
            for host, t in items:
                info = read_wled(host)
                was = t["online"]
                t["online"] = bool(info.get("ok"))
                t["error"] = None if t["online"] else info.get("error")
                if t["online"]:
                    g = info.get("geometry") or {}
                    t["w"], t["h"] = g.get("width"), g.get("height")
                    t["fps"], t["name"] = info.get("fps"), info.get("name")
                    t["photo_port"] = info.get("photoPort")
                    up = info.get("uptime") or 0
                    if not was or up < t.get("uptime", 0):
                        t["sent"] = {}       # (re)connected or restarted: send everything again
                    t["uptime"] = up
                    if not was:
                        log.info("mirror %s online (%sx%s)", host, t["w"], t["h"])
                    try:
                        if t["sent"].get("state") != want_state:
                            _post(host, "/json/state", want_state)
                            t["sent"]["state"] = want_state
                        if t["sent"].get("look") != want_look:
                            _post(host, "/api/look", want_look)
                            t["sent"]["look"] = want_look
                        if t["sent"].get("song") != want_song:
                            _post(host, "/api/overlay", want_song)
                            t["sent"]["song"] = want_song
                    except Exception as ex:  # noqa: BLE001
                        t["error"] = str(ex)
                elif was:
                    log.info("mirror %s offline: %s", host, t["error"])
            self._kick.wait(self.POLL_S)
            self._kick.clear()

    def poke(self):
        """Brightness / power / song changed: update the screens now."""
        self._kick.set()

    # ---- full-resolution look ----
    def _hires_loop(self):
        e = self.e
        try:                                   # the sign comes first: lower this thread's priority
            import os
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (OSError, AttributeError):
            pass
        last = time.monotonic()
        while True:
            c = self.cfg()
            t0 = time.monotonic()
            with self._lock:
                any_on = c["enabled"] and any(t["online"] for t in self._targets.values())
            awake = e.screen_awake()
            photo = c["photos"] and any_on and awake and self._is_photo()
            if photo != self.photo_active:
                # LED content and photos have different speeds: measure again from the maximum
                self._fps_target, self._fps_changed = None, time.monotonic()
            self.photo_active = photo
            if photo:
                try:
                    if self._photo_step(t0):
                        continue                 # a still photo: nothing new to send yet
                except Exception as ex:  # noqa: BLE001
                    log.warning("mirror photo: %s", ex)
                    self._ph_scene = None
                    time.sleep(1.0)
                dt = time.monotonic() - t0
                time.sleep(max(0.0, 1.0 / self.current_fps() - dt))
                continue
            self._ph_key = None
            # LED content drawn here: the higher-resolution look, or (sign resolution) when the screen
            # shows something other than the sign (routing), else the sign's own frames are sent
            own = c["look"] == "hires" or not self.follows_sign()
            if not own or not any_on or not awake or e.shows("screen") == "dark":
                self._hi_key, self._hi_scene, self.hires_fps = None, None, 0.0
                time.sleep(0.25)
                continue
            try:
                W, H = (c["size"], c["size"]) if c["look"] == "hires" else (e.w, e.h)
                key = self._content_key((W, H))
                if key != self._hi_key or self._hi_scene is None:
                    self._hi_scene = self._build(W, H)
                    self._hi_key = key
                frame = self._hi_scene.render(t0)
                if frame.size == (W, H) and frame.mode == "RGB":
                    self._send(frame)
            except Exception as ex:  # noqa: BLE001 - never kill the thread
                log.warning("full-resolution mirror: %s", ex)
                self._hi_scene = None
                time.sleep(1.0)
            dt = time.monotonic() - t0
            time.sleep(max(0.0, 1.0 / self.current_fps() - dt))
            now = time.monotonic()
            self.hires_fps = self.hires_fps * 0.9 + (1.0 / max(1e-3, now - last)) * 0.1
            last = now

    # ---- photos: album art, GIFs and pictures at full resolution (JPEG to /api/photo) ----
    def _is_photo(self) -> bool:
        e = self.e
        k = e.shows("screen")
        if k == "art":
            return bool(e.nowplaying.art)
        return k == "content" and e.state.get("kind") in ("gif", "image")

    def _photo_step(self, t0) -> bool:
        """Send the next photo. Returns True when nothing needed sending (a still that's up to date)."""
        key = self._content_key(480)
        if key != self._ph_key or self._ph_scene is None:
            # stills (album art, pictures) at the screen's full 480x480; animations at 240x240,
            # which the screen doubles: a quarter of the work per frame
            animated = self._photo_animated()
            self._ph_size = 240 if animated else 480
            self._ph_scene = self._build(self._ph_size, self._ph_size)
            self._ph_key = key
            frames = getattr(self._ph_scene, "frames", None)
            self._ph_static = (not animated and frames is not None and len(frames) == 1 and
                               getattr(self._ph_scene, "mode", "") != "scroll")
            self._ph_jpeg, self._ph_last = None, 0.0
        now = time.monotonic()
        if self._ph_static and self._ph_jpeg and now - self._ph_last < 1.0:
            time.sleep(0.1)                     # stills: re-send once a second so the screen stays live
            return True
        if not self._ph_static or self._ph_jpeg is None:
            from io import BytesIO
            buf = BytesIO()
            self._ph_scene.render(t0).convert("RGB").save(buf, "JPEG", quality=85 if self._ph_size == 480 else 88)
            self._ph_jpeg = buf.getvalue()
        self._post_photo(self._ph_jpeg, scale=2 if self._ph_size == 240 else 1)
        self._ph_last = time.monotonic()
        if not self._ph_static:
            self._count_sent()
        return False

    def _photo_animated(self) -> bool:
        e = self.e
        if e.shows("screen") == "art":
            return False                       # album art
        st = e.state
        path = e.gifs.path(st.get("name", "")) if st.get("kind") == "gif" else e._image_path
        if st.get("kind") == "gif" and st.get("beat"):
            return True
        try:
            with Image.open(path) as im:
                return bool(getattr(im, "is_animated", False)) or st.get("mode") == "scroll"
        except Exception:  # noqa: BLE001
            return False

    def _post_photo(self, jpeg: bytes, scale=1):
        """Send one photo to each screen: over its photo stream (waits until it's drawn), else HTTP."""
        with self._lock:
            targets = [(h, t) for h, t in self._targets.items() if t["online"]]
        for h, t in targets:
            if t.get("photo_port"):
                self._stream_photo(h, t, jpeg, scale)
            else:
                self._http_photo(h, jpeg)

    def _stream_photo(self, host, t, jpeg, scale):
        s = t.get("photo_sock")
        try:
            if s is None:
                s = socket.create_connection((host.split(":")[0], int(t["photo_port"])), timeout=3)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                t["photo_sock"] = s
            s.settimeout(3)
            s.sendall(b"MXF1" + bytes([1 if scale == 2 else 0]) + b"\0\0\0" + struct.pack("<I", len(jpeg)) + jpeg)
            if s.recv(1) != b"K":               # drawn: ready for the next one
                raise OSError("no answer")
        except OSError as ex:
            t["error"] = "photo stream: %s" % ex
            try:
                s and s.close()
            except OSError:
                pass
            t["photo_sock"] = None

    def _http_photo(self, h, jpeg: bytes):
        b = "----signinator%d" % int(time.time() * 1000)
        body = (("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"photo.jpg\"\r\n"
                 "Content-Type: image/jpeg\r\n\r\n" % b).encode() + jpeg + ("\r\n--%s--\r\n" % b).encode())
        req = urllib.request.Request("http://%s/api/photo" % h, data=body,
                                     headers={"Content-Type": "multipart/form-data; boundary=" + b})
        try:
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as ex:  # noqa: BLE001
            with self._lock:
                if h in self._targets:
                    self._targets[h]["error"] = "photo: %s" % ex

    def _content_key(self, size):
        e = self.e
        np = e.nowplaying
        k = e.shows("screen")
        ov_key = "art:%s:%s" % (np.art_sha, np.cfg().get("mode")) if k == "art" else k
        return (size, json.dumps(e.state, sort_keys=True, default=str), ov_key)

    def _build(self, W, H):
        """The current content again, rendered at W x H for the screens."""
        from .render import (ImageScene, SizedTextScene, SolidScene, TestPatternScene,
                             load_image_bytes)
        e = self.e
        np = e.nowplaying
        k = e.shows("screen")
        if k == "art" and np.art:
            # the original cover, not the LED-tuned one
            return ImageScene(W, H, load_image_bytes(np.art), mode=np.cfg().get("mode", "fill"))
        if k == "dark":
            return SolidScene(W, H, "#000000")
        st = dict(e.state)
        kind = st.get("kind")
        f = W / max(1, e.w)                      # how much bigger than the sign
        if kind == "text":
            args = {k: st[k] for k in TEXT_KEYS if k in st}
            if args.get("size", "normal") == "normal":
                args["size"], args["height"] = "px", int(8 * f)
            elif args.get("size") == "px":
                args["height"] = int((args.get("height") or e.h) * f)
            args["speed"] = max(1, int(int(args.get("speed", 24)) * f))   # same speed across the screen
            return SizedTextScene(W, H, st.get("text", ""), **args)
        if kind == "effect":
            from .effects import EffectScene
            return EffectScene(W, H, st)
        if kind == "gif":
            p = e.gifs.path(st.get("name", ""))
            if p:
                with open(p, "rb") as fh:
                    img = load_image_bytes(fh.read())
                if st.get("beat"):
                    from .render import BeatGifScene
                    return BeatGifScene(W, H, img, mode=st.get("mode", "fit"), rate=st.get("rate", 1.0),
                                        beats_per_loop=st.get("bpl", "auto"))
                return ImageScene(W, H, img, mode=st.get("mode", "fit"), rate=st.get("rate", 1.0))
        if kind == "image":
            try:
                with open(e._image_path, "rb") as fh:
                    img = load_image_bytes(fh.read())
                return ImageScene(W, H, img, mode=st.get("mode", "fit"),
                                  speed=max(1, int(int(st.get("speed", 24)) * f)))
            except OSError:
                pass
        if kind == "test":
            return TestPatternScene(W, H, st.get("pattern", "border"))
        return SolidScene(W, H, "#000000")
