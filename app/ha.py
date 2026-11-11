"""Home Assistant over MQTT: Signinator shows up in HA as a device with
controls and sensors (MQTT discovery), no YAML needed in HA.

Topics (base = "signinator" unless changed):
  <base>/status          online / offline (last will)
  <base>/state/<key>     retained states
  <base>/cmd/<key>       commands from HA
  <prefix>/device/<base>/config   the discovery message (prefix = "homeassistant")
Commands run on this bridge's own thread through the same Engine methods the
web page uses, so HA and the page behave the same."""
from __future__ import annotations
import json
import logging
import re
import threading
import time

from . import mqttc

log = logging.getLogger("signinator.ha")

DEFAULTS = {"enabled": False, "host": "homeassistant.local", "port": 1883, "username": "",
            "password": "", "base": "signinator", "prefix": "homeassistant"}
BASE_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
MODES = [("on", "On"), ("off", "Off"), ("wled", "WLED"), ("auto", "Auto")]
ROUTES = [("all", "Everything"), ("art", "Album art only"), ("noart", "No album art")]
DIMMING = [("off", "Off"), ("spatial", "Spatial"), ("hue", "Hue"), ("temporal", "Temporal"), ("wled", "In WLED")]
NONE = "—"                       # "nothing of this kind is showing"
TEXT_KEYS = ("color", "bg", "speed", "scroll", "emoji", "effect", "gap_mode", "color2",
             "size", "font", "height", "smooth", "align")


class HABridge:
    STATE_EVERY = 2.0          # seconds between change checks
    OPTIONS_EVERY = 30.0       # GIF list / speakers: re-check this often

    def __init__(self, engine):
        self.e = engine
        self.status = {"state": "off", "error": None, "since": time.time()}
        self._kick = threading.Event()
        self._cl = None
        self._sent = {}
        self._disc = None
        self._disc_key = None      # (prefix, base) the discovery message went to
        self._art_sha = None
        self._last_text = {}
        threading.Thread(target=self._loop, daemon=True, name="ha-mqtt").start()

    # ---- settings ----
    def cfg(self) -> dict:
        c = dict(DEFAULTS)
        c.update(self.e.cfg.get("mqtt") or {})
        if not BASE_RE.match(str(c.get("base") or "")):
            c["base"] = DEFAULTS["base"]
        if not BASE_RE.match(str(c.get("prefix") or "")):
            c["prefix"] = DEFAULTS["prefix"]
        return c

    def public_cfg(self) -> dict:
        c = self.cfg()
        c["password_set"] = bool(c.pop("password", ""))
        return c

    def settings_changed(self):
        self._kick.set()           # reconnect with the new settings

    def _set(self, state, error=None):
        since = self.status["since"] if state == self.status["state"] else time.time()
        self.status = {"state": state, "error": error, "since": since}

    # ---- connection ----
    def _loop(self):
        while True:
            self._kick.clear()
            c = self.cfg()
            if not c["enabled"] or not c["host"]:
                self._set("off")
                self._kick.wait()
                continue
            try:
                self._session(c)
            except Exception as e:  # noqa: BLE001 - show any failure on the page
                msg = str(e) or e.__class__.__name__
                if self.status["state"] != "error" or self.status["error"] != msg:
                    log.warning("Home Assistant (MQTT %s:%s): %s", c["host"], c["port"], msg)
                self._set("error", msg)
            finally:
                if self._cl is not None:
                    self._cl.close(clean=False)
                    self._cl = None
            if not self._kick.is_set():
                self._kick.wait(15)       # retry later (or now, if settings change)

    def _session(self, c):
        base, prefix = c["base"], c["prefix"]
        self.base, self.prefix = base, prefix
        self._set("connecting")
        cl = mqttc.Client(c["host"], int(c["port"]), "signinator-" + base, c["username"],
                          c["password"], will=(base + "/status", b"offline", True), keepalive=30)
        cl.connect()
        self._cl = cl
        # a new topic name: remove the device announced under the old one
        if self._disc_key and self._disc_key != (prefix, base):
            cl.publish("%s/device/%s/config" % self._disc_key, b"", retain=True)
        self._sent, self._disc, self._art_sha = {}, None, None
        cl.subscribe([base + "/cmd/#", prefix + "/status"])
        self._publish_discovery()
        cl.publish(base + "/status", b"online", retain=True)
        self._publish_states()
        self._set("connected")
        log.info("Home Assistant: connected to MQTT %s:%s as %s", c["host"], c["port"], base)
        next_state = next_opts = time.monotonic()
        while not self._kick.is_set():
            for topic, payload in cl.poll(0.5):
                self._on_message(topic, payload)
            now = time.monotonic()
            if now >= next_opts:
                next_opts = now + self.OPTIONS_EVERY
                self._publish_discovery()
                threading.Thread(target=self._refresh_speakers, daemon=True).start()
            if now >= next_state:
                next_state = now + self.STATE_EVERY
                self._publish_states()
        cl.publish(base + "/status", b"offline", retain=True)
        cl.close()
        self._cl = None

    def _refresh_speakers(self):
        try:
            self.e.relay.speakers()          # cached; re-scans every few minutes
        except Exception:  # noqa: BLE001
            pass

    def _on_message(self, topic, payload):
        if topic == self.prefix + "/status":
            if payload.strip() == b"online":      # HA restarted: announce again
                self._disc, self._sent, self._art_sha = None, {}, None
                self._publish_discovery()
                self._publish_states()
            return
        key = topic[len(self.base) + 5:]
        value = payload.decode("utf-8", "replace").strip()
        try:
            self.command(key, value)
        except Exception as e:  # noqa: BLE001
            log.warning("Home Assistant command %s=%r failed: %s", key, value[:80], e)
        self._publish_states()

    # ---- commands ----
    def _on(self):
        """New content from Home Assistant: wake a display that's switched on (never one that's off)."""
        self.e.wake()

    def command(self, key: str, v: str):
        e = self.e
        if key == "light":
            j = json.loads(v) if v.startswith("{") else {"state": v.upper()}
            if "brightness" in j:
                e.set_brightness(int(j["brightness"]))
            if j.get("state") == "ON":
                e.set_led(True)               # in Auto: lights up while music plays
            elif j.get("state") == "OFF":
                e.set_led(False)              # stays off, music or not
        elif key == "brightness":
            e.set_brightness(max(0, min(255, int(float(v)))))
        elif key in ("route_sign", "route_screen"):
            m = dict((lab, k) for k, lab in ROUTES).get(v)
            if m:
                e.update_config({"routing": {key[6:]: m}})
        elif key == "auto":
            e.set_auto(v.upper() == "ON")
        elif key == "screen":
            e.update_config({"mirror": {"enabled": v.upper() == "ON"}})
        elif key == "mode":
            m = dict((lab, k) for k, lab in MODES).get(v)
            if m:
                e.set_mode(m)
        elif key == "text":
            st = e.state if e.state.get("kind") == "text" else self._last_text
            args = {k: st[k] for k in TEXT_KEYS if k in st}
            e.set_text(v[:1000], **args)
            self._on()
        elif key == "effect":
            from .effects import catalog
            fx = {x["name"]: x["id"] for x in catalog()["effects"]}.get(v)
            if fx:
                st = e.state if e.state.get("kind") == "effect" else {}
                p = {k: st[k] for k in ("speed", "intensity", "c1", "c2", "c3") if k in st}
                p["fx"] = fx
                e.set_effect(p)
                self._on()
        elif key == "gif":
            if v != NONE and e.gifs.path(v):
                st = e.state if e.state.get("kind") == "gif" else {}
                e.set_gif(v, st.get("mode", "fit"), st.get("rate", 1.0), bool(st.get("beat")),
                          st.get("bpl", "auto"))
                self._on()
        elif key == "rest":
            e.show_rest()
        elif key == "dismiss":
            e.nowplaying.dismiss()
        elif key == "relay":
            target = "" if v in ("Off", "") else v[:120]
            e.update_config({"relay": {"target": target}})
        elif key == "dimming":
            m = dict((lab, k) for k, lab in DIMMING).get(v)
            if m:
                e.update_config({"dither": {"mode": m, "enabled": m != "off"}})
        elif key == "fps_auto":
            e.update_config({"fps_auto": v.upper() == "ON"})
        elif key == "fps_max":
            e.update_config({"fps": max(5, min(240, int(float(v))))})

    # ---- states ----
    def _showing(self) -> str:
        e = self.e
        if e.released:
            return "WLED's own effects"
        if not e.power:
            return "Off"
        if e._override is not None and getattr(e.nowplaying, "active", False):
            return "Album art"
        st = e.state
        k = st.get("kind")
        if k == "text":
            return ("Text: " + " / ".join(str(st.get("text", "")).splitlines()))[:200]
        if k == "effect":
            return "Effect: " + self._fx_label(st.get("fx"))
        if k == "gif":
            return "GIF: " + str(st.get("name", ""))
        if k == "test":
            return "Test pattern"
        return "Blank"

    @staticmethod
    def _fx_label(fx):
        from .effects import EFFECTS
        cls = EFFECTS.get(fx)
        return cls.label if cls else str(fx or "")

    def states(self) -> dict:
        e = self.e
        st = e.state
        if st.get("kind") == "text":
            self._last_text = dict(st)
        np = e.nowplaying.status()
        mon = e.monitor.status or {}
        on = bool(getattr(e, "led_enabled", True)) and not e.released   # the LED's switch, not the music
        fps = mon.get("fps")
        return {
            "brightness": str(e.brightness),
            "screen": "ON" if e.mirrors.cfg()["enabled"] else "OFF",
            "auto": "ON" if getattr(e, "auto", False) else "OFF",
            "route_sign": dict(ROUTES)[e.route("sign")],
            "route_screen": dict(ROUTES)[e.route("screen")],
            "light": json.dumps({"state": "ON" if on else "OFF", "brightness": e.brightness,
                                 "color_mode": "brightness"}),
            "mode": dict(MODES).get(e.mode, "On"),
            "text": str(st.get("text", ""))[:255] if st.get("kind") == "text" else "",
            "effect": self._fx_label(st.get("fx")) if st.get("kind") == "effect" else NONE,
            "gif": str(st.get("name", "")) if st.get("kind") == "gif" else NONE,
            "showing": self._showing(),
            "title": np.get("title") or "None",
            "artist": np.get("artist") or "None",
            "music": np.get("state") or "stopped",
            "relay": (e.relay.cfg().get("target") or "Off"),
            "wled_fps": "None" if fps is None else str(fps),
            "wled_online": "ON" if mon.get("online") else "OFF",
            "dimming": dict(DIMMING).get(e.dither_cfg()["mode"], "Off"),
            "fps_auto": "ON" if e.fps_cfg()["auto"] else "OFF",
            "fps_max": str(int(e.cfg.get("fps", 40))),
        }

    def _publish_states(self):
        cl = self._cl
        if cl is None:
            return
        for k, v in self.states().items():
            if self._sent.get(k) != v:
                cl.publish("%s/state/%s" % (self.base, k), v, retain=True)
                self._sent[k] = v
        np = self.e.nowplaying
        if np.art and np.art_sha != self._art_sha:
            cl.publish(self.base + "/state/art", np.art, retain=True)
            self._art_sha = np.art_sha

    # ---- discovery ----
    def discovery(self, local_ip="") -> dict:
        e, b = self.e, self.base
        from .effects import catalog
        fx_names = [x["name"] for x in catalog()["effects"]]
        gifs = [g["name"] for g in e.gifs.list()]
        target = e.relay.cfg().get("target") or ""
        speakers = [s["name"] for s in (getattr(e.relay, "_speakers", None) or [])]
        relay_opts = ["Off"] + sorted(set(speakers + ([target] if target else [])), key=str.lower)
        cmps = {}

        def add(key, platform, name, cmd=True, state=True, **kw):
            d = {"p": platform, "name": name, "unique_id": "%s_%s" % (b, key)}
            if cmd:
                d["command_topic"] = "%s/cmd/%s" % (b, key)
            if state:
                d["state_topic"] = "%s/state/%s" % (b, key)
            d.update(kw)
            cmps[key] = d

        add("light", "light", "LED sign", schema="json", brightness_scale=255,
            supported_color_modes=["brightness"], icon="mdi:sign-text")
        add("brightness", "number", "LED brightness", min=0, max=255, step=1, mode="slider",
            icon="mdi:brightness-6")
        add("screen", "switch", "LCD screen", icon="mdi:monitor")
        add("auto", "switch", "Follow music (Auto)", icon="mdi:music-circle")
        add("route_sign", "select", "LED sign shows", options=[lab for _, lab in ROUTES], icon="mdi:sign-text")
        add("route_screen", "select", "LCD screen shows", options=[lab for _, lab in ROUTES], icon="mdi:monitor")
        add("mode", "select", "Power mode", options=[lab for _, lab in MODES], icon="mdi:power-settings")
        add("text", "text", "Message", max=255, icon="mdi:message-text")
        add("effect", "select", "Effect", options=[NONE] + fx_names, icon="mdi:auto-fix")
        add("gif", "select", "GIF", options=[NONE] + gifs, icon="mdi:file-gif-box")
        add("rest", "button", "Show resting look", state=False, icon="mdi:sleep")
        add("dismiss", "button", "Hide album art", state=False, icon="mdi:image-off")
        add("showing", "sensor", "Showing", cmd=False, icon="mdi:television-play")
        add("title", "sensor", "Song", cmd=False, icon="mdi:music")
        add("artist", "sensor", "Artist", cmd=False, icon="mdi:account-music")
        add("music", "sensor", "Music", cmd=False, icon="mdi:play-pause")
        add("art", "image", "Album art", cmd=False, state=False, image_topic=b + "/state/art",
            content_type=e.nowplaying.art_type or "image/jpeg")
        add("relay", "select", "Music relay speaker", options=relay_opts,
            icon="mdi:speaker-wireless", entity_category="config")
        add("dimming", "select", "Dimming", options=[lab for _, lab in DIMMING],
            icon="mdi:brightness-6", entity_category="config")
        add("fps_auto", "switch", "Auto frame rate", icon="mdi:speedometer", entity_category="config")
        add("fps_max", "number", "Max frame rate", min=5, max=240, step=1, mode="box",
            unit_of_measurement="fps", icon="mdi:speedometer", entity_category="config")
        add("wled_fps", "sensor", "WLED frame rate", cmd=False, unit_of_measurement="fps",
            state_class="measurement", icon="mdi:speedometer", entity_category="diagnostic")
        add("wled_online", "binary_sensor", "WLED controller", cmd=False,
            device_class="connectivity", entity_category="diagnostic")
        dev = {"ids": [b], "name": "Signinator", "mf": "Signinator",
               "mdl": "%dx%d LED sign" % (e.w, e.h)}
        if local_ip:
            port = int((e.cfg.get("server") or {}).get("port", 8080))
            dev["cu"] = "http://%s:%d/" % (local_ip, port)
        origin = {"name": "Signinator"}
        if dev.get("cu"):
            origin["url"] = dev["cu"]
        return {"dev": dev, "o": origin,
                "availability_topic": b + "/status", "cmps": cmps}

    def _publish_discovery(self):
        cl = self._cl
        if cl is None:
            return
        payload = json.dumps(self.discovery(cl.local_ip()), separators=(",", ":"))
        if payload != self._disc:
            cl.publish("%s/device/%s/config" % (self.prefix, self.base), payload, retain=True)
            self._disc = payload
            self._disc_key = (self.prefix, self.base)
