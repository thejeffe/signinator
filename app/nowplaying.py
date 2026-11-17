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
SONGS_N = 10          # recent songs kept for picking song cues
CUES_MAX = 100
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
        # song cues: a look shown instead of everything while a chosen song plays
        self._song_key = ("", "")    # (title, artist) of the current song
        self._song_since = 0.0       # when it started playing
        self._logged_key = None      # last song put in the recent-songs log
        self._cue_on = None          # (cue id, song key) on the sign now
        self._cue_dismissed = None   # song key whose cue the user sent something over
        self._cue_try = None         # (cue, until): "Try it" from the page
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
            key = (self.info["title"], self.info["artist"])
            if key != self._song_key:
                self._song_key, self._song_since = key, now
            elif self.info["state"] == "playing" and was == "stopped":
                self._song_since = now          # the same song started again
            if self.info["state"] == "playing" and self.info["title"]:
                self._log_song(key, now)
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

    def dismiss(self, target="both"):
        """A manual send: stop showing the art (and the song cue, if it's on that display)
        until the track changes."""
        with self._lock:
            cue = self.cue_now() if self._cue_on is not None else None
            on = cue_displays(cue) if cue else ()
            if self._cue_on is not None and (target == "both" or target in on):
                self._cue_dismissed = self._cue_on[1]
                self._cue_try = None
                self._cue_on = None
                self.e.clear_cue()
            if self.active:
                self._dismissed = self._key()
                self.active = False
                self.e.clear_override()

    def refresh(self):
        """Settings or sign size changed: rebuild (or drop) the art."""
        with self._lock:
            if self._cue_on is not None:     # rebuild (or take down) a song cue too
                self._cue_on = ("rebuild", None)
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
        self._cue_step(now)
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

    def playing_art(self, firm=False) -> bool:
        """Music is playing and its cover is here (for displays set to "album art only":
        they show it the whole song, whatever the brief / hold settings). firm: also when
        something was sent over it (a display with album art switched on: art wins)."""
        if not self.cfg().get("enabled", True) or not self.art or self._art_stale:
            return False
        if not firm and self._key() == self._dismissed:
            return False
        return self.info["state"] == "playing" and time.time() - self.info["updated"] < WATCHDOG

    def led_scene(self):
        """The cover as the LED sign shows it (cached until the cover or the look changes)."""
        key = (self.art_sha, self.e.w, self.e.h, self.e.brightness, self.cfg().get("mode"),
               self.cfg().get("enhance"), self.cfg().get("lift"))
        if getattr(self, "_led_key", None) != key:
            self._led_scene, self._led_key = self._scene(), key
        return self._led_scene

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

    # ---- recent songs ----
    def _songs_path(self):
        import os
        return os.path.join(self.e._data_dir, "songs.json")

    def songs(self) -> list:
        import json
        try:
            with open(self._songs_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _log_song(self, key, now):
        """Put the song at the top of the recent-songs log (once per play)."""
        import json, os
        title, artist = key
        if key == self._logged_key and self.art_sha is None:
            return
        lst = self.songs()
        top = lst[0] if lst else None
        same_play = key == self._logged_key or (
            top and top.get("title") == title and not top.get("artist") and now - top.get("time", 0) < 30)
        if same_play and top:
            # the same play (the artist or the cover arrived a moment later): fill it in
            entry = top
            lst = lst[1:]
        else:
            entry = {"title": title, "artist": artist, "time": now}
            lst = [s for s in lst if (s.get("title"), s.get("artist")) != key]
        entry.update(title=title, artist=artist, album=self.info.get("album", ""))
        if self.art_sha and not self._art_stale:
            entry["art"] = self.art_sha
        new = [entry] + lst[:SONGS_N - 1]
        if new == self.songs() and key == self._logged_key:
            return
        self._logged_key = key
        try:
            p = self._songs_path()
            with open(p + ".tmp", "w") as f:
                json.dump(new, f)
            os.replace(p + ".tmp", p)
        except OSError as e:
            log.warning("couldn't keep the recent songs: %s", e)

    # ---- song cues ----
    def cues(self) -> list:
        c = self.e.cfg.get("cues")
        return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []

    def find_cue(self, title, artist):
        """The cue for this song: one can name the title, the artist, or both (the cue's
        words inside the song's). The most specific match wins: both, then title, then artist."""
        t, a = " %s " % norm(title), " %s " % norm(artist)
        if not t.strip() and not a.strip():
            return None
        best, best_score = None, 0
        for c in self.cues():
            if c.get("on") is False:
                continue
            ct, ca = norm(c.get("title")), norm(c.get("artist"))
            if not ct and not ca:
                continue
            if ct and " %s " % ct not in t:
                continue
            if ca and " %s " % ca not in a:
                continue
            score = (2 if ct else 0) + (1 if ca else 0)
            if score > best_score:
                best, best_score = c, score
        return best

    def try_cue(self, cue, seconds=10.0):
        with self._lock:
            self._cue_try = (cue, time.time() + seconds)
            self._cue_on = None
            self._cue_step(time.time())

    def stop_try(self):
        with self._lock:
            if self._cue_try:
                self._cue_try = None
                self._cue_step(time.time())

    def _cue_want(self, now):
        if self._cue_try:
            if now < self._cue_try[1]:
                return self._cue_try[0], ("try", self._cue_try[0].get("id"))
            self._cue_try = None
        key = self._song_key
        if self.info["state"] != "playing" or now - self.info["updated"] >= WATCHDOG:
            return None, None
        if key == self._cue_dismissed:
            return None, None
        cue = self.find_cue(*key)
        if cue is None:
            return None, None
        secs = cue.get("secs") or 0
        if secs and now - self._song_since > float(secs):
            return None, None
        return cue, key

    def _cue_step(self, now):
        cue, key = self._cue_want(now)
        want = (cue.get("id"), key) if cue else None
        if want == self._cue_on:
            return
        if cue is not None:
            try:
                scene = self.e.look_scene(cue["look"])
            except Exception as ex:  # noqa: BLE001 - e.g. the GIF was deleted
                log.warning("can't show the song cue for %r: %s", cue.get("title"), ex)
                scene = None
            on = cue_displays(cue)
            if scene is not None:            # (built even for the screen alone: checks the look works)
                self.e.set_cue(scene if "sign" in on else None, cue["look"], on)
                self._cue_on = want
                self.event("cue", title=cue.get("title"), song=self._song_key[0])
                log.info("song cue: %r -> %s", self._song_key[0] if key and key[0] != "try" else "(try)",
                         cue["look"].get("name") or cue["look"].get("text") or cue["look"].get("fx") or cue["look"].get("kind"))
                return
        if self._cue_on is not None:
            self.event("cue-off")
            self._cue_on = None
            self.e.clear_cue()
            if self.info["state"] != "playing" and not self.active:
                # the music stopped while the cue was up: whatever follows the music
                threading.Thread(target=self.e.music_ended, daemon=True).start()

    def cue_now(self):
        """The cue on the sign now, or None."""
        c, _ = self._cue_want(time.time()) if self._cue_on else (None, None)
        return c

    def cue_title(self):
        c = self.cue_now() or {}
        return c.get("title") or ("songs by " + c["artist"] if c.get("artist") else "")

    # ---- for the API / page ----
    def status(self) -> dict:
        with self._lock:
            return dict(self.info, active=self.active, has_art=bool(self.art),
                        cue=self.cue_title() or None, cue_id=(self.cue_now() or {}).get("id"),
                        colors=["#%02x%02x%02x" % c for c in self.colors],
                        art_sha=self.art_sha,
                        hold_until=self._stop_at if (self.active and self._stop_at) else None)


DISPLAYS = {"sign": ("sign",), "screen": ("screen",), "both": ("sign", "screen")}


def cue_displays(cue) -> tuple:
    """Where a cue shows: its "show_on" (sign | screen | both), the LED sign by default."""
    return DISPLAYS.get((cue or {}).get("show_on"), ("sign",))


def norm(s) -> str:
    """Song names for matching: no case, accents, apostrophes or punctuation
    (“It's Me, It's Verity” == "its me its verity")."""
    import re, unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['`\u2019]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


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
