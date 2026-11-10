"""Now playing: show album art from AirPlay on the sign while music plays.

Sources call NowPlaying.update(): the AirPlay metadata reader (app/airplay_meta.py,
Signinator as a silent AirPlay 2 speaker) and the ESP32 speaker's firmware
(POST /api/nowplaying and /api/nowplaying/art). Both can run at once; the same
art arriving twice is ignored.

While music plays the art is shown as an engine *override*, so the content the
user sent (engine.scene / state.json) is never touched. After a pause or stop
the art stays for `hold` seconds, then the sign goes back to that content.
Sending something from the page while music plays wins until the track changes.
"""
from __future__ import annotations
import hashlib
import logging
import threading
import time

log = logging.getLogger("signinator.nowplaying")

DEFAULTS = {"enabled": True, "mode": "fill", "hold": 60, "token": "",
            "enhance": True, "lift": True,
            # "song": art stays up the whole song; "brief": art shows for
            # brief_s seconds when a song starts or changes, then the resting look
            "show": "song", "brief_s": 10,
            # when to apply art/track changes: "auto" = the audio delay (what the
            # speaker is playing now), or a number of milliseconds
            "art_delay": "auto",
            # hashes of images never to show (e.g. a placeholder Spotify sends
            # just before the real cover)
            "blocked": []}
HISTORY_N = 10
STATES = ("playing", "paused", "stopped")
WATCHDOG = 15 * 60   # no event at all for this long while "playing" -> give up


class NowPlaying:
    def __init__(self, engine):
        self.e = engine
        self._lock = threading.RLock()
        self.info = {"source": None, "state": "stopped", "title": "", "artist": "",
                     "album": "", "since": 0.0, "updated": 0.0}
        self.art = None
        self.art_sha = None
        self.art_type = "image/jpeg"
        self.active = False          # art is on the sign right now
        self._dismissed = None       # track key the user dismissed
        self._stop_at = None         # end of the hold after pause/stop
        self._flash_until = 0.0      # "brief" mode: art shows until this time
        # The new title arrives before the new cover: after a change to another album, the
        # cover we have belongs to the old song, so don't show it until the new one comes.
        self._art_stale = False
        self._art_at = 0.0           # when the current cover arrived
        import collections
        self.events = collections.deque(maxlen=400)   # what arrived and what was shown (for troubleshooting)
        self.colors = []             # the current cover's main colors
        try:                         # last saved cover colors, so music effects keep them
            import json, os
            from . import effects
            with open(os.path.join(engine._data_dir, "album_colors.json")) as f:
                saved = json.load(f)
            self.colors = [tuple(c) for c in saved["colors"]]
            effects.ALBUM = {"colors": self.colors, "version": saved.get("version", "saved")}
        except (OSError, ValueError, KeyError):
            pass
        threading.Thread(target=self._tick, daemon=True, name="nowplaying").start()

    # ---- config ----
    def cfg(self) -> dict:
        c = dict(DEFAULTS)
        c.update(self.e.cfg.get("nowplaying") or {})
        return c

    def _key(self):
        return (self.info["title"], self.info["artist"], self.art_sha)

    # ---- input from sources ----
    def delay_s(self) -> float:
        v = self.cfg().get("art_delay", "auto")
        if v == "auto":
            try:
                return float(self.e.light_delay_s())
            except Exception:  # noqa: BLE001
                return 0.0
        try:
            return max(0.0, min(10.0, int(v) / 1000.0))
        except (TypeError, ValueError):
            return 0.0

    def update(self, source: str, **kw):
        """Track info, art or play state from a source. Applied after delay_s(),
        so the sign changes when the speaker reaches that point in the music."""
        d = self.delay_s()
        self.event("got", src=source, delay=round(d, 2), **{k: (picture_id(v)[:8] if k == "art" else v)
                                                          for k, v in kw.items() if v is not None and k != "art_type"})
        if d <= 0.05:
            self._update_now(source, **kw)
        else:
            t = threading.Timer(d, self._update_now, args=(source,), kwargs=kw)
            t.daemon = True
            t.start()

    def event(self, kind, **kw):
        self.events.append(dict(t=round(time.time(), 3), ev=kind, **kw))

    def _update_now(self, source: str, state=None, title=None, artist=None, album=None,
                    art: bytes | None = None, art_type: str | None = None):
        with self._lock:
            now = time.time()
            self.event("apply", src=source, state=state, title=title, artist=artist, album=album,
                       art=picture_id(art)[:8] if art else None)
            self.info["source"] = source or "unknown"
            self.info["updated"] = now
            changed = False
            old_album = (self.info["artist"], self.info["album"])
            for k, v in (("title", title), ("artist", artist), ("album", album)):
                if v is not None and str(v) != self.info[k]:
                    self.info[k] = str(v)[:200]
                    changed = True
            if changed and (self.info["artist"], self.info["album"]) != old_album and now - self._art_at > 3.0:
                # another album (and its cover didn't just arrive first): the cover is the old song's
                self._art_stale = True
            if art:
                sha = picture_id(art)
                self._remember(sha, art, title)
                if sha in (self.cfg().get("blocked") or []):
                    log.info("ignoring hidden album art %s", sha[:10])
                    art = None
            if art:
                if self._art_stale and self.info["state"] == "playing":
                    # the new song's cover arrived: start the brief art time from now
                    self._flash_until = now + max(1.0, float(self.cfg().get("brief_s", 10)))
                    changed = True
                self._art_stale = False
                self._art_at = now
                if sha != self.art_sha:
                    self.art, self.art_sha = art, sha
                    self.art_type = art_type or _sniff(art)
                    changed = True
                    self._album_colors(art, sha)
            was = self.info["state"]
            if state in STATES:
                if state != self.info["state"]:
                    self.info["state"], self.info["since"] = state, now
            elif changed and self.info["state"] == "stopped":
                # new art or a new title with no state means a track just started
                self.info["state"], self.info["since"] = "playing", now
            if self.info["state"] == "playing" and (changed or was == "stopped"):
                # a song started or changed: "brief" mode shows the art for a while
                self._flash_until = now + max(1.0, float(self.cfg().get("brief_s", 10)))
            if self.info["state"] == "playing":
                self._stop_at = None
            elif self._stop_at is None:
                self._stop_at = now + max(0, float(self.cfg().get("hold", 60)))
            if changed:
                log.info("now playing (%s): %s - %s [%s]%s", source, self.info["artist"],
                         self.info["title"], self.info["state"],
                         " +art %d bytes" % len(self.art) if art else "")
            self._apply(changed)

    # ---- recent covers (to pick ones to hide) ----
    def _hist_dir(self):
        import os
        d = os.path.join(self.e._data_dir, "art_history")
        os.makedirs(d, exist_ok=True)
        return d

    def _hist_load(self):
        import json, os
        try:
            with open(os.path.join(self._hist_dir(), "index.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _remember(self, sha, art, title):
        import json, os
        try:
            d = self._hist_dir()
            hist = [h for h in self._hist_load() if h.get("sha") != sha]
            hist.insert(0, {"sha": sha, "id": "pic1", "title": title or self.info.get("title", ""),
                            "artist": self.info.get("artist", ""), "time": time.time(),
                            "type": _sniff(art), "bytes": len(art)})
            for old in hist[HISTORY_N:]:
                try:
                    os.remove(os.path.join(d, old["sha"]))
                except OSError:
                    pass
            hist = hist[:HISTORY_N]
            p = os.path.join(d, sha)
            if not os.path.exists(p):
                with open(p, "wb") as f:
                    f.write(art)
            with open(os.path.join(d, "index.json.tmp"), "w") as f:
                json.dump(hist, f)
            os.replace(os.path.join(d, "index.json.tmp"), os.path.join(d, "index.json"))
        except OSError as e:
            log.warning("couldn't keep art history: %s", e)

    def migrate_ids(self):
        """Once: rename saved covers (and hidden ones) from byte names to picture names."""
        import json, os
        try:
            d = self._hist_dir()
            blocked = list(self.cfg().get("blocked") or [])
            with self._lock:
                hist = self._hist_load()
                changed = False
                for h in hist:
                    p = os.path.join(d, h["sha"])
                    if h.get("id") == "pic1" or not os.path.exists(p):
                        continue
                    with open(p, "rb") as f:
                        pid = picture_id(f.read())
                    if h["sha"] in blocked and pid not in blocked:
                        blocked.append(pid)
                    if pid != h["sha"]:
                        os.replace(p, os.path.join(d, pid))
                        h["sha"] = pid
                    h["id"] = "pic1"
                    changed = True
                if changed:
                    seen, hist2 = set(), []
                    for h in hist:                      # the same picture twice: keep the newest
                        if h["sha"] not in seen:
                            seen.add(h["sha"]); hist2.append(h)
                    with open(os.path.join(d, "index.json.tmp"), "w") as f:
                        json.dump(hist2, f)
                    os.replace(os.path.join(d, "index.json.tmp"), os.path.join(d, "index.json"))
            if blocked != list(self.cfg().get("blocked") or []):
                self.e.update_config({"nowplaying": {"blocked": blocked[-100:]}})
                log.info("hidden covers now matched by picture (%d)", len(blocked))
        except Exception as e:  # noqa: BLE001
            log.warning("cover history migration: %s", e)

    def history(self) -> list:
        blocked = set(self.cfg().get("blocked") or [])
        return [dict(h, blocked=h["sha"] in blocked) for h in self._hist_load()]

    def history_image(self, sha):
        import os, re
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            return None
        p = os.path.join(self._hist_dir(), sha)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def set_blocked(self, sha, blocked: bool):
        """Hide (or unhide) an image. Hiding the one on the sign takes it down now."""
        lst = [s for s in (self.cfg().get("blocked") or []) if s != sha]
        if blocked:
            lst.append(sha)
        self.e.update_config({"nowplaying": {"blocked": lst[-100:]}})
        with self._lock:
            if blocked and sha == self.art_sha:
                self.art, self.art_sha = None, None
                self._apply(True)
        return lst

    def _album_colors(self, art, sha):
        """Share the cover's main colors with effects in album-color mode."""
        from . import effects
        from .albumcolors import extract
        try:
            cols = extract(art)
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't read album colors: %s", e)
            return
        self.colors = cols
        effects.ALBUM = {"colors": cols, "version": sha}
        try:
            import json, os
            with open(os.path.join(self.e._data_dir, "album_colors.json"), "w") as f:
                json.dump({"colors": cols, "version": sha}, f)
        except OSError as e:
            log.warning("couldn't save album colors: %s", e)

    def dismiss(self):
        """A manual send: stop showing the art until the track changes."""
        with self._lock:
            if self.active:
                self._dismissed = self._key()
                self.active = False
                self.e.clear_override()

    def refresh(self):
        """Settings or sign size changed: rebuild (or drop) the art."""
        with self._lock:
            self._apply(True)

    # ---- decide what the sign shows ----
    def _want(self, now: float) -> bool:
        c = self.cfg()
        if not c.get("enabled", True) or not self.art or self._art_stale:
            return False
        if self._key() == self._dismissed:
            return False
        if c.get("show") == "brief":
            return self.info["state"] == "playing" and now < self._flash_until
        if self.info["state"] == "playing":
            return now - self.info["updated"] < WATCHDOG
        return self.active and self._stop_at is not None and now < self._stop_at

    def _apply(self, changed: bool):
        now = time.time()
        want = self._want(now)
        if want and (changed or not self.active):
            scene = self._scene()
            if scene is not None:
                self.e.set_override(scene)
                self.active = True
                self.event("show", art=(self.art_sha or "")[:8], title=self.info["title"])
        elif not want and self.active:
            self.event("hide", stale=self._art_stale, title=self.info["title"])
            self.active = False
            self.e.clear_override()
            if (self.info["state"] != "playing" or now - self.info["updated"] >= WATCHDOG
                    or self.cfg().get("show") == "brief"):
                # music stopped, or the brief art time is over: back to resting
                threading.Thread(target=self.e.music_ended, daemon=True).start()

    def _scene(self):
        from .render import SolidScene, load_image_bytes, prepare_for_leds
        try:
            c = self.cfg()
            mode = c.get("mode", "fill")
            frame = prepare_for_leds(
                load_image_bytes(self.art), self.e.w, self.e.h,
                mode=mode if mode in ("fill", "fit", "stretch") else "fill",
                enhance=bool(c.get("enhance", True)), lift=bool(c.get("lift", True)),
                bri=self.e.brightness, gamma=self.e.gamma,
                steps=0.25 if self.e.dither_cfg()["mode"] in ("spatial", "temporal") else 1.0)
            scene = SolidScene(self.e.w, self.e.h)
            scene.frame = frame
            return scene
        except Exception as e:  # noqa: BLE001
            log.warning("can't show album art: %s", e)
            return None

    def _tick(self):
        while True:
            time.sleep(1.0)
            with self._lock:
                try:
                    self._apply(False)
                except Exception as e:  # noqa: BLE001
                    log.warning("now playing tick: %s", e)

    # ---- for the API / page ----
    def status(self) -> dict:
        with self._lock:
            return dict(self.info, active=self.active, has_art=bool(self.art),
                        colors=["#%02x%02x%02x" % c for c in self.colors],
                        art_sha=self.art_sha,
                        hold_until=self._stop_at if (self.active and self._stop_at) else None)


def picture_id(art: bytes) -> str:
    """Name a cover by its picture, not its bytes. AirPlay covers can arrive padded to a fixed
    size with leftovers of the previous image, so the same picture (e.g. Spotify's grey
    placeholder) has different bytes each time; its decoded pixels are the same."""
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(art)) as im:
            small = im.convert("RGB").resize((16, 16), Image.BILINEAR)
        return hashlib.sha1(b"pic1" + small.tobytes()).hexdigest()
    except Exception:  # noqa: BLE001 - not an image we can read: fall back to the bytes
        return hashlib.sha1(art).hexdigest()


def _sniff(b: bytes) -> str:
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "application/octet-stream"
