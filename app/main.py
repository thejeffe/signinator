"""FastAPI app: web UI + REST API + live MJPEG preview."""
from __future__ import annotations
import asyncio
import os
from fastapi import FastAPI, UploadFile, File, Form, Body, Request
from fastapi.responses import (Response, StreamingResponse, JSONResponse,
                               FileResponse)
from fastapi.staticfiles import StaticFiles
from .engine import engine
from . import config as cfgmod
from . import wled as wledmod
import hashlib
import re

app = FastAPI(title="Signinator", version="2.0")
_STATIC = os.path.join(cfgmod.base_dir(), "static")


def _build() -> str:
    """Short hash of the page assets, so the page can tell it's out of date."""
    h = hashlib.sha1()
    for n in ("index.html", "app.js", "style.css"):
        try:
            with open(os.path.join(_STATIC, n), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:10]


BUILD = _build()

@app.on_event("startup")
def _startup():
    engine.start()


@app.get("/")
def index():
    # no-store on the HTML so the page (and its versioned ?v= asset pointers)
    # always revalidates; static assets stay cache-busted via ?v=N.
    with open(os.path.join(_STATIC, "index.html")) as f:
        html = f.read().replace("?v=BUILD", "?v=" + BUILD) \
                       .replace("<body>", '<body data-build="%s">' % BUILD)
    return Response(html, media_type="text/html",
                    headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/health")
def health():
    return {"ok": True, "build": BUILD, "size": [engine.w, engine.h],
            "wled": engine.monitor.status}


@app.get("/api/version")
def version():
    return {"build": BUILD}


@app.post("/api/restart")
async def restart(request: Request):
    """Settings -> Restart Signinator. The process exits with a failure code
    and systemd (Restart=on-failure) starts it again, so no sudo is needed.
    JSON only, so a plain web form on another site can't trigger it."""
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        return JSONResponse({"error": "Send JSON."}, status_code=415)
    print("restart asked for from the web page", flush=True)

    def _bye():
        try:
            engine.stop()
        finally:
            os._exit(1)
    asyncio.get_running_loop().call_later(0.4, _bye)
    return {"ok": True, "restarting": True}


# ---- WLED controller ----
@app.get("/api/wled")
def get_wled(host: str = ""):
    """Read a controller's setup (outputs, 2D matrix, realtime flags)."""
    h = (host or engine.cfg.get("wled", {}).get("host", "")).strip()
    if not wledmod.valid_host(h):
        return JSONResponse({"ok": False, "host": h,
                             "error": "not a valid host name or IP"}, status_code=400)
    info = wledmod.read_wled(h)
    if info["ok"] and h == engine.cfg.get("wled", {}).get("host"):
        engine.wled_info = info
    return info


@app.post("/api/wled/geometry")
def wled_geometry(payload: dict = Body(default={})):
    """Size the sign to the controller's 2D matrix."""
    h = (payload.get("host") or engine.cfg.get("wled", {}).get("host", "")).strip()
    info = wledmod.read_wled(h)
    if not info["ok"]:
        return JSONResponse({"ok": False, "error": "couldn't reach %s: %s" % (h, info.get("error"))},
                            status_code=502)
    g = info.get("geometry")
    if not g:
        return JSONResponse({"ok": False, "error": "%s has no 2D matrix set up "
                             "(WLED → Config → 2D Configuration)" % h, "wled": info},
                            status_code=409)
    engine.set_geometry(g["width"], g["height"], g["mode"])
    return {"ok": True, "geometry": g, "wled": info, "state": get_state()}


# ---- state / control ----
@app.get("/api/state")
def get_state():
    w = engine.cfg.get("wled", {})
    return {
        "content": engine.state,
        "screen_content": engine.screen_state,     # None: the LCD shows the same as the sign
        "screen_mode": engine.screen_mode(),       # the LCD's own on / off / auto
        "screen_awake": engine.screen_awake(),
        "brightness": engine.brightness,
        "power": engine.power,
        "mode": engine.mode,
        "width": engine.w, "height": engine.h,
        "wled": {"host": w.get("host", ""), "port": w.get("port", 4048),
                 "enabled": bool(w.get("enabled")),
                 "auto_geometry": w.get("auto_geometry", True) is not False,
                 "status": engine.monitor.status,
                 "send_error": getattr(engine._sender, "error", None)},
        "build": BUILD,
        "rest": engine.rest_cfg(),
        "dither": engine.dither_cfg(),
        "send_fps": round(getattr(engine, "send_fps", 0.0), 1),
        "fps_auto": engine.fps_cfg()["auto"], "fps_target": round(engine.current_fps(), 1),
        "wled_fps": (engine.monitor.status or {}).get("fps"),
        "sync": {"active": engine.relay.sync_active(),
                 "remaining": max(0, int(engine.relay._sync_until - __import__("time").time()))},
        "music_playing": engine.music_playing(),
        "audio": dict({"auto": True, "delay_ms": 1500, "offset_ms": 0}, **(engine.cfg.get("audio") or {}),
                      live=engine.audio.live(), current_ms=int(engine.light_delay_s() * 1000),
                      bpm=engine.audio.tempo(),
                      latency_ms=engine.relay.status.get("latency_ms"),
                      backlog_ms=engine.relay.status.get("backlog_ms")),
        "nowplaying": dict(engine.nowplaying.status(),
                           settings={k: v for k, v in engine.nowplaying.cfg().items() if k != "token"},
                           token_set=bool(engine.nowplaying.cfg().get("token")),
                           airplay=engine.airplay.status),
        "relay": dict(engine.relay.status, settings=engine.relay.cfg()),
        "mqtt": dict(engine.ha.public_cfg(), status=engine.ha.status),
        "mirror": dict(engine.mirrors.cfg(), status=engine.mirrors.status()),
        "stereo": dict(engine.stereo.public_cfg(), status=engine.stereo.status),
        "routing": {"sign": engine.route("sign"), "screen": engine.route("screen"),
                    "showing": {"sign": engine.shows("sign"), "screen": engine.shows("screen")}},
        "mapping": engine.cfg.get("mapping", {}),
        "fps": engine.cfg.get("fps", 40),
        "gamma": engine.cfg.get("gamma", 2.2),
        "color_correction": engine.cfg.get("color_correction", {}),
    }


def _text_args(payload: dict) -> dict:
    return dict(
        text=str(payload.get("text", ""))[:1000],
        color=payload.get("color", "#00e5ff"),
        bg=payload.get("bg", "#000000"),
        speed=int(payload.get("speed", 24)),
        scroll=bool(payload.get("scroll", True)),
        emoji=bool(payload.get("emoji", False)),
        effect=str(payload.get("effect", "solid")),
        gap_mode=str(payload.get("gap_mode", "screen")),
        color2=payload.get("color2", "#ff00ff"),
        size=str(payload.get("size", "normal")),
        font=str(payload.get("font", "pixel")),
        height=int(payload.get("height", 0) or 0),
        smooth=bool(payload.get("smooth", False)),
        align=str(payload.get("align", "center")),
    )


@app.get("/api/fonts")
def get_fonts():
    from .render import FONTS
    return {"fonts": [{"id": k, "name": v[0]} for k, v in FONTS.items()]}


def _target(p) -> str:
    """Which display a send goes to: both (default) | sign (the LED sign) | screen (the LCD)."""
    t = (p or {}).get("target")
    return t if t in ("both", "sign", "screen") else "both"


@app.post("/api/text")
def post_text(payload: dict = Body(...)):
    engine.set_text(target=_target(payload), **_text_args(payload))
    return {"ok": True, "state": engine.state, "screen": engine.screen_state}


# ---- now playing (album art from AirPlay) ----
_MAX_ART = 2 * 1024 * 1024


def _np_auth(request: Request):
    """If a token is set in settings, senders must pass it as X-Token (or ?token=)."""
    tok = engine.nowplaying.cfg().get("token") or ""
    if not tok:
        return None
    got = request.headers.get("x-token") or request.query_params.get("token") or ""
    if got != tok:
        return JSONResponse({"ok": False, "error": "bad or missing X-Token"}, status_code=401)
    return None


@app.post("/api/nowplaying")
async def nowplaying_post(request: Request):
    """{state: playing|paused|stopped, title?, artist?, album?, source?}"""
    bad = _np_auth(request)
    if bad:
        return bad
    try:
        p = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "send JSON"}, status_code=400)
    if not isinstance(p, dict):
        return JSONResponse({"ok": False, "error": "send a JSON object"}, status_code=400)
    engine.nowplaying.update(str(p.get("source") or (request.client.host if request.client else "http")),
                             state=p.get("state"), title=p.get("title"),
                             artist=p.get("artist"), album=p.get("album"))
    return {"ok": True, "nowplaying": engine.nowplaying.status()}


@app.post("/api/nowplaying/art")
async def nowplaying_art(request: Request):
    """Cover art: raw body (Content-Type image/jpeg or image/png), or multipart
    field `file`. Optional ?source=&state=&title=&artist=&album=."""
    bad = _np_auth(request)
    if bad:
        return bad
    if int(request.headers.get("content-length") or 0) > _MAX_ART:
        return JSONResponse({"ok": False, "error": "image over 2 MB"}, status_code=413)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        data = await f.read() if f is not None and hasattr(f, "read") else b""
    else:
        data = await request.body()
    if not data or len(data) > _MAX_ART:
        return JSONResponse({"ok": False, "error": "no image, or over 2 MB"}, status_code=400)
    try:
        from PIL import Image
        from io import BytesIO
        Image.open(BytesIO(data)).load()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "can't read that image: %s" % e}, status_code=400)
    q = request.query_params
    src = q.get("source") or (request.client.host if request.client else "http")
    engine.nowplaying.update(src, state=q.get("state"), title=q.get("title"),
                             artist=q.get("artist"), album=q.get("album"), art=data)
    return {"ok": True, "nowplaying": engine.nowplaying.status()}


@app.get("/api/relay/speakers")
def relay_speakers(fresh: int = 0):
    """AirPlay speakers on the network that Signinator can relay music to."""
    return {"speakers": engine.relay.speakers(fresh=bool(fresh)),
            "status": engine.relay.status, "settings": engine.relay.cfg()}


@app.get("/api/nowplaying/history")
def nowplaying_history():
    """The last 10 different covers received (to hide placeholders)."""
    return {"history": engine.nowplaying.history()}


@app.get("/api/nowplaying/history/{sha}")
def nowplaying_history_image(sha: str):
    data = engine.nowplaying.history_image(sha)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    from .nowplaying import _sniff
    return Response(data, media_type=_sniff(data), headers={"Cache-Control": "max-age=3600"})


@app.post("/api/nowplaying/block")
def nowplaying_block(payload: dict = Body(...)):
    sha = str(payload.get("sha", ""))
    if not __import__("re").fullmatch(r"[0-9a-f]{40}", sha):
        return JSONResponse({"ok": False, "error": "bad image id"}, status_code=400)
    lst = engine.nowplaying.set_blocked(sha, bool(payload.get("blocked", True)))
    return {"ok": True, "blocked": lst}


@app.get("/api/nowplaying")
def nowplaying_get():
    return engine.nowplaying.status()


@app.get("/api/stereo/entities")
def stereo_entities():
    """On/off entities in Home Assistant (to pick the stereo)."""
    try:
        return {"entities": engine.stereo.entities()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "Couldn't ask Home Assistant: %s" % (getattr(e, "reason", None) or e)}, status_code=502)


@app.post("/api/stereo/test")
def stereo_test(payload: dict = Body(...)):
    """Turn the stereo on or off now (from the settings page)."""
    try:
        engine.stereo.turn(bool(payload.get("on")), "(test)")
        return {"ok": True, "state": engine.stereo.entity_state()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "Home Assistant: %s" % (getattr(e, "reason", None) or e)}, status_code=502)


@app.get("/api/nowplaying/events")
def nowplaying_events():
    """Recent AirPlay metadata as it arrived ("got"), was applied after the delay ("apply"),
    and what the sign did ("show"/"hide"). For troubleshooting covers."""
    return {"events": list(engine.nowplaying.events)}


@app.get("/api/nowplaying/art")
def nowplaying_art_get():
    np = engine.nowplaying
    if not np.art:
        return JSONResponse({"error": "no art yet"}, status_code=404)
    return Response(np.art, media_type=np.art_type, headers={"Cache-Control": "no-store"})


@app.post("/api/nowplaying/dismiss")
def nowplaying_dismiss():
    engine.nowplaying.dismiss()
    return {"ok": True, "nowplaying": engine.nowplaying.status()}


# ---- effects (WLED-style) ----
@app.get("/api/effects")
def effects_list():
    from .effects import catalog
    return catalog()


@app.post("/api/effect")
def effect_post(payload: dict = Body(...)):
    target = _target(payload)
    engine.set_effect({k: v for k, v in payload.items() if k != "target"}, target)
    return {"ok": True, "state": engine.state, "screen": engine.screen_state}


@app.post("/api/preview/effect")
def effect_preview(payload: dict = Body(...)):
    engine.preview_effect(payload)
    return {"ok": True}


# ---- GIF library ----
@app.get("/api/gifs")
def gifs_list(hidden: int = 0):
    """The library; hidden=1 also lists the originals hidden behind black copies."""
    return {"gifs": engine.gifs.list(include_hidden=bool(hidden)), "hidden": len(engine.gifs.hidden())}


@app.post("/api/gifs/{name}/hide")
def gifs_hide(name: str, payload: dict = Body(default={})):
    if not engine.gifs.path(name):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    engine.gifs.set_hidden(name, bool(payload.get("hidden", True)))
    return {"ok": True}


@app.post("/api/gifs")
async def gifs_add(file: UploadFile = File(...), name: str = Form("")):
    data = await file.read()
    try:
        final = engine.gifs.add(file.filename or "upload", data, name)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "name": final}


@app.delete("/api/gifs/{name}")
def gifs_delete(name: str):
    return {"ok": engine.gifs.delete(name)}


@app.post("/api/gifs/restore")
def gifs_restore():
    return {"ok": True, "restored": engine.gifs.restore_defaults()}


@app.post("/api/gifs/{name}/black")
@app.post("/api/gifs/{name}/copy")
def gifs_black(name: str, payload: dict = Body(default={})):
    """Copy "<name>-black" (black background), "<name>-trim" (trimmed, background
    kept) or "<name>-square" (only made square), each optionally made square. Always made from the original (so
    trying other settings on a black copy redoes it from the original), replaces
    an earlier copy, and hides the original (kept as a backup) unless keep_original."""
    from . import gifwork
    blacken = bool(payload.get("black", True))
    trim = bool(payload.get("trim", True))
    square = payload.get("square") if payload.get("square") in ("crop", "pad") else "none"
    if not blacken and not trim and square == "none":
        return JSONResponse({"ok": False, "error": "pick black background, trim or square"}, status_code=400)
    base = re.sub(r"-(black|trim|square)$", "", name)
    src = engine.gifs.path(base) or engine.gifs.path(name)
    if not src:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if not engine.gifs.path(base):
        base = name
    try:
        strength = max(0.3, min(2.5, float(payload.get("strength", 1.0))))
    except (TypeError, ValueError):
        strength = 1.0
    col = str(payload.get("line_color", "#ffffff"))
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", col)
    color = tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else (255, 255, 255)
    try:
        with open(src, "rb") as f:
            out = gifwork.black_background(f.read(), strength, bool(payload.get("holes")),
                                           lines=str(payload.get("lines", "keep")), line_color=color,
                                           trim=trim, blacken=blacken, square=square)
        kind = "black" if blacken else ("trim" if trim else "square")
        final = base[:41] + "-" + kind
        for other in ("black", "trim", "square"):  # one copy per original: replaces the other kinds
            op = engine.gifs.path(base[:41] + "-" + other)
            if other != kind and op:
                os.remove(op)
        engine.gifs.put(final, ".gif", out)
        if not payload.get("keep_original"):
            engine.gifs.set_hidden(base, True)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "name": final, "original": base}


@app.get("/api/gifs/{name}/moves")
def gifs_moves(name: str, bpm: float = 120.0):
    """Dance moves found in a GIF, and how they'd fall on beats at this tempo."""
    from . import gifwork
    from PIL import Image
    p = engine.gifs.path(name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    with Image.open(p) as im:
        d = gifwork.analyse(p, im)
    if not d:
        return {"moves": 0, "beats": 0, "loop_s": 0, "steady": 0}
    bpm = max(40.0, min(240.0, float(bpm or 120)))
    nb, _, _ = gifwork.dance_map(d, 60.0 / bpm)
    return {"moves": len(d["beats"]), "beats": nb, "loop_s": round(d["total"], 2),
            "dance_bpm": round(60.0 / d["spacing"]), "steady": round(d["steady"], 2)}


@app.get("/api/gifs/{name}/poses.png")
def gifs_poses(name: str):
    """The frame on each dance beat, side by side: the poses that land on the music's beats."""
    from . import gifwork
    from PIL import Image
    p = engine.gifs.path(name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    with Image.open(p) as im:
        d = gifwork.analyse(p, im)
        png = gifwork.pose_strip(im, d)
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=60"})


@app.get("/api/gifs/{name}")
def gifs_file(name: str):
    p = engine.gifs.path(name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, headers={"Cache-Control": "max-age=60"})


def _gif_args(p: dict):
    mode = p.get("mode") if p.get("mode") in ("fit", "fill", "stretch", "scroll") else "fit"
    try:
        rate = max(0.1, min(8.0, float(p.get("rate", 1.0))))
    except (TypeError, ValueError):
        rate = 1.0
    bpl = p.get("bpl", "auto")
    if bpl not in ("auto", "loop"):
        try:
            bpl = max(1, min(32, int(bpl)))
        except (TypeError, ValueError):
            bpl = "auto"
    try:
        nudge = round(max(-1.0, min(1.0, float(p.get("nudge", 0.0)))), 3)
    except (TypeError, ValueError):
        nudge = 0.0
    return str(p.get("name", "")), mode, rate, bool(p.get("beat")), bpl, nudge


@app.post("/api/gif")
def gif_post(payload: dict = Body(...)):
    try:
        engine.set_gif(*_gif_args(payload), target=_target(payload))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    return {"ok": True, "state": engine.state, "screen": engine.screen_state}


@app.post("/api/preview/gif")
def gif_preview(payload: dict = Body(...)):
    try:
        engine.preview_gif(*_gif_args(payload))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    return {"ok": True}


# ---- sync test ----
@app.post("/api/sync/start")
def sync_start(payload: dict = Body(default={})):
    try:
        engine.sync_start(int(payload.get("seconds", 60) or 60))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
    return {"ok": True}


@app.post("/api/sync/stop")
def sync_stop():
    engine.sync_stop()
    return {"ok": True}


# ---- resting state ----
_LOOK_KINDS = ("text", "effect", "gif", "idle")


@app.get("/api/rest")
def rest_get():
    return engine.rest_cfg()


@app.post("/api/rest")
def rest_post(payload: dict = Body(...)):
    """{look?: {kind: text|effect|gif|idle, ...} | null, target?: both|sign|screen,
    after_music?: rest|previous, use_current?: true (save what that display shows now)}"""
    target = _target(payload)
    patch = {}
    look = ...
    if payload.get("use_current"):
        look = dict(engine.screen_content() if target == "screen" else engine.state)
        if look.get("kind") not in _LOOK_KINDS:
            return JSONResponse({"ok": False, "error": "it isn't showing text, an effect or a GIF"},
                                status_code=400)
    elif "look" in payload:
        look = payload["look"]
        if look is not None and (not isinstance(look, dict) or look.get("kind") not in _LOOK_KINDS):
            return JSONResponse({"ok": False, "error": "look.kind must be text, effect, gif or idle"},
                                status_code=400)
    if look is not ...:
        if target in ("both", "sign"):
            patch["look"] = look
        if target in ("both", "screen"):
            patch["screen_look"] = look
    if payload.get("after_music") in ("rest", "previous"):
        patch["after_music"] = payload["after_music"]
    rest = dict(engine.rest_cfg())
    rest.update(patch)
    engine.cfg["rest"] = rest
    cfgmod.save(engine.cfg)
    return {"ok": True, "rest": rest}


@app.post("/api/rest/show")
def rest_show():
    if not engine.show_rest():
        return JSONResponse({"ok": False, "error": "no resting look saved yet"}, status_code=400)
    return {"ok": True, "state": engine.state}


# ---- the LCD screen: its own On / Off / Auto, and what it's showing ----
@app.post("/api/screen")
def screen_post(payload: dict = Body(...)):
    """{mode: on|off|auto} and/or {wake: true} (Auto with no music: light it now)."""
    if payload.get("mode") is not None:
        try:
            engine.set_screen_mode(str(payload["mode"]))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if payload.get("wake"):
        engine.wake("screen")
    return {"ok": True, "mode": engine.screen_mode(), "awake": engine.screen_awake()}


@app.get("/api/screen.bin")
def screen_bin():
    """What the LCD was last sent, pixel for pixel. X-Screen: "frame" (uint16 w, uint16 h
    big-endian, then RGB bytes, drawn as LED dots), "photo" (the JPEG; X-Scale 2 = the
    screen doubles it) or "none" (nothing sent lately: dark, asleep or away)."""
    import struct
    snap = engine.mirrors.snapshot()
    h = {"Cache-Control": "no-store"}
    if snap is None:
        return Response(b"", media_type="application/octet-stream", headers=dict(h, **{"X-Screen": "none"}))
    if snap[0] == "photo":
        return Response(snap[1], media_type="image/jpeg", headers=dict(h, **{"X-Screen": "photo", "X-Scale": str(snap[2])}))
    img = snap[1].convert("RGB")
    return Response(struct.pack(">HH", img.width, img.height) + img.tobytes(),
                    media_type="application/octet-stream", headers=dict(h, **{"X-Screen": "frame"}))


# ---- song cues: show a look instead of everything while a chosen song plays ----
@app.get("/api/songs")
def songs_get():
    """The last 10 songs played, and the song cues."""
    return {"recent": engine.nowplaying.songs(), "cues": engine.nowplaying.cues(),
            "playing": engine.nowplaying.status().get("cue")}


@app.post("/api/cues")
def cues_post(payload: dict = Body(...)):
    """Add or change a cue: {id?, title?, artist?, look | use_current, secs? (0 = the whole song),
    show_on? (sign | screen | both; the LED sign by default), on?}.
    Title only: that song by anyone. Artist only: any song by them. Both: that song by them."""
    import uuid
    from .nowplaying import CUES_MAX, norm
    title = str(payload.get("title") or "").strip()[:200]
    artist = str(payload.get("artist") or "").strip()[:200]
    if not norm(title) and not norm(artist):
        return JSONResponse({"ok": False, "error": "type a song title, an artist, or both"}, status_code=400)
    look = dict(engine.state) if payload.get("use_current") else payload.get("look")
    if not isinstance(look, dict) or look.get("kind") not in _LOOK_KINDS:
        return JSONResponse({"ok": False, "error": "pick text, an effect or a GIF to show"}, status_code=400)
    if look.get("kind") == "gif" and not engine.gifs.path(str(look.get("name", ""))):
        return JSONResponse({"ok": False, "error": "no GIF called %r" % look.get("name")}, status_code=400)
    try:
        secs = max(0, min(3600, int(payload.get("secs") or 0)))
    except (TypeError, ValueError):
        secs = 0
    cues = engine.nowplaying.cues()
    cid = str(payload.get("id") or "")
    old = next((c for c in cues if c.get("id") == cid), None) if cid else None
    if old is None:   # the same song again: replace its cue
        old = next((c for c in cues if norm(c.get("title")) == norm(title)
                    and norm(c.get("artist")) == norm(artist)), None)
    show_on = payload.get("show_on") if payload.get("show_on") in ("sign", "screen", "both") else \
        (old or {}).get("show_on", "sign")
    cue = {"id": old["id"] if old else uuid.uuid4().hex[:10], "title": title, "artist": artist,
           "look": look, "secs": secs, "on": payload.get("on") is not False, "show_on": show_on}
    cues = [cue if c is old else c for c in cues] if old else cues + [cue]
    if len(cues) > CUES_MAX:
        return JSONResponse({"ok": False, "error": "too many song cues"}, status_code=400)
    engine.cfg["cues"] = cues
    cfgmod.save(engine.cfg)
    engine.nowplaying.refresh()        # the song playing now may have just got (or lost) a cue
    return {"ok": True, "cue": cue, "cues": cues}


@app.delete("/api/cues/{cid}")
def cues_delete(cid: str):
    cues = [c for c in engine.nowplaying.cues() if c.get("id") != cid]
    engine.cfg["cues"] = cues
    cfgmod.save(engine.cfg)
    engine.nowplaying.refresh()
    return {"ok": True, "cues": cues}


@app.post("/api/cues/{cid}/try")
def cues_try(cid: str, payload: dict = Body(default={})):
    """Show a cue on the sign for a few seconds (default 10), then put back what was there."""
    cue = next((c for c in engine.nowplaying.cues() if c.get("id") == cid), None)
    if cue is None:
        return JSONResponse({"ok": False, "error": "no such cue"}, status_code=404)
    try:
        secs = max(1, min(120, float(payload.get("seconds") or 10)))
    except (TypeError, ValueError):
        secs = 10
    from .nowplaying import cue_displays
    on = cue_displays(cue)
    engine.wake("both" if len(on) == 2 else on[0])
    engine.nowplaying.try_cue(cue, secs)
    return {"ok": True, "seconds": secs}


@app.post("/api/cues/stop")
def cues_stop():
    engine.nowplaying.stop_try()
    return {"ok": True}


# ---- preview (not sent to the sign) ----
@app.post("/api/preview/text")
def preview_text(payload: dict = Body(...)):
    engine.preview_text(**_text_args(payload))
    return {"ok": True}


@app.post("/api/preview/image")
async def preview_image(file: UploadFile = File(...), mode: str = Form("fit"),
                        speed: int = Form(24)):
    try:
        engine.preview_image(await file.read(), mode=mode, speed=int(speed))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "can't read that image: %s" % e},
                            status_code=400)
    return {"ok": True}


@app.get("/api/preview.bin")
def preview_bin():
    return Response(engine.preview_raw(), media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/effects")
def get_effects():
    from .render import EFFECTS
    return {"effects": EFFECTS}


@app.post("/api/image")
async def post_image(file: UploadFile = File(...), mode: str = Form("fit"),
                     speed: int = Form(24), target: str = Form("both")):
    data = await file.read()
    engine.set_image(data, mode=mode, speed=int(speed), name=file.filename or "",
                     target=_target({"target": target}))
    return {"ok": True, "state": engine.state}


# ---- custom emoji (user-supplied images) ----
@app.get("/api/emoji")
def list_emoji():
    """All renderable custom-glyph tokens: {name: 'builtin'|'user'}."""
    from . import customglyphs
    return {"emoji": customglyphs.all_tokens()}


@app.post("/api/emoji")
async def add_emoji(file: UploadFile = File(...), name: str = Form(...)):
    """Upload one of YOUR images as a reusable :name: emoji. The image is
    resized server-side to the 8px panel height, preserving transparency."""
    from . import customglyphs
    from .render import fit_emoji_png
    n = (name or "").strip().lower()
    if not customglyphs.NAME_RE.match(n):
        return JSONResponse(
            {"ok": False, "error": "name must be 1-32 chars: a-z, 0-9, _"},
            status_code=400)
    if n in customglyphs.BUILTIN:
        return JSONResponse(
            {"ok": False, "error": f"'{n}' is a built-in badge; pick another name"},
            status_code=400)
    data = await file.read()
    try:
        png = fit_emoji_png(data, height=customglyphs.HEIGHT)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"bad image: {e}"},
                            status_code=400)
    path = customglyphs._user_path(n)
    with open(path, "wb") as f:
        f.write(png)
    customglyphs._USER_CACHE.pop(n, None)
    return {"ok": True, "name": n, "token": f":{n}:"}


@app.delete("/api/emoji/{name}")
def remove_emoji(name: str):
    from . import customglyphs
    ok = customglyphs.delete_user((name or "").lower())
    return {"ok": ok}


@app.get("/api/emoji/{name}.png")
def emoji_png(name: str, scale: int = 1):
    """Preview a custom emoji as a PNG (built-in or user)."""
    from . import customglyphs
    img = customglyphs.glyph_image((name or "").lower(), bg=(0, 0, 0))
    if img is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    s = max(1, min(40, int(scale)))
    if s > 1:
        img = img.resize((img.width * s, img.height * s))
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.post("/api/testpattern")
def post_testpattern(payload: dict = Body(...)):
    engine.test_pattern(payload.get("pattern", "border"))
    return {"ok": True, "state": engine.state}


@app.post("/api/clear")
def post_clear(payload: dict = Body(default={})):
    engine.clear(_target(payload))
    return {"ok": True}


@app.post("/api/brightness")
def post_brightness(payload: dict = Body(...)):
    engine.set_brightness(int(payload.get("value", 128)))
    return {"ok": True, "brightness": engine.brightness}


@app.post("/api/power")
def post_power(payload: dict = Body(...)):
    """{"on": bool} or {"mode": "on" | "off" | "wled"} (wled = stop sending,
    WLED runs its own effects)."""
    mode = payload.get("mode")
    if mode == "auto" and payload.get("wake"):
        engine.wake()                 # a manual send while in auto: show it now
    elif mode in ("on", "off", "wled", "auto"):
        engine.set_mode(mode)
    else:
        engine.set_power(bool(payload.get("on", True)))
    return {"ok": True, "power": engine.power, "mode": engine.mode}


@app.post("/api/config")
def post_config(patch: dict = Body(...)):
    allowed = {k: v for k, v in patch.items()
               if k in {"mapping", "wled", "gamma", "brightness", "fps", "fps_auto",
                        "color_correction", "nowplaying", "airplay", "relay", "audio", "dither", "mqtt", "mirror", "routing", "stereo"}}
    if "fps_auto" in allowed:
        allowed["fps_auto"] = bool(allowed["fps_auto"])
    if "fps" in allowed:
        try:
            allowed["fps"] = max(5, min(240, int(allowed["fps"])))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "fps must be a number"}, status_code=400)
    if "dither" in allowed:
        dz = allowed["dither"] if isinstance(allowed["dither"], dict) else {}
        clean = {k: bool(dz[k]) for k in ("enabled", "darkest") if k in dz}
        if dz.get("mode") in ("off", "spatial", "hue", "temporal", "wled"):
            clean["mode"] = dz["mode"]
            clean["enabled"] = dz["mode"] != "off"
        if "fit" in dz:
            clean["fit"] = bool(dz["fit"])
        if "min_hz" in dz:
            try:
                clean["min_hz"] = max(0, min(120, int(dz["min_hz"])))
            except (TypeError, ValueError):
                pass
        if "wled_fps" in dz:
            if dz["wled_fps"] == "auto":
                clean["wled_fps"] = "auto"
            else:
                try:
                    clean["wled_fps"] = max(0, min(250, int(dz["wled_fps"])))
                except (TypeError, ValueError):
                    pass
        allowed["dither"] = clean
    au = allowed.get("audio")
    if au is not None:
        try:
            au = au or {}
            clean = {}
            if "auto" in au:
                clean["auto"] = bool(au["auto"])
            if "delay_ms" in au:
                clean["delay_ms"] = max(0, min(5000, int(au["delay_ms"])))
            if "offset_ms" in au:
                clean["offset_ms"] = max(-2000, min(2000, int(au["offset_ms"])))
            allowed["audio"] = clean
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "audio.delay_ms must be a number"}, status_code=400)
    rl = allowed.get("relay")
    if rl is not None:
        if not isinstance(rl, dict):
            return JSONResponse({"ok": False, "error": "relay must be an object"}, status_code=400)
        clean = {}
        if "target" in rl:
            clean["target"] = str(rl["target"] or "").strip()[:120]
        if "volume" in rl:
            clean["volume"] = max(0, min(100, int(rl["volume"] or 0)))
        if rl.get("codec") in ("alac", "pcm"):
            clean["codec"] = rl["codec"]
        if rl.get("volume_mode") in ("audio", "speaker"):
            clean["volume_mode"] = rl["volume_mode"]
        if "airplay_name" in rl:     # the AirPlay speaker name phones see ("" = "<speaker> w/Art")
            clean["airplay_name"] = re.sub(r'["\\\x00-\x1f]', "", str(rl["airplay_name"] or "")).strip()[:60]

        allowed["relay"] = clean
    np_cfg = allowed.get("nowplaying")
    if np_cfg is not None:
        if not isinstance(np_cfg, dict):
            return JSONResponse({"ok": False, "error": "nowplaying must be an object"}, status_code=400)
        clean = {}
        if "enabled" in np_cfg:
            clean["enabled"] = bool(np_cfg["enabled"])
        if np_cfg.get("mode") in ("fill", "fit", "stretch"):
            clean["mode"] = np_cfg["mode"]
        if "hold" in np_cfg:
            clean["hold"] = max(0, min(3600, int(np_cfg["hold"] or 0)))
        for k in ("enhance", "lift"):
            if k in np_cfg:
                clean[k] = bool(np_cfg[k])
        if "art_delay" in np_cfg:
            v = np_cfg["art_delay"]
            if v == "auto":
                clean["art_delay"] = "auto"
            else:
                try:
                    clean["art_delay"] = max(0, min(10000, int(v)))
                except (TypeError, ValueError):
                    pass
        if np_cfg.get("show") in ("song", "brief"):
            clean["show"] = np_cfg["show"]
        if "brief_s" in np_cfg:
            clean["brief_s"] = max(1, min(600, int(np_cfg["brief_s"] or 10)))
        if "token" in np_cfg:
            clean["token"] = str(np_cfg["token"] or "")[:200]
        allowed["nowplaying"] = clean
    st = allowed.get("stereo")
    if st is not None:
        if not isinstance(st, dict):
            return JSONResponse({"ok": False, "error": "stereo must be an object"}, status_code=400)
        clean = {}
        for k in ("enabled", "only_own"):
            if k in st:
                clean[k] = bool(st[k])
        if "url" in st:
            u = str(st["url"] or "").strip().rstrip("/")
            if u and not re.match(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$", u):
                return JSONResponse({"ok": False, "error": "Home Assistant address: like http://homeassistant.local:8123"}, status_code=400)
            clean["url"] = u
        if "entity" in st:
            ent = str(st["entity"] or "").strip()
            if ent and not re.match(r"^[a-z_]+\.[a-z0-9_]+$", ent):
                return JSONResponse({"ok": False, "error": "Stereo: pick an entity like switch.main_stereo"}, status_code=400)
            clean["entity"] = ent
        if st.get("token"):                     # empty = keep the saved one
            clean["token"] = str(st["token"]).strip()[:400]
        if "off_after_s" in st:
            try:
                clean["off_after_s"] = max(0, min(24 * 3600, int(st["off_after_s"])))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "off delay must be a number"}, status_code=400)
        allowed["stereo"] = clean
    ro = allowed.get("routing")
    if ro is not None:
        if not isinstance(ro, dict):
            return JSONResponse({"ok": False, "error": "routing must be an object"}, status_code=400)
        # "on" (the page's album-art switch): back to how album art was on that display before
        # it was switched off ("art" = dark without music, or "all"); switching off remembers it
        last = dict(engine.cfg.get("routing_last") or {})
        clean = {}
        for k in ("sign", "screen"):
            v = ro.get(k)
            if v == "on":
                v = last.get(k) if last.get(k) in ("all", "art") else "all"
            if v in ("all", "art", "noart"):
                if v == "noart" and engine.route(k) in ("all", "art"):
                    last[k] = engine.route(k)
                clean[k] = v
        allowed["routing"] = clean
        if last != (engine.cfg.get("routing_last") or {}):
            allowed["routing_last"] = last
    mr = allowed.get("mirror")
    if mr is not None:
        if not isinstance(mr, dict):
            return JSONResponse({"ok": False, "error": "mirror must be an object"}, status_code=400)
        look_clean = {}
        if mr.get("look") in ("led", "hires"):
            look_clean["look"] = mr["look"]
        try:
            if "size" in mr:
                from .mirror import HIRES_SIZES
                if int(mr["size"]) in HIRES_SIZES:
                    look_clean["size"] = int(mr["size"])
            if "fps" in mr:
                look_clean["fps"] = max(5, min(60, int(mr["fps"])))
            if "fps_auto" in mr:
                look_clean["fps_auto"] = bool(mr["fps_auto"])
            if "photos" in mr:
                look_clean["photos"] = bool(mr["photos"])
            if "enabled" in mr:
                look_clean["enabled"] = bool(mr["enabled"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "mirror size/fps must be numbers"}, status_code=400)
        hosts = mr.get("hosts")
        if hosts is None:
            allowed["mirror"] = look_clean
            hosts = []
        elif not isinstance(hosts, list):
            return JSONResponse({"ok": False, "error": "mirror.hosts must be a list"}, status_code=400)
        clean_hosts = []
        for h in hosts[:4]:
            h = str(h or "").strip()
            if not h:
                continue
            if not wledmod.valid_host(h):
                return JSONResponse({"ok": False, "error": "Mirror screen %r: not a valid host name or IP" % h}, status_code=400)
            if h not in clean_hosts:
                clean_hosts.append(h)
        if mr.get("hosts") is not None:
            allowed["mirror"] = dict(look_clean, hosts=clean_hosts)
    mq = allowed.get("mqtt")
    if mq is not None:
        if not isinstance(mq, dict):
            return JSONResponse({"ok": False, "error": "mqtt must be an object"}, status_code=400)
        clean = {}
        if "enabled" in mq:
            clean["enabled"] = bool(mq["enabled"])
        if "host" in mq:
            h = str(mq["host"] or "").strip()
            if h and not wledmod.valid_host(h):
                return JSONResponse({"ok": False, "error": "MQTT broker: not a valid host name or IP"}, status_code=400)
            clean["host"] = h.split(":")[0]
        if "port" in mq:
            try:
                clean["port"] = max(1, min(65535, int(mq["port"])))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "MQTT port must be a number"}, status_code=400)
        if "username" in mq:
            clean["username"] = str(mq["username"] or "").strip()[:100]
        if mq.get("password"):                 # empty = keep the saved one
            clean["password"] = str(mq["password"])[:200]
        if mq.get("clear_password"):
            clean["password"] = ""
        if "base" in mq:
            b = str(mq["base"] or "").strip().lower()
            if not re.match(r"^[a-z0-9_-]{1,40}$", b):
                return JSONResponse({"ok": False, "error": "MQTT topic: use a-z, 0-9, - or _"}, status_code=400)
            clean["base"] = b
        allowed["mqtt"] = clean
    if "airplay" in allowed:
        pipe = str((allowed["airplay"] or {}).get("metadata_pipe", "")).strip()
        allowed["airplay"] = {"metadata_pipe": pipe} if pipe.startswith("/") else {}
    host = (allowed.get("wled") or {}).get("host")
    if host is not None:
        host = str(host).strip()
        if host and not wledmod.valid_host(host):
            return JSONResponse({"ok": False, "error": "not a valid host name or IP"},
                                status_code=400)
        allowed["wled"]["host"] = host
    engine.update_config(allowed)
    return {"ok": True, "state": get_state()}


# ---- preview ----
@app.get("/api/frame.bin")
def frame_bin():
    """Current frame for the page's LED canvas (see Engine.frame_raw)."""
    return Response(engine.frame_raw(), media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/frame.png")
def frame_png(scale: int = 10):
    return Response(engine.preview_png(scale=scale), media_type="image/png")


@app.get("/api/stream.mjpg")
def stream_mjpg(scale: int = 12, fps: int = 15):
    boundary = "frame"

    async def gen():
        delay = 1.0 / max(1, min(30, fps))
        while True:
            png = engine.preview_png(scale=scale)
            yield (b"--" + boundary.encode() + b"\r\n"
                   b"Content-Type: image/png\r\n\r\n" + png + b"\r\n")
            await asyncio.sleep(delay)

    return StreamingResponse(
        gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}")


class _NoCacheStatic(StaticFiles):
    """Serve static assets with revalidation so browsers can't get stuck on a
    stale app.js/style.css after a deploy (the ?v= bump only helps if the HTML
    referencing it isn't itself cached; no-cache here is the belt-and-braces)."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.endswith((".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/static", _NoCacheStatic(directory=_STATIC), name="static")
