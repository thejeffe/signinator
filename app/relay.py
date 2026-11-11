"""Music relay: forward the AirPlay audio Signinator receives to another AirPlay
speaker (e.g. the "Marantz Stereo" ESP32), so the phone only has to pick
"Signinator" and the sign still shows the album art.

shairport-sync writes the music as raw PCM (44.1 kHz, 16-bit, stereo) into a
FIFO (pipe backend). This module always drains that FIFO. When a target speaker
is set it pipes the audio into `cliraop` (philippe44/libraop, an AirPlay 1
sender), started when music starts and stopped a few seconds after it stops.

Audio is sent on as Apple Lossless (bit-exact after shairport-sync's output).

Volume (relay.volume_mode):
  "audio" (default, the setup that worked): shairport-sync applies the phone's
     slider to the audio over `volume_range_db` (30 dB); cliraop asks the
     speaker to play at `relay.volume`.
  "speaker": the audio passes at full level and the phone's slider is sent to
     the speaker's own volume (POST http://<speaker>/api/volume {"db": ...};
     needs the airplay-esp32 signinator-nowplaying firmware).
"""
from __future__ import annotations
import logging
import os
import re
import select
import shutil
import socket
import subprocess
import threading
import time
import json
import math
import urllib.request
import fcntl
import struct
import termios

log = logging.getLogger("signinator.relay")

CLIRAOP = os.environ.get("SIGNINATOR_CLIRAOP", "/usr/local/bin/cliraop")  # override for tests
DEFAULTS = {"target": "", "volume": 60, "codec": "alac", "volume_mode": "audio",
            "volume_range_db": 30,
            "audio_pipe": "/var/lib/signinator-airplay/audio"}
IDLE_STOP = 4.0          # seconds of no audio before the sender is stopped
SPS_CONF = os.environ.get("SIGNINATOR_SPS_CONF", "/etc/shairport-sync.conf")
VOLUME_URL = os.environ.get("SIGNINATOR_VOLUME_URL", "http://%s/api/volume")  # override for tests
NAME_SUFFIX = " w/Art"
BASE_NAME = "Signinator"
DISCOVER_TTL = 30.0


def _unescape(s: str) -> str:
    """avahi-browse -p escapes bytes as \\DDD (decimal)."""
    raw = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s)
    try:
        return raw.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return raw


def _txt(field: str) -> dict:
    return dict(kv.split("=", 1) for kv in re.findall(r'"([^"]*)"', field) if "=" in kv)


def _my_ips() -> set:
    ips = {"127.0.0.1"}
    try:
        ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None))
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        ips.update(out.split())
    except (OSError, subprocess.SubprocessError):
        pass
    return ips


class Relay:
    def __init__(self, get_cfg, own_name="Signinator", on_audio=None):
        self._get_cfg = get_cfg
        self._on_audio = on_audio      # every audio chunk, for music-reactive effects
        self.own_name = own_name
        self._lock = threading.Lock()
        self._proc = None
        self._proc_target = None
        self._speakers = []
        self._speakers_at = 0.0
        self.status = {"state": "off", "target": "", "address": None, "error": None,
                       "since": time.time(), "installed": os.access(CLIRAOP, os.X_OK),
                       "airplay_name": None, "phone_db": None, "volume_forward": None,
                       "latency_ms": None, "backlog_ms": 0}
        self._backlog_ms = 0.0
        self._wlock = threading.Lock()
        self._last_audio = 0.0
        self._last_fifo = 0.0
        self._sync_until = 0.0
        self._sync_thread = None
        self._sync_done = None
        self.phone_db = None            # phone's AirPlay volume: 0 .. -30 dB, -144 = mute
        self._vol_kick = threading.Event()
        threading.Thread(target=self._vol_loop, daemon=True, name="relay-volume").start()
        threading.Thread(target=self._loop, daemon=True, name="relay").start()
        threading.Thread(target=self._name_loop, daemon=True, name="relay-name").start()
        threading.Thread(target=self._watch_loop, daemon=True, name="relay-watch").start()

    # ---- the name phones see: "<target> w/Art", or "Signinator" when off ----
    def airplay_name(self) -> str:
        t = (self.cfg().get("target") or "").replace('"', "").replace("\\", "").strip()
        return (t + NAME_SUFFIX) if t else BASE_NAME

    # shairport-sync `general` settings Signinator manages: pass the audio
    # through untouched (only delay may be added).
    _REMOVE = ("resync_threshold_in_seconds", "drift_tolerance_in_seconds")

    def _speaker_volume(self) -> bool:
        return self.cfg().get("volume_mode") == "speaker"

    def _wanted_general(self) -> dict:
        rng = max(10, min(60, int(self.cfg().get("volume_range_db", 30) or 30)))
        return {
            "name": '"%s"' % self.airplay_name(),
            # "audio": phone slider scales the audio (worked well); "speaker": full
            # level here, the slider goes to the speaker's own volume
            "ignore_volume_control": '"yes"' if self._speaker_volume() else '"no"',
            "volume_range_db": str(rng),
        }

    def sync_name(self) -> bool:
        """Keep shairport-sync's name ("<target> w/Art") and volume handling in
        line with the relay settings; restart it if anything changed.
        Only touches a config written by Signinator's install script."""
        try:
            with open(SPS_CONF) as f:
                conf = f.read()
        except OSError:
            return False
        if "signinator" not in conf:
            return False
        g = re.search(r"general\s*=\s*\{(.*?)\n\};", conf, re.S)
        if not g:
            return False
        body = g.group(1)
        cur = re.search(r'\bname\s*=\s*"([^"]*)"', body)
        self.status["airplay_name"] = cur.group(1) if cur else None
        new = body
        for key, val in self._wanted_general().items():
            pat = re.compile(r"(^\s*)(?://\s*)?%s\s*=\s*[^;\n]*;" % re.escape(key), re.M)
            line = "%s = %s;" % (key, val)
            if pat.search(new):
                new = pat.sub(lambda m: m.group(1) + line, new, count=1)
            else:
                new = new.rstrip("\n") + "\n  " + line
        for key in self._REMOVE:
            new = re.sub(r"^\s*%s\s*=\s*[^;\n]*;[^\n]*\n?" % re.escape(key), "", new, flags=re.M)
        if new == body:
            return False
        tmp = SPS_CONF + ".signinator.tmp"
        try:
            with open(tmp, "w") as f:
                f.write(conf[:g.start(1)] + new + conf[g.end(1):])
            os.chmod(tmp, os.stat(SPS_CONF).st_mode & 0o777)
            os.replace(tmp, SPS_CONF)
        except OSError as e:
            log.warning("can't update the AirPlay speaker settings: %s", e)
            return False
        want = self.airplay_name()
        log.info("AirPlay speaker settings updated (name %r); restarting shairport-sync", want)
        self.status["airplay_name"] = want
        if not os.environ.get("SIGNINATOR_SPS_CONF"):      # tests use a scratch file
            try:
                subprocess.run(["systemctl", "restart", "shairport-sync"], timeout=30,
                               capture_output=True)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("couldn't restart shairport-sync: %s", e)
        return True

    def _name_loop(self):
        # at start, and every 30 s (e.g. after the install script rewrote the config)
        while True:
            try:
                self.sync_name()
            except Exception as e:  # noqa: BLE001
                log.warning("name sync: %s", e)
            time.sleep(30)

    def cfg(self) -> dict:
        c = dict(DEFAULTS)
        c.update(self._get_cfg() or {})
        return c

    def _set(self, state, **kw):
        if state != self.status["state"]:
            kw["since"] = time.time()
        self.status.update(state=state, **kw)

    # ---- speakers on the network ----
    def speakers(self, fresh=False) -> list:
        if not fresh and time.time() - self._speakers_at < DISCOVER_TTL:
            return self._speakers
        if not shutil.which("avahi-browse"):
            return []
        try:
            out = subprocess.run(["avahi-browse", "-rpt", "_raop._tcp"], capture_output=True,
                                 text=True, timeout=8).stdout
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("speaker discovery failed: %s", e)
            return self._speakers
        mine = _my_ips()
        found = {}
        for line in out.splitlines():
            f = line.split(";")
            if len(f) < 10 or f[0] != "=" or f[2] != "IPv4":
                continue
            svc = _unescape(f[3])
            name = svc.split("@", 1)[1] if "@" in svc else svc
            addr, port, txt = f[7], int(f[8] or 0), _txt(f[9])
            if addr in mine or name == self.own_name or name.endswith(NAME_SUFFIX):
                continue    # never relay to ourselves
            found[name] = {"name": name, "address": addr, "port": port,
                           "model": txt.get("am", ""),
                           "encrypted_only": "0" not in txt.get("et", "0").split(",")}
        self._speakers = sorted(found.values(), key=lambda s: s["name"].lower())
        self._speakers_at = time.time()
        return self._speakers

    def _resolve(self, name):
        for fresh in (False, True):
            for s in self.speakers(fresh=fresh):
                if s["name"] == name:
                    return s
        return None

    # ---- the sender process ----
    def _start(self, target):
        if not os.access(CLIRAOP, os.X_OK):
            self._set("error", error="cliraop isn't installed (run deploy/install-airplay.sh)")
            return None
        sp = self._resolve(target)
        if not sp:
            self._set("error", error="can't find speaker “%s” on the network" % target)
            return None
        if self._speaker_volume() and self.phone_db is not None:   # start at the phone's volume
            vol = 0 if self.phone_db <= -30 else round((self.phone_db + 30) / 30 * 100)
        else:
            vol = max(0, min(100, int(self.cfg().get("volume", 60))))
        # -d 3 (info) so cliraop reports the speaker's playback latency
        cmd = [CLIRAOP, "-p", str(sp["port"]), "-v", str(vol), "-d", "3"]
        # "alac": real Apple Lossless frames, what iPhones send. The ESP32 speaker's
        # decoder rejects cliraop's default (PCM in uncompressed ALAC frames) with
        # "Failed to decode alac frame, error:-50" and plays silence.
        if self.cfg().get("codec", "alac") != "pcm":
            cmd.append("-a")
        if sp["encrypted_only"]:
            cmd.append("-e")
        cmd += [sp["address"], "-"]
        try:
            # lower priority: the prebuilt cliraop busy-waits between packets, so
            # keep it from taking CPU away from Signinator and the AirPlay receiver
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, preexec_fn=lambda: os.nice(10))
        except OSError as e:
            self._set("error", error=str(e))
            return None
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        log.info("relaying music to %s (%s:%d)", target, sp["address"], sp["port"])
        self._set("playing", target=target, address="%s:%d" % (sp["address"], sp["port"]),
                  error=None)
        return proc

    # ---- heartbeat: is the speaker still there? ----
    WATCH_S = 5.0

    def _watch_loop(self):
        """While relaying: check the speaker every few seconds. If it stops answering, or
        restarted (an airplay-esp32 reports its uptime), restart the sender: the old AirPlay
        session is gone, and a new one starts with the next audio once the speaker is back."""
        import socket
        fails, last_up = 0, None
        while True:
            time.sleep(self.WATCH_S)
            try:
                fails, last_up = self._watch_once(fails, last_up)
            except Exception as e:  # noqa: BLE001 - the watchdog must keep running
                log.warning("relay watchdog: %s", e)

    def _watch_once(self, fails, last_up):
        import socket
        if True:
            addr = self.status.get("address") if self._proc and self.status.get("state") == "playing" else None
            if not addr:
                return 0, None
            host, _, port = addr.rpartition(":")
            ok = True
            try:
                socket.create_connection((host, int(port)), timeout=3).close()
            except OSError:
                ok = False
            restarted = False
            if ok:
                try:
                    with urllib.request.urlopen("http://%s/api/system/info" % host, timeout=3) as r:
                        up = int(json.loads(r.read()).get("info", {}).get("uptime_s"))
                    restarted = last_up is not None and up < last_up
                    last_up = up
                except Exception:  # noqa: BLE001 - not an airplay-esp32: the port check is enough
                    pass
            fails = 0 if ok else fails + 1
            if restarted or fails >= 2:
                why = "the speaker restarted" if restarted else "the speaker stopped answering"
                log.info("relay: %s, restarting the sender", why)
                with self._lock:
                    if self._proc:
                        self._stop(why)
                        self._set("idle", address=None, error=why)
                fails, last_up = 0, None
        return fails, last_up

    def _drain_stderr(self, proc):
        for line in proc.stderr:
            line = line.decode("utf-8", "replace").strip()
            if not line:
                continue
            m = re.search(r"player latency is (\d+) ms", line)
            if m:
                self.status["latency_ms"] = int(m.group(1))
                log.info("relay: speaker playback latency %s ms", m.group(1))
            elif re.search(r"(?i)error|fail|warn|cannot|unable", line):
                log.info("cliraop: %s", line[:300])
            else:
                log.debug("cliraop: %s", line[:300])

    def audio_delay_s(self):
        """How long after Signinator reads the audio the speaker plays it:
        the audio queued for cliraop plus the speaker's playback latency.
        None when the relay isn't playing."""
        if self.status.get("state") != "playing":
            return None
        lat = self.status.get("latency_ms")
        lat = 1500 if lat is None else lat      # until cliraop reports it
        return (lat + self._backlog_ms) / 1000.0

    def _stop(self, why="idle"):
        p, self._proc, self._proc_target = self._proc, None, None
        self.status["latency_ms"] = None
        self._backlog_ms = 0.0
        self.status["backlog_ms"] = 0
        if p is None:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        log.info("relay stopped (%s)", why)

    # ---- phone volume -> target speaker ----
    def set_phone_volume(self, db: float):
        """Called with the phone's AirPlay volume for this speaker."""
        self.phone_db = max(-144.0, min(0.0, float(db)))
        self.status["phone_db"] = self.phone_db
        self._vol_kick.set()

    def _vol_loop(self):
        sent = (None, None)
        while True:
            self._vol_kick.wait()
            self._vol_kick.clear()
            time.sleep(0.15)             # coalesce slider drags
            db, addr = self.phone_db, (self.status.get("address") or "").split(":")[0]
            if not self._speaker_volume():
                self.status["volume_forward"] = None
                continue
            if db is None or not addr or self.status.get("state") != "playing":
                continue
            if (db, addr) == sent:
                continue
            try:
                req = urllib.request.Request(
                    VOLUME_URL % addr, data=json.dumps({"db": db}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3).read()
                sent = (db, addr)
                self.status["volume_forward"] = "ok"
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "code", None)
                self.status["volume_forward"] = (
                    "this speaker can't take volume changes (volume is set when a song starts)"
                    if code in (404, 405) else "error: %s" % e)

    def _kick_volume_later(self, delay=2.0):
        """After the sender connects (it sets its own start volume), apply the phone's."""
        threading.Timer(delay, self._vol_kick.set).start()

    def target_changed(self):
        """Settings changed: the next audio starts the new target, and the
        AirPlay name follows it."""
        with self._lock:
            self._stop("settings changed")
            t = self.cfg().get("target", "")
            self._set("idle" if t else "off", target=t, address=None, error=None)
        threading.Thread(target=self.sync_name, daemon=True).start()

    def settings_changed(self):
        """Relay settings that don't affect the running sender changed: only
        update the AirPlay speaker's config (it restarts only if that differs)."""
        threading.Thread(target=self.sync_name, daemon=True).start()

    # ---- main loop: drain the FIFO, forward when a target is set ----
    def _handle_audio(self, data, now, target):
        """One chunk of audio (from the pipe, or the sync test): analyze it and
        send it to the target speaker, starting the sender if needed."""
        self._last_audio = now
        if self._on_audio:
            try:
                self._on_audio(data)
            except Exception as e:  # noqa: BLE001
                log.debug("audio analysis: %s", e)
        with self._lock:
            if not target:
                if self._proc:
                    self._stop("relay turned off")
                if self.status["state"] != "off":
                    self._set("off", target="", address=None, error=None)
                return
            if self._proc and (self._proc.poll() is not None or self._proc_target != target):
                self._stop("restart")
            if self._proc is None:
                if self.status["state"] == "error" and now - self.status["since"] < 10:
                    return  # don't hammer a speaker that just failed
                self._proc = self._start(target)
                if self._proc:
                    self._kick_volume_later()
                self._proc_target = target if self._proc else None
            proc = self._proc
        if not proc:
            return
        with self._wlock:     # the pipe reader and the sync test never interleave
            try:
                proc.stdin.write(data)   # blocks at real-time pace: cliraop sets the speed
                proc.stdin.flush()
                try:     # audio still queued for cliraop (44.1 kHz x 4 bytes)
                    q = struct.unpack("i", fcntl.ioctl(proc.stdin.fileno(), termios.FIONREAD, b"\0\0\0\0"))[0]
                    self._backlog_ms += (q / 176.4 - self._backlog_ms) * 0.1
                    self.status["backlog_ms"] = int(self._backlog_ms)
                except OSError:
                    pass
            except (BrokenPipeError, OSError) as e:
                with self._lock:
                    self._stop("sender exited")
                    self._set("error", error="sender stopped: %s" % e)

    # ---- sync test: ticks through the relay, flashes on the sign ----
    SYNC_CHUNK = 4410                 # 0.1 s of audio

    def sync_active(self) -> bool:
        return self._sync_until > time.time()

    def start_sync(self, seconds=60, on_done=None):
        target = self.cfg().get("target", "")
        if not target:
            raise ValueError("pick a speaker for the music relay first")
        if time.time() - self._last_fifo < 3:
            raise ValueError("stop the music first: the test plays its own ticks")
        self._sync_until = time.time() + max(5, min(300, seconds))
        self._sync_done = on_done
        if not (self._sync_thread and self._sync_thread.is_alive()):
            self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="relay-sync")
            self._sync_thread.start()

    def stop_sync(self, why="stopped"):
        if self._sync_until:
            log.info("sync test %s", why)
        self._sync_until = 0.0

    @staticmethod
    def _tick_audio(k0, n):
        """n stereo samples starting at sample k0: a short soft tick at the start
        of every second, silence otherwise (about -18 dB, so it isn't loud)."""
        out = bytearray(n * 4)
        for i in range(n):
            k = (k0 + i) % 44100
            if k < 1500:
                env = math.exp(-k / 260.0) * min(1.0, k / 40.0)
                v = int(4200 * env * math.sin(2 * math.pi * 1800 * k / 44100))
                struct.pack_into("<hh", out, i * 4, v, v)
        return bytes(out)

    def _sync_loop(self):
        start = time.time()
        k = 0
        log.info("sync test started")
        try:
            while self.sync_active():
                target = self.cfg().get("target", "")
                if not target:
                    break
                self._handle_audio(self._tick_audio(k, self.SYNC_CHUNK), time.time(), target)
                k += self.SYNC_CHUNK
                wait = start + k / 44100 - time.time()
                if wait > 0:
                    time.sleep(wait)
        finally:
            self._sync_until = 0.0
            cb, self._sync_done = self._sync_done, None
            if cb:
                try:
                    cb()
                except Exception as e:  # noqa: BLE001
                    log.warning("sync test end: %s", e)

    def _loop(self):
        while True:
            path = self.cfg().get("audio_pipe")
            if not path or not os.path.exists(path):
                time.sleep(5)
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                self._set("error", error="can't open audio pipe: %s" % e)
                time.sleep(5)
                continue
            try:
                while True:
                    r, _, _ = select.select([fd], [], [], 1.0)
                    now = time.time()
                    target = self.cfg().get("target", "")
                    data = b""
                    if r:
                        try:
                            data = os.read(fd, 65536)
                        except BlockingIOError:
                            data = b""
                    if not data:
                        # No audio: either nothing to read, or no writer at all
                        # (EOF: shairport-sync closed the pipe after the music stopped).
                        with self._lock:
                            if self._proc and now - self._last_audio > IDLE_STOP:
                                self._stop("music stopped")
                                self._set("idle" if target else "off", address=None)
                        if r:
                            time.sleep(0.5)   # EOF makes select return at once; don't spin
                        continue
                    if self.sync_active():
                        self.stop_sync("music started")
                    self._last_fifo = now
                    self._handle_audio(data, now, target)
            finally:
                with self._lock:
                    self._stop("pipe closed")
                os.close(fd)
