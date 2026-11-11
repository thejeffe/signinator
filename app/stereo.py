"""Stereo power: turn a Home Assistant entity (the main stereo's switch, a smart plug, ...) on
when music starts and off a while after it stops.

Uses Home Assistant's REST API with a long-lived access token (Profile -> Security in HA).
Config: stereo: {enabled, url, token (write-only), entity, off_after_s, only_own}
  url          e.g. http://homeassistant.local:8123
  entity       e.g. switch.main_stereo (switch, light, input_boolean, media_player, fan, ...)
  off_after_s  seconds of no music before turning it off (0 = never turn off)
  only_own     only turn it off if Signinator turned it on (don't switch off a stereo
               someone turned on by hand)
Music = AirPlay playing or relayed audio (Engine.music_playing)."""
from __future__ import annotations
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("signinator.stereo")
DEFAULTS = {"enabled": False, "url": "http://homeassistant.local:8123", "token": "", "entity": "",
            "off_after_s": 300, "only_own": True}
DOMAINS = ("switch", "light", "input_boolean", "media_player", "fan", "remote", "climate", "humidifier")


class Stereo:
    POLL_S = 2.0

    def __init__(self, engine):
        self.e = engine
        self.status = {"state": None, "error": None, "last": None, "at": 0, "music": False}
        self._own = False                # we turned it on (and haven't turned it off since)
        self._music_at = 0.0             # last time music was seen
        self._was_music = False
        self._kick = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="stereo").start()

    def cfg(self) -> dict:
        c = dict(DEFAULTS)
        c.update(self.e.cfg.get("stereo") or {})
        return c

    def public_cfg(self) -> dict:
        c = self.cfg()
        c["token_set"] = bool(c.pop("token", ""))
        return c

    def settings_changed(self):
        self._kick.set()

    # ---- Home Assistant API ----
    def _api(self, method, path, body=None, timeout=6):
        c = self.cfg()
        if not c["url"] or not c["token"]:
            raise RuntimeError("set Home Assistant's address and a token first")
        req = urllib.request.Request(c["url"].rstrip("/") + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + c["token"],
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def entity_state(self, entity=None):
        entity = entity or self.cfg()["entity"]
        return self._api("GET", "/api/states/" + entity).get("state")

    def entities(self) -> list:
        """On/off entities in Home Assistant, for the settings page."""
        out = []
        for s in self._api("GET", "/api/states", timeout=10) or []:
            eid = s.get("entity_id", "")
            if eid.split(".")[0] in DOMAINS:
                out.append({"id": eid, "name": (s.get("attributes") or {}).get("friendly_name") or eid,
                            "state": s.get("state")})
        return sorted(out, key=lambda x: x["name"].lower())

    def turn(self, on: bool, why="") -> None:
        entity = self.cfg()["entity"]
        if not entity:
            raise RuntimeError("pick the stereo's entity first")
        self._api("POST", "/api/services/homeassistant/turn_%s" % ("on" if on else "off"), {"entity_id": entity})
        self.status.update(last=("%s %s" % ("on" if on else "off", why)).strip(), at=time.time(), error=None,
                           state="on" if on else "off")
        log.info("stereo %s turned %s %s", entity, "on" if on else "off", why)

    # ---- automatic on/off ----
    def _loop(self):
        while True:
            c = self.cfg()
            try:
                if c["enabled"] and c["entity"] and c["token"]:
                    self._step(c)
            except Exception as ex:  # noqa: BLE001 - show it on the page, try again next time
                self.status["error"] = str(getattr(ex, "reason", None) or ex)
            self._kick.wait(self.POLL_S)
            self._kick.clear()

    def _step(self, c):
        now = time.time()
        music = bool(self.e.music_playing())
        self.status["music"] = music
        if music:
            self._music_at = now
            if not self._was_music:                     # music just started
                state = self.entity_state()
                self.status["state"] = state
                if state != "on":
                    self.turn(True, "(music started)")
                    self._own = True
        elif self._was_music or self._music_at:
            off_after = int(c.get("off_after_s") or 0)
            if off_after and self._music_at and now - self._music_at >= off_after:
                self._music_at = 0.0
                if self._own or not c.get("only_own", True):
                    if self.entity_state() == "on":
                        self.turn(False, "(no music for %d min)" % max(1, off_after // 60))
                self._own = False
        self._was_music = music
