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


@app.post("/api/text")
def post_text(payload: dict = Body(...)):
    engine.set_text(**_text_args(payload))
    return {"ok": True, "state": engine.state}


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
    engine.set_effect(payload)
    return {"ok": True, "state": engine.state}


@app.post("/api/preview/effect")
def effect_preview(payload: dict = Body(...)):
    engine.preview_effect(payload)
    return {"ok": True}


# ---- GIF library ----
@app.get("/api/gifs")
def gifs_list():
    return {"gifs": engine.gifs.list()}


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
    if bpl != "auto":
        try:
            bpl = max(1, min(16, int(bpl)))
        except (TypeError, ValueError):
            bpl = "auto"
    return str(p.get("name", "")), mode, rate, bool(p.get("beat")), bpl


@app.post("/api/gif")
def gif_post(payload: dict = Body(...)):
    try:
        engine.set_gif(*_gif_args(payload))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    return {"ok": True, "state": engine.state}


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
    """{look?: {kind: text|effect|gif|idle, ...}, after_music?: rest|previous,
    use_current?: true (save what the sign shows now)}"""
    patch = {}
    if payload.get("use_current"):
        cur = dict(engine.state)
        if cur.get("kind") not in _LOOK_KINDS:
            return JSONResponse({"ok": False, "error": "the sign isn't showing text, an effect or a GIF"},
                                status_code=400)
        patch["look"] = cur
    elif "look" in payload:
        look = payload["look"]
        if look is not None and (not isinstance(look, dict) or look.get("kind") not in _LOOK_KINDS):
            return JSONResponse({"ok": False, "error": "look.kind must be text, effect, gif or idle"},
                                status_code=400)
        patch["look"] = look
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
                     speed: int = Form(24)):
    data = await file.read()
    engine.set_image(data, mode=mode, speed=int(speed), name=file.filename or "")
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
def post_clear():
    engine.clear()
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
                        "color_correction", "nowplaying", "airplay", "relay", "audio", "dither", "mqtt", "mirror", "routing"}}
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
    ro = allowed.get("routing")
    if ro is not None:
        if not isinstance(ro, dict):
            return JSONResponse({"ok": False, "error": "routing must be an object"}, status_code=400)
        allowed["routing"] = {k: ro[k] for k in ("sign", "screen") if ro.get(k) in ("all", "art", "noart")}
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
