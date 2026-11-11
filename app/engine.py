"""The engine: owns config, current scene, the render/stream loop, and preview."""
from __future__ import annotations
import json
import logging
import os
import threading
import time
import random
from array import array
from io import BytesIO

log = logging.getLogger("signinator.engine")
from PIL import Image
from . import config as cfgmod
from .matrix import DDPSender, build_coords
from .wled import WledMonitor
from .render import (SolidScene, SizedTextScene, ImageScene, TestPatternScene,
                     load_image_bytes)


class Engine:
    def __init__(self):
        self.cfg = cfgmod.load()
        self.w = int(self.cfg["width"])
        self.h = int(self.cfg["height"])
        self.brightness = int(self.cfg.get("brightness", 128))
        self.gamma = float(self.cfg.get("gamma", 2.2))
        cc = self.cfg.get("color_correction", {})
        self.cc = (int(cc.get("r", 255)), int(cc.get("g", 176)), int(cc.get("b", 240)))
        self.power = True
        self.released = False   # True: Signinator sends nothing; WLED runs its own effects
        self._lock = threading.Lock()
        self._sender = None
        self._coords = []
        self._dither_err = None   # per LED channel: leftover fraction of a level
        self._recompute_luts()
        self.latest = Image.new("RGB", (self.w, self.h))
        self.scene = None
        self._override = None    # e.g. album art while music plays; never saved
        self._override_kind = "art"
        self.state = {"kind": "idle"}
        self._stop = threading.Event()
        # persistent runtime state (content + brightness + power) survives restart
        data_dir = self.cfg.get("server", {}).get("data_dir", "data")
        self._data_dir = os.path.join(cfgmod.base_dir(), data_dir)
        os.makedirs(self._data_dir, exist_ok=True)
        self._state_path = os.path.join(self._data_dir, "state.json")
        self._image_path = os.path.join(self._data_dir, "current_image")
        # user-uploaded custom emoji live here (their own images)
        from . import customglyphs
        self._emoji_dir = os.path.join(self._data_dir, "custom_emoji")
        customglyphs.set_store_dir(self._emoji_dir)
        from .gifs import Library
        self.gifs = Library(os.path.join(self._data_dir, "gifs"))
        self._rebuild_mapping()
        self._rebuild_sender()
        self._restore()
        self._sync_wled_bri()  # WLED at 100% while Signinator dithers, else WLED dims
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.wled_info = None   # last full read of the controller
        self._fps_target = None        # auto frame rate (None = use the setting)
        self._fps_changed_at = time.time()   # WLED's count lags after a change
        self._fps_probe_at = 0.0
        self._fps_wled_key = None
        self.monitor = WledMonitor(self._wled_host, on_info=self._on_wled_info, every=8.0)
        from .nowplaying import NowPlaying
        from .airplay_meta import AirplayMetaReader
        self.nowplaying = NowPlaying(self)
        from .relay import Relay
        from .audio import AudioAnalyzer
        from . import effects as fxmod
        # music-reactive effects: delay the analysis to line up with the speaker
        # (only needed while relaying; otherwise the receiver's output is in sync)
        self.audio = AudioAnalyzer(self.light_delay_s)
        fxmod.AUDIO = self.audio
        self.relay = Relay(lambda: self.cfg.get("relay"), on_audio=self.audio.feed)
        self.airplay = AirplayMetaReader(
            self.nowplaying, on_volume=self.relay.set_phone_volume, get_path=
            lambda: (self.cfg.get("airplay") or {}).get("metadata_pipe", "/tmp/shairport-sync-metadata"))
        from .mirror import Mirrors
        self.mirrors = Mirrors(self)  # extra screens showing the same picture (e.g. Matrixinator)
        from .ha import HABridge
        self.ha = HABridge(self)      # Home Assistant over MQTT (off until set up)
        from .stereo import Stereo
        self.stereo = Stereo(self)    # turns the stereo (a Home Assistant entity) on with the music
        # covers are now named by picture: convert the saved ones (and the hidden list) once
        threading.Thread(target=lambda: (time.sleep(3), self.nowplaying.migrate_ids()), daemon=True).start()

    def light_delay_s(self) -> float:
        """How late music effects should run: the relay's measured AirPlay delay
        (audio.auto) or a fixed delay_ms while relaying, plus offset_ms."""
        a = self.cfg.get("audio") or {}
        relay = getattr(self, "relay", None)
        base = 0.0
        if relay is not None and (self.cfg.get("relay") or {}).get("target"):
            measured = relay.audio_delay_s()
            if a.get("auto", True) and measured is not None:
                base = measured
            else:
                base = max(0, min(5000, int(a.get("delay_ms", 1500)))) / 1000.0
        off = max(-2000, min(2000, int(a.get("offset_ms", 0)))) / 1000.0
        return max(0.0, base + off)

    # ---- sync test (lights vs. the relayed audio) ----
    def sync_start(self, seconds=60):
        from .effects import SyncFlashScene
        self.relay.start_sync(seconds, on_done=self._sync_done)   # ValueError if it can't
        self.set_override(SyncFlashScene(self.w, self.h), kind="sync")

    def _sync_done(self):
        self.clear_override()
        self.nowplaying.refresh()      # album art comes back if it was up

    def sync_stop(self):
        self.relay.stop_sync()

    # ---- override (album art) ----
    def set_override(self, scene, kind="art"):
        """kind "art": album art (what each display does with it: routing); "sync": the sync test flash."""
        with self._lock:
            self._override = scene
            self._override_kind = kind

    ROUTES = ("all", "art", "noart")

    def route(self, target: str) -> str:
        """routing setting for "sign" or "screen": all (album art while it plays, else the
        animation) | art (album art only, dark otherwise) | noart (always the animation)."""
        m = (self.cfg.get("routing") or {}).get(target, "all")
        return m if m in self.ROUTES else "all"

    def shows(self, target: str) -> str:
        """What a display shows right now: "art", "content" (the animation/text/GIF) or "dark"."""
        m = self.route(target)
        art_on = self._override is not None and getattr(self, "_override_kind", "art") == "art"
        if m == "noart":
            return "content"
        if m == "art":
            # album art only: the cover the whole time music plays (not just the brief flash)
            np = getattr(self, "nowplaying", None)
            return "art" if art_on or (np is not None and np.playing_art()) else "dark"
        return "art" if art_on else "content"

    def _sign_scene(self):
        ov = self._override
        if ov is not None and getattr(self, "_override_kind", "art") != "art":
            return ov                        # sync test flash
        k = self.shows("sign")
        if k == "art":
            return ov if ov is not None else self.nowplaying.led_scene()
        return None if k == "dark" else self.scene

    def clear_override(self):
        with self._lock:
            self._override = None

    def _manual(self):
        """The user sent something: it wins over album art until the track changes."""
        np = getattr(self, "nowplaying", None)
        if np is not None and not getattr(self, "_restoring", False):
            np.dismiss()

    # ---- WLED geometry ----
    def _wled_host(self):
        w = self.cfg.get("wled", {})
        return (w.get("host") or "").strip()

    def _on_wled_info(self, info: dict):
        """Called by the monitor after each successful read. With auto_geometry
        on, follow the controller's 2D matrix size."""
        self.wled_info = info
        try:
            self._wled_heartbeat(info)
        except Exception as e:  # noqa: BLE001
            log.warning("WLED heartbeat: %s", e)
        # WLED dithering: (re)send the settings if WLED lost them (restart) or differs
        want = self.wled_dither_want()
        have = info.get("dither")
        if have is not None and (bool(have.get("on")) != want["on"] or
                                 (want["on"] and int(have.get("bri16", -1)) != want["bri16"])):
            log.info("WLED dithering is %s; setting it again", "on" if have.get("on") else "off")
            self._sync_wled_bri()
        try:
            self._adapt_fps(info)
        except Exception as e:  # noqa: BLE001
            log.warning("auto fps: %s", e)
        g = info.get("geometry")
        if not g or not self.cfg.get("wled", {}).get("auto_geometry", True):
            return
        if (g["width"], g["height"]) != (self.w, self.h) or \
                self.cfg.get("mapping", {}).get("mode") != g["mode"]:
            log.info("WLED %s is %dx%d; resizing the sign to match",
                     info.get("host"), g["width"], g["height"])
            self.set_geometry(g["width"], g["height"], g["mode"])

    def set_geometry(self, width: int, height: int, mode: str = "matrix"):
        """Resize the render surface (and mapping) to width x height."""
        w = max(1, min(1024, int(width)))
        h = max(1, min(256, int(height)))
        m = dict(self.cfg.get("mapping", {}))
        m["mode"] = mode if mode in ("matrix", "panels") else "matrix"
        tw = int(m.get("tile_w") or w)
        th = int(m.get("tile_h") or h)
        if tw <= 0 or w % tw:
            tw = w
        if th <= 0 or h % th:
            th = h
        m.update(tile_w=tw, tile_h=th, tiles_x=w // tw, tiles_y=h // th)
        self.cfg["mapping"] = m
        self.cfg["width"], self.cfg["height"] = w, h
        try:
            cfgmod.save(self.cfg)
        except OSError as e:
            log.warning("could not save config: %s", e)
        with self._lock:
            self.w, self.h = w, h
            self.latest = Image.new("RGB", (w, h))
            self._rebuild_mapping()
            self.scene = None
            self._override = None
            self._pv = None
        self._restore()  # rebuild the current content at the new size
        np = getattr(self, "nowplaying", None)
        if np is not None:
            np.refresh()

    # ---- lifecycle ----
    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- setup ----
    def _recompute_luts(self):
        """Per-channel output tables folding gamma + LED white balance.
        Integer tables: dithering off (WLED's master brightness dims, and
        rounds each channel on its own, which shifts hues when dim).
        Float tables: dithering on (Signinator dims with fractions of a level
        and dithers over time, so the average color stays exact)."""
        g = self.gamma if self.gamma and self.gamma > 0 else 1.0
        luts, lutf = [], []
        for c in self.cc:  # per-channel white-balance factor
            gain = c / 255.0
            f = [((i / 255.0) ** g) * 255 * gain for i in range(256)]
            lutf.append(f)
            luts.append([round(v) for v in f])
        self._lut_r, self._lut_g, self._lut_b = luts
        self._lutf_r, self._lutf_g, self._lutf_b = lutf

    # ---- frame rate: follow what WLED can show ----
    FPS_MIN = 10
    FPS_PROBE_S = float(os.environ.get("SIGNINATOR_FPS_PROBE_S", "300"))   # shorter in tests
    FPS_SETTLE_S = 6.0

    def fps_cfg(self):
        mx = max(5, min(240, int(self.cfg.get("fps", 40) or 40)))
        return {"max": mx, "auto": self.cfg.get("fps_auto", True) is not False}

    def current_fps(self) -> float:
        c = self.fps_cfg()
        if c["auto"] and self._fps_target:
            return max(self.FPS_MIN, min(c["max"], self._fps_target))
        return c["max"]

    def _adapt_fps(self, info):
        """WLED reports how many frames it shows per second (never more than we
        send). Probe: send at the maximum; WLED's count then is what it can
        really show, and we settle at 95% of that (a little headroom so it
        doesn't skip frames). Every few minutes probe again in case WLED got
        faster (e.g. strands split across outputs). Readings are ignored until
        a new rate has run for a few seconds, since WLED's count lags."""
        c = self.fps_cfg()
        if not c["auto"]:
            self._fps_target = None
            return
        wfps, now = info.get("fps"), time.time()
        if not wfps or not info.get("live") or not self.power or self.released:
            return                        # only judge while we're streaming to it
        # WLED rebooted or its LED count changed: measure again from the top
        key = (info.get("count"), info.get("uptime", 0))
        if self._fps_wled_key and (key[0] != self._fps_wled_key[0] or key[1] < self._fps_wled_key[1]):
            log.info("WLED changed (LED count or reboot): measuring its frame rate again")
            self._set_fps_target(None, now)
        self._fps_wled_key = key
        if now - getattr(self, "_fps_changed_at", 0.0) < self.FPS_SETTLE_S:
            return                        # WLED's count hasn't caught up yet
        sent = getattr(self, "send_fps", 0.0) or self.current_fps()
        if wfps < sent * 0.9:             # WLED can't keep up: settle just under it
            new = max(self.FPS_MIN, min(c["max"], int(wfps * 0.95)))
            if new != self._fps_target:
                log.info("WLED shows %s fps of %.0f sent: sending %s", wfps, sent, new)
                self._set_fps_target(new, now)
            self._fps_probe_at = now + self.FPS_PROBE_S
        elif self._fps_target and self._fps_target < c["max"] and now >= self._fps_probe_at:
            log.info("probing: can WLED show more than %s fps?", self._fps_target)
            self._set_fps_target(None, now)        # None = the maximum

    def _set_fps_target(self, value, now):
        self._fps_target = value
        self._fps_changed_at = now

    # ---- temporal dithering ----
    DITHER_MODES = ("off", "spatial", "hue", "temporal", "wled")

    def dither_cfg(self) -> dict:
        """mode: off (WLED dims) | spatial (steady 4x4 pattern) | hue (steady,
        hue-preserving rounding) | temporal (frame-to-frame, needs fast refresh).
        Older configs only had enabled: true/false (= temporal/off)."""
        raw = dict(self.cfg.get("dither") or {})
        mode = raw.get("mode")
        if mode not in self.DITHER_MODES:
            mode = ("temporal" if raw.get("enabled") else "off") if "enabled" in raw else "spatial"
        wfps = raw.get("wled_fps", "auto")      # WLED re-sends per second: "auto" = as fast as the LEDs allow
        if wfps != "auto":
            try:
                wfps = max(0, min(250, int(wfps)))
            except (TypeError, ValueError):
                wfps = "auto"
        try:
            min_hz = max(0, min(120, int(raw.get("min_hz", 0))))
        except (TypeError, ValueError):
            min_hz = 0
        return {"mode": mode, "darkest": bool(raw.get("darkest", False)),
                "enabled": mode != "off", "wled_fps": wfps, "min_hz": min_hz,
                "fit": raw.get("fit", True) is not False}

    def wled_dither_want(self) -> dict:
        """What WLED's own temporal dithering should be set to (dither firmware, mode "wled")."""
        d = self.dither_cfg()
        if d["mode"] != "wled":
            return {"on": False}
        return {"on": True, "bri16": max(1, self.brightness * 257) if self.brightness else 1,
                "g": round(float(self.gamma), 2), "wb": list(self.cc), "dark": d["darkest"],
                "fps": d["wled_fps"], "minHz": d["min_hz"]}

    def _sync_wled_bri(self):
        """Signinator dims (spatial/hue/temporal): WLED stays at 255. Off: WLED dims.
        wled: WLED's dither firmware does gamma, white balance and dimming (bri16)."""
        mode = self.dither_cfg()["mode"]
        bri = self.brightness if mode == "off" else 255
        self._wled_set({"on": True, "bri": bri, "v": False, "dither": self.wled_dither_want()})

    def _wled_heartbeat(self, info):
        """After a WLED restart it may come up off, or at its own brightness: set them again."""
        st = info.get("state")
        if not st or self.released:
            return
        want_bri = self.brightness if self.dither_cfg()["mode"] == "off" else 255
        if st.get("on") is False or (st.get("bri") is not None and int(st["bri"]) != want_bri):
            log.info("WLED is %s at brightness %s: setting it on at %s again",
                     "on" if st.get("on") else "off", st.get("bri"), want_bri)
            self._sync_wled_bri()

    def _wled_set(self, state: dict):
        """Best-effort push to WLED's JSON state API (e.g. brightness)."""
        w = self.cfg.get("wled", {})
        if not (w.get("enabled") and w.get("host")):
            return
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://%s/json/state" % w["host"],
                data=json.dumps(state).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass

    def _rebuild_mapping(self):
        self._coords = build_coords(self.cfg["mapping"], self.w, self.h)
        self._dither_err = None

    def _rebuild_sender(self):
        if self._sender:
            self._sender.close()
            self._sender = None
        w = self.cfg.get("wled", {})
        if w.get("enabled") and w.get("host"):
            # host may carry ":port" for WLED's HTTP API; DDP only wants the name
            self._sender = DDPSender(w["host"].split(":")[0], int(w.get("port", 4048)))

    # ---- persistence ----
    def _save_state(self):
        """Persist current content + brightness + power so a restart resumes."""
        # don't persist transient calibration patterns
        content = self.state if self.state.get("kind") != "test" else {"kind": "idle"}
        mirrors = getattr(self, "mirrors", None)
        if mirrors is not None:
            mirrors.poke()                   # brightness / power may have changed
        data = {"content": content, "brightness": self.brightness,
                "power": self.power, "released": self.released,
                "auto": bool(getattr(self, "auto", False)),
                "led_on": bool(getattr(self, "led_enabled", True))}
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._state_path)
        except OSError as e:
            # Don't fail silently: an unwritable data_dir is the usual reason a
            # setting (e.g. brightness) "won't save" across restarts.
            log.warning("could not persist state to %s: %s", self._state_path, e)

    def _restore(self):
        """Reload last content/brightness/power, else fall back to the default.
        Not a manual send, so it doesn't dismiss album art."""
        self._restoring = True
        try:
            self._restore_inner()
        finally:
            self._restoring = False

    def _restore_inner(self):
        data = None
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = None
        if data:
            self.brightness = int(data.get("brightness", self.brightness))
            self._recompute_luts()
            self.power = bool(data.get("power", True))
            self.released = bool(data.get("released", False))
            self.auto = bool(data.get("auto", False))
            # the LED's own switch (older state files: Auto meant on, otherwise the power)
            self.led_enabled = bool(data.get("led_on", True if self.auto else self.power))
            c = data.get("content", {})
            kind = c.get("kind")
            if kind == "text":
                self.set_text(c.get("text", ""), c.get("color", "#00e5ff"),
                              c.get("bg", "#000000"), c.get("speed", 24),
                              c.get("scroll", True), c.get("emoji", False),
                              c.get("effect", "solid"), c.get("gap_mode", "screen"),
                              color2=c.get("color2", "#ff00ff"),
                              size=c.get("size", "normal"),
                              font=c.get("font", "pixel"), height=c.get("height", 0),
                              smooth=c.get("smooth", False), align=c.get("align", "center"))
                return
            if kind == "image" and os.path.exists(self._image_path):
                try:
                    with open(self._image_path, "rb") as f:
                        self.set_image(f.read(), c.get("mode", "fit"),
                                       c.get("speed", 24), c.get("name", ""))
                    return
                except OSError:
                    pass
            if kind in ("effect", "gif"):
                try:
                    self._apply_look_inner(c)
                    return
                except Exception as e:  # noqa: BLE001
                    log.warning("couldn't restore %s: %s", kind, e)
            if kind == "idle":
                self.clear()
                return
        self.apply_default()

    # ---- content ----
    def apply_default(self):
        d = self.cfg.get("default", {})
        if d.get("kind") == "text":
            self.set_text(d.get("text", "SIGNINATOR"), d.get("color", "#00e5ff"),
                          d.get("bg", "#000000"), d.get("speed", 24),
                          d.get("scroll", True))
        else:
            self.clear()

    def _text_scene(self, text, color="#00e5ff", bg="#000000", speed=24,
                    scroll=True, emoji=False, effect="solid", gap_mode="screen",
                    color2="#ff00ff", size="normal", font="pixel", height=0,
                    smooth=False, align="center"):
        return SizedTextScene(self.w, self.h, text, size=size, speed=speed,
                              scroll=scroll, bg=bg, color=color, emoji=emoji,
                              effect=effect, gap_mode=gap_mode, color2=color2,
                              font=font, height=height, smooth=smooth, align=align)

    def set_text(self, text, color="#00e5ff", bg="#000000", speed=24,
                 scroll=True, emoji=False, effect="solid", gap_mode="screen",
                 color2="#ff00ff", size="normal", font="pixel", height=0,
                 smooth=False, align="center"):
        scene = self._text_scene(text, color, bg, speed, scroll, emoji, effect,
                                 gap_mode, color2, size, font, height, smooth, align)
        self._manual()
        with self._lock:
            self.scene = scene
            self.state = {"kind": "text", "text": text, "color": color,
                          "bg": bg, "speed": speed, "scroll": scroll,
                          "emoji": emoji, "effect": effect, "gap_mode": gap_mode,
                          "color2": color2, "size": size, "font": font,
                          "height": height, "smooth": smooth, "align": align}
        self._save_state()

    # ---- preview: render a look without sending it to the sign ----
    PREVIEW_TTL = 300  # seconds an unused preview is kept

    def preview_text(self, **kw):
        scene = self._text_scene(**kw)
        with self._lock:
            self._pv = scene
            self._pv_used = time.monotonic()

    def preview_image(self, data: bytes, mode="fit", speed=24):
        scene = ImageScene(self.w, self.h, load_image_bytes(data), mode=mode, speed=speed)
        with self._lock:
            self._pv = scene
            self._pv_used = time.monotonic()

    def preview_raw(self) -> bytes:
        """Like frame_raw, but for the preview scene (falls back to live)."""
        import struct
        now = time.monotonic()
        with self._lock:
            pv = getattr(self, "_pv", None)
            fresh = pv is not None and now - getattr(self, "_pv_used", 0) < self.PREVIEW_TTL
            size = (self.w, self.h)
            if fresh:
                self._pv_used = now
        img = None
        if fresh and (pv.w, pv.h) == size:
            try:
                img = pv.render(now)
            except Exception as e:  # noqa: BLE001
                log.warning("preview render failed: %s", e)
        if img is None or img.size != size:
            img = self.latest
        return struct.pack(">HH", img.width, img.height) + img.convert("RGB").tobytes()

    def set_image(self, data: bytes, mode="fit", speed=24, name=""):
        self._manual()
        img = load_image_bytes(data)
        try:
            with open(self._image_path, "wb") as f:
                f.write(data)
        except OSError:
            pass
        with self._lock:
            self.scene = ImageScene(self.w, self.h, img, mode=mode, speed=speed)
            self.state = {"kind": "image", "mode": mode, "speed": speed, "name": name}
        self._save_state()

    # ---- effects and GIFs ----
    def _effect_scene(self, params):
        from .effects import EffectScene
        return EffectScene(self.w, self.h, params)

    @staticmethod
    def _effect_state(scene):
        p = dict(scene.params)
        for k in ("c1", "c2", "c3"):
            p[k] = "#%02x%02x%02x" % tuple(p[k])
        return dict(kind="effect", **p)

    def set_effect(self, params: dict):
        self._manual()
        scene = self._effect_scene(params)
        with self._lock:
            self.scene = scene
            self.state = self._effect_state(scene)
        self._save_state()

    def _gif_scene(self, name, mode="fit", rate=1.0, beat=False, bpl="auto"):
        p = self.gifs.path(name)
        if not p:
            raise ValueError("no GIF called %r" % name)
        with open(p, "rb") as f:
            img = load_image_bytes(f.read())
        if beat:
            from .render import BeatGifScene
            return BeatGifScene(self.w, self.h, img, mode=mode, rate=rate, beats_per_loop=bpl)
        return ImageScene(self.w, self.h, img, mode=mode, rate=rate)

    def set_gif(self, name, mode="fit", rate=1.0, beat=False, bpl="auto"):
        scene = self._gif_scene(name, mode, rate, beat, bpl)
        self._manual()
        with self._lock:
            self.scene = scene
            self.state = {"kind": "gif", "name": name, "mode": scene.mode,
                          "rate": getattr(scene, "free_rate", scene.rate),
                          "beat": bool(beat), "bpl": bpl}
        self._save_state()

    def preview_effect(self, params):
        scene = self._effect_scene(params)
        with self._lock:
            self._pv = scene
            self._pv_used = time.monotonic()

    def preview_gif(self, name, mode="fit", rate=1.0, beat=False, bpl="auto"):
        scene = self._gif_scene(name, mode, rate, beat, bpl)
        with self._lock:
            self._pv = scene
            self._pv_used = time.monotonic()

    # ---- resting state: what the sign shows when music stops ----
    def rest_cfg(self) -> dict:
        r = {"after_music": "rest", "look": None}
        r.update(self.cfg.get("rest") or {})
        return r

    def _apply_look_inner(self, look: dict):
        kind = (look or {}).get("kind")
        if kind == "text":
            args = {k: look[k] for k in ("text", "color", "bg", "speed", "scroll", "emoji", "effect",
                                         "gap_mode", "color2", "size", "font", "height", "smooth",
                                         "align") if k in look}
            self.set_text(**args)
        elif kind == "effect":
            self.set_effect(look)
        elif kind == "gif":
            self.set_gif(look.get("name", ""), look.get("mode", "fit"), look.get("rate", 1.0),
                         bool(look.get("beat")), look.get("bpl", "auto"))
        elif kind == "idle":
            self.clear()
        else:
            raise ValueError("unknown look %r" % kind)

    def apply_look(self, look: dict):
        """Show a saved look without counting it as a manual send."""
        self._restoring = True
        try:
            self._apply_look_inner(look)
        finally:
            self._restoring = False

    def show_rest(self) -> bool:
        look = self.rest_cfg().get("look")
        if not look:
            return False
        self.apply_look(look)
        return True

    def music_ended(self):
        """Album art just went away because the music stopped."""
        r = self.rest_cfg()
        if r.get("after_music") == "rest" and r.get("look"):
            try:
                self.show_rest()
                log.info("album art done: showing the resting look (%s)", r["look"].get("kind"))
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't show the resting look: %s", e)

    def test_pattern(self, kind="border"):
        self._manual()
        with self._lock:
            self.scene = TestPatternScene(self.w, self.h, kind)
            self.state = {"kind": "test", "pattern": kind}

    def clear(self):
        with self._lock:
            self.scene = SolidScene(self.w, self.h, "#000000")
            self.state = {"kind": "idle"}
        self._save_state()

    def set_brightness(self, value: int):
        self.brightness = max(0, min(255, int(value)))
        self.cfg["brightness"] = self.brightness
        # dithering on: Signinator dims (WLED stays at 255); off: WLED dims
        self._sync_wled_bri()
        np = getattr(self, "nowplaying", None)
        if np is not None:
            np.refresh()   # dark-tone lift for album art depends on brightness
        self._save_state()
        # Also persist to config.yaml so the dim level survives a restart even
        # if the runtime state file (data/state.json) can't be written. On boot
        # config.yaml seeds brightness and state.json overrides it when present,
        # so keeping both in sync is safe.
        try:
            cfgmod.save(self.cfg)
        except OSError as e:
            log.warning("could not persist brightness to config: %s", e)

    def set_mode(self, mode: str):
        """Power control for the LED sign (the page's On/Off/WLED/Auto):
        'on'   LED on, not following the music
        'off'  everything manual and off: Auto off, LED off (the LED switch alone: set_led)
        'auto' follow the music: each display that's switched on lights up only while music plays
        'wled' Signinator stops sending and hands the sign back to WLED."""
        if mode == "wled":
            self.released = True
            self._save_state()
            self._wled_set({"live": False})   # leave realtime mode now instead of waiting for WLED's timeout
            return
        self.released = False
        if mode == "auto":
            self.led_enabled = True
            self.set_auto(True)
        elif mode == "on":
            self.led_enabled = True
            self.set_auto(False)
        else:
            self.auto = False
            self._auto_active = None
            self.set_led(False)

    def set_led(self, on: bool):
        """The LED sign's own switch. Off stays off whatever the music does."""
        self.led_enabled = bool(on)
        self.released = False
        self._apply_power()
        self._save_state()

    def set_auto(self, on: bool):
        """Follow the music (both displays, each only if it's switched on)."""
        self.auto = bool(on)
        self._auto_active = None
        if self.auto:
            self._auto_check(force=True)
        self._apply_power()
        self._save_state()

    def screen_awake(self) -> bool:
        """Auto mode with no music: the LCD screen sleeps too (its own switch still decides the rest)."""
        return not getattr(self, "auto", False) or bool(getattr(self, "_auto_active", False))

    def _apply_power(self):
        want = bool(getattr(self, "led_enabled", True)) and self.screen_awake()
        if want != self.power:
            self._set_power(want)

    @property
    def mode(self) -> str:
        if self.released:
            return "wled"
        if getattr(self, "auto", False):
            return "auto"                    # Auto stays Auto when the LED alone is switched off
        return "on" if getattr(self, "led_enabled", True) else "off"

    AUTO_GRACE = 15.0   # seconds of silence before auto mode turns the displays off

    def music_playing(self) -> bool:
        np = getattr(self, "nowplaying", None)
        audio = getattr(self, "audio", None)
        if audio is not None and audio.live():
            return True
        return bool(np and np.info.get("state") == "playing"
                    and time.time() - np.info.get("updated", 0) < 15 * 60)

    def _auto_check(self, force=False):
        """Auto mode: displays on when music starts, off when it has stopped for AUTO_GRACE
        seconds, each only if its own switch is on. Only acts on changes, so a manual wake-up
        stays on until the music next stops."""
        if not getattr(self, "auto", False):
            return
        now = time.time()
        if self.music_playing():
            self._auto_last_music = now
        active = now - getattr(self, "_auto_last_music", 0.0) < self.AUTO_GRACE
        if force or active != getattr(self, "_auto_active", None):
            self._auto_active = active
            want = bool(getattr(self, "led_enabled", True)) and active
            if want != self.power:
                log.info("auto: music %s, sign %s", "started" if active else "stopped", "on" if want else "off")
                self._set_power(want)
            mirrors = getattr(self, "mirrors", None)
            if mirrors is not None:
                mirrors.poke()

    def wake(self):
        """A manual send in auto mode with no music: show it now (if the LED is switched on)."""
        if getattr(self, "auto", False) and not self.power and getattr(self, "led_enabled", True):
            self._set_power(True)

    def set_power(self, on: bool):
        self.set_led(on)

    def _set_power(self, on: bool):
        if on and not self.power:
            self._fps_changed_at = time.time()   # streaming restarts: let WLED's count settle
        self.power = bool(on)
        self.released = False
        # when turning off, push one black frame so the panel goes dark, then
        # the loop stops streaming entirely (realtime off).
        if not self.power and self._sender is not None:
            black = Image.new("RGB", (self.w, self.h))
            self._sender.send(self._frame_to_bytes(black))
        self._save_state()

    def update_config(self, patch: dict):
        """Merge a config patch, persist, and rebuild affected subsystems."""
        old_relay = dict(self.cfg.get("relay") or {})
        cfg = cfgmod._deep_merge(self.cfg, patch)
        self.cfg = cfg
        cfgmod.save(cfg)
        if "gamma" in patch:
            self.gamma = float(cfg.get("gamma", 2.2))
        if "mapping" in patch:
            # Derive canvas dimensions from the tile layout so adding/removing
            # panels via the API resizes the render surface without a restart.
            m = cfg.get("mapping", {})
            new_w = int(m.get("tile_w", self.w)) * int(m.get("tiles_x", 1))
            new_h = int(m.get("tile_h", self.h)) * int(m.get("tiles_y", 1))
            if new_w != self.w or new_h != self.h:
                cfg["width"], cfg["height"] = new_w, new_h
                cfgmod.save(cfg)
                with self._lock:
                    self.w, self.h = new_w, new_h
                    self.latest = Image.new("RGB", (self.w, self.h))
                    self._rebuild_mapping()
                    self.scene = None
                    self._override = None
                # Rebuild the active scene at the new dimensions.
                self._restore()
                self.nowplaying.refresh()
            else:
                with self._lock:
                    self._rebuild_mapping()
        if "nowplaying" in patch:
            self.nowplaying.refresh()
        if "relay" in patch:
            # The Settings page sends every setting on Save: only restart the
            # music sender when something it starts with actually changed.
            new_relay = dict(cfg.get("relay") or {})
            changed = {k for k in set(old_relay) | set(new_relay) if old_relay.get(k) != new_relay.get(k)}
            if changed & {"target", "codec", "volume", "audio_pipe"}:
                log.info("relay settings changed (%s): restarting the music sender", ", ".join(sorted(changed)))
                self.relay.target_changed()
            elif changed:
                self.relay.settings_changed()    # e.g. volume mode: speaker config only
        if "wled" in patch:
            self._rebuild_sender()
            self.monitor.check_now()
        if "color_correction" in patch:
            cc = cfg.get("color_correction", {})
            self.cc = (int(cc.get("r", 255)), int(cc.get("g", 176)),
                       int(cc.get("b", 240)))
        if "brightness" in patch:
            self.brightness = int(cfg["brightness"])
            self._sync_wled_bri()
        if "dither" in patch:
            self._dither_err = None
            self._sync_wled_bri()
            np = getattr(self, "nowplaying", None)
            if np is not None:
                np.refresh()      # album-art dark lift depends on dithering
        if "gamma" in patch or "color_correction" in patch:
            self._recompute_luts()
            if self.dither_cfg()["mode"] == "wled":
                self._sync_wled_bri()            # WLED applies gamma / white balance
        if "mqtt" in patch:
            self.ha.settings_changed()
        if "mirror" in patch:
            self.mirrors.sync_hosts()
        if "stereo" in patch:
            self.stereo.settings_changed()

    # ---- render loop ----
    def _frame_to_bytes(self, img: Image.Image) -> bytes:
        # single combined LUT per channel (gamma * white-balance * brightness)
        px = img.load()
        lr, lg, lb = self._lut_r, self._lut_g, self._lut_b
        coords = self._coords
        iw, ih = img.size
        if coords and (max(x for x, _ in coords) >= iw or max(y for _, y in coords) >= ih):
            return b""  # mapping and frame disagree mid-resize; skip this frame
        d = self.dither_cfg()
        if d["mode"] == "wled":
            # WLED does gamma, white balance and dimming: send the plain picture, or with "fit",
            # each color moved to the nearest one WLED can show steadily (app/colorfit.py)
            out = bytearray(len(coords) * 3)
            i = 0
            fit = None
            if d.get("fit", True):
                info = (getattr(self, "wled_info", None) or {}).get("dither") or {}
                if info.get("levels") is not None:
                    want = self.wled_dither_want()
                    if not hasattr(self, "_colorfit"):
                        from .colorfit import ColorFit
                        self._colorfit = ColorFit()
                    self._colorfit.setup(want["bri16"], want["g"], want["wb"], int(info["levels"]), want["dark"])
                    if self._colorfit.active:
                        fit = self._colorfit.fit
            for (x, y) in coords:
                c = px[x, y]
                out[i], out[i + 1], out[i + 2] = fit(*c) if fit else c
                i += 3
            return bytes(out)
        if d["mode"] == "temporal":
            return self._frame_dithered(img, coords, d["darkest"])
        if d["mode"] == "spatial":
            return self._frame_spatial(img, coords, d["darkest"])
        if d["mode"] == "hue":
            return self._frame_hue(img, coords)
        out = bytearray(len(coords) * 3)
        i = 0
        for (x, y) in coords:
            r, g, bl = px[x, y]
            out[i] = lr[r]
            out[i + 1] = lg[g]
            out[i + 2] = lb[bl]
            i += 3
        return bytes(out)

    def _frame_dithered(self, img, coords, darkest):
        """Temporal dithering: each LED channel carries the leftover fraction
        of a level to the next frame (first-order sigma-delta), so over a few
        frames it averages to the exact dimmed color. Values under one level
        are rounded (no flicker) unless `darkest` is on."""
        n = len(coords) * 3
        err = self._dither_err
        if err is None or len(err) != n:
            rnd = random.Random(42)
            err = self._dither_err = array("f", (rnd.random() for _ in range(n)))
        k = max(0, min(255, self.brightness)) / 255.0
        sr = [v * k for v in self._lutf_r]
        sg = [v * k for v in self._lutf_g]
        sb = [v * k for v in self._lutf_b]
        data = img.tobytes()
        iw = img.width
        out = bytearray(n)
        i = 0
        for (x, y) in coords:
            j = (y * iw + x) * 3
            for v in (sr[data[j]], sg[data[j + 1]], sb[data[j + 2]]):
                if v < 1.0 and not darkest:
                    out[i] = 1 if v >= 0.5 else 0
                else:
                    a = err[i] + v
                    o = int(a)
                    if o > 255:
                        o = 255
                    err[i] = a - o
                    out[i] = o
                i += 1
        return bytes(out)

    # 4x4 ordered-dither (Bayer) thresholds; each channel uses the pattern
    # shifted differently so R, G and B don't step up on the same LEDs.
    _BAYER = (0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5)
    _SHIFT = ((0, 0), (2, 1), (1, 3))

    def _frame_spatial(self, img, coords, darkest):
        """Spatial dithering: within every 4x4 block some LEDs take the level
        above and some the level below, in a fixed pattern, so the block
        averages the exact dimmed color. Nothing changes over time, so the
        refresh rate doesn't matter. Values under one level are rounded unless
        `darkest` is on (then they become a sparse steady pattern)."""
        k = max(0, min(255, self.brightness)) / 255.0
        luts = ([v * k for v in self._lutf_r], [v * k for v in self._lutf_g], [v * k for v in self._lutf_b])
        thr = [[(self._BAYER[((y + sy) % 4) * 4 + (x + sx) % 4] + 0.5) / 16.0 for y in range(4) for x in range(4)]
               for (sx, sy) in self._SHIFT]
        data = img.tobytes()
        iw = img.width
        out = bytearray(len(coords) * 3)
        i = 0
        for (x, y) in coords:
            j = (y * iw + x) * 3
            cell = (y & 3) * 4 + (x & 3)
            for c in range(3):
                v = luts[c][data[j + c]]
                if v < 1.0 and not darkest:
                    o = 1 if v >= 0.5 else 0
                else:
                    o = int(v)
                    if v - o > thr[c][cell]:
                        o += 1
                    if o > 255:
                        o = 255
                out[i] = o
                i += 1
        return bytes(out)

    def _hue_round(self, t):
        """Pick floor/ceil per channel so the color keeps its hue: minimize the
        brightness error plus (weighted) the error in the color's direction, so
        a weak channel isn't dropped to 0 when that would change the hue."""
        tr, tg, tb = t
        n_t = (tr * tr + tg * tg + tb * tb) ** 0.5
        if n_t < 0.35:
            return (0, 0, 0)
        best, best_cost = None, None
        for r in {int(tr), min(255, int(tr) + 1)}:
            for g in {int(tg), min(255, int(tg) + 1)}:
                for b in {int(tb), min(255, int(tb) + 1)}:
                    n_c = (r * r + g * g + b * b) ** 0.5
                    if n_c == 0:
                        continue
                    lum = (r - tr) ** 2 + (g - tg) ** 2 + (b - tb) ** 2
                    hue = ((r / n_c - tr / n_t) ** 2 + (g / n_c - tg / n_t) ** 2 + (b / n_c - tb / n_t) ** 2)
                    cost = lum + 6.0 * n_t * n_t * hue
                    if best_cost is None or cost < best_cost:
                        best, best_cost = (r, g, b), cost
        return best or (0, 0, 0)

    def _frame_hue(self, img, coords):
        """Hue-preserving rounding: every LED holds one steady color, chosen as
        the nearby combination of levels with the closest hue (cached per input
        color, since frames repeat colors a lot)."""
        k = max(0, min(255, self.brightness)) / 255.0
        key = (k, id(self._lutf_r))
        if getattr(self, "_hue_key", None) != key:
            self._hue_key, self._hue_cache = key, {}
        cache = self._hue_cache
        fr, fg, fb = self._lutf_r, self._lutf_g, self._lutf_b
        data = img.tobytes()
        iw = img.width
        out = bytearray(len(coords) * 3)
        i = 0
        for (x, y) in coords:
            j = (y * iw + x) * 3
            px = data[j:j + 3]
            o = cache.get(px)
            if o is None:
                o = self._hue_round((fr[px[0]] * k, fg[px[1]] * k, fb[px[2]] * k))
                if len(cache) > 50000:
                    cache.clear()
                cache[px] = o
            out[i], out[i + 1], out[i + 2] = o
            i += 3
        return bytes(out)

    OFF_KEEPALIVE = 1.0  # seconds; < WLED's ~2.5s realtime timeout

    def _loop(self):
        fps = self.current_fps()
        interval = 1.0 / fps
        self.send_fps = float(fps)     # measured rate, for the page
        black = Image.new("RGB", (self.w, self.h))
        last_off_send = 0.0
        last_auto = 0.0
        while not self._stop.is_set():
            t0 = time.monotonic()
            if t0 - last_auto >= 1.0:
                last_auto = t0
                try:
                    self._auto_check()
                except Exception as e:  # noqa: BLE001
                    log.warning("auto mode: %s", e)
            with self._lock:
                scene = self._sign_scene()
                size = (self.w, self.h)
            if black.size != size:
                black = Image.new("RGB", size)
            frame = black
            if self.power and scene is not None and (scene.w, scene.h) == size:
                try:
                    frame = scene.render(t0)
                except Exception as e:  # noqa: BLE001 - never kill the loop
                    log.warning("render failed: %s", e)
            if frame.size != size or frame.mode != "RGB":
                frame = black
            self.latest = frame
            mirrors = getattr(self, "mirrors", None)
            if mirrors is not None and self.power and not self.released:
                mirrors.send(frame)          # screens get the plain picture (no LED processing)
            if self._sender is not None and not self.released:
                if self.power:
                    self._sender.send(self._frame_to_bytes(frame))
                elif t0 - last_off_send >= self.OFF_KEEPALIVE:
                    # OFF: send black periodically so WLED stays in realtime
                    # mode (holding black) instead of reverting to its screensaver
                    self._sender.send(self._frame_to_bytes(black))
                    last_off_send = t0
            # frames per second can change live (Settings -> Layout)
            interval = 1.0 / self.current_fps()
            dt = time.monotonic() - t0
            if dt < interval:
                time.sleep(interval - dt)
            self.send_fps = self.send_fps * 0.95 + (1.0 / max(1e-3, time.monotonic() - t0)) * 0.05

    # ---- preview ----
    def frame_raw(self) -> bytes:
        """uint16 width, uint16 height (big-endian), then RGB bytes row-major.
        Unscaled by brightness; the page applies that for display."""
        import struct
        img = self.latest
        return struct.pack(">HH", img.width, img.height) + img.tobytes()

    def preview_png(self, scale: int = 10, grid: bool = True) -> bytes:
        src = self.latest
        # reflect master brightness so the Dim slider is visibly effective
        # (skip the LED white-balance correction — that's physical, not visual).
        bf = self.brightness / 255.0
        if self.brightness < 255:
            from PIL import ImageEnhance
            src = ImageEnhance.Brightness(src).enhance(bf)
        big = src.resize((src.width * scale, src.height * scale), Image.NEAREST)
        if grid and scale >= 4:
            from PIL import ImageDraw
            d = ImageDraw.Draw(big)
            # Dim the grid alongside the image so a low Dim value reads as
            # near-black instead of being masked by a fixed-grey grid.
            g = max(2, int(20 * bf))
            gc = (g, g, g)
            for x in range(0, big.width + 1, scale):
                d.line([(x, 0), (x, big.height)], fill=gc)
            for y in range(0, big.height + 1, scale):
                d.line([(0, y), (big.width, y)], fill=gc)
        buf = BytesIO()
        big.save(buf, format="PNG")
        return buf.getvalue()


engine = Engine()
