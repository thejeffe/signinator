# Signinator

Send **anything** — scrolling text, bitmaps, GIFs, emoji — to an 8×64 WS2812
LED matrix driven by an ESP32.

**Architecture:** a host renders 64×8 frames (Pillow) and streams them over
UDP/**DDP** to an **ESP32 running WLED**, which just displays pixels. All the
smarts (fonts, images, emoji, mapping) live host-side where libraries are rich.

```
[Web UI] → FastAPI (this service) --DDP--> ESP32/WLED → 8×64 panel
```

## Look and settings
The page uses the same design system as Treeinator (light/dark tokens, Barlow
Condensed + IBM Plex, boxes, status chip, ⚙ Settings dialog). The status chip
shows LIVE / SIGN OFF / OFFLINE / PREVIEW from a 15 s poll of WLED's JSON API.
Settings (⚙, or open `/#settings`): WLED host,
DDP port, Check WLED, "Match the sign's size to WLED's 2D matrix" (auto, on
by default) and "Get size from WLED now", sign layout (2D matrix or panels),
gamma, white balance, calibration patterns.

## Layout
- `app/` — service: `config`, `matrix` (mapping + DDP), `render` (scenes),
  `engine` (render/stream loop), `main` (FastAPI + UI + MJPEG preview).
- `static/` — web UI.
- `config.yaml` — geometry, **wiring/mapping**, WLED host, brightness.
- `deploy/` — systemd unit + Caddyfile.

## Run (dev)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```
Open http://host:8080 — works fully in **preview only** mode with no hardware.

## API (for Home Assistant / automation / Jamal)
- `POST /api/text`  `{text,color,bg,speed,scroll,emoji,effect,color2,gap_mode,
  font,size,height,smooth,align}`. `text` may hold several lines (`\n`);
  still text (scroll off) shows them stacked, aligned `left|center|right`.
  `size`: `fit|stretch|px` (`height` = pixels per line for `px`).
  `font`: an id from `GET /api/fonts`.
- `POST /api/preview/text` (same body) · `POST /api/preview/image` ·
  `GET /api/preview.bin` — render a look for the page's Preview without
  sending it to the sign.
- `POST /api/image` (multipart `file`, `mode`=fit|fill|stretch|scroll, `speed`)
- `POST /api/testpattern` `{pattern}` (origin|rows|sweep|border)
- `POST /api/brightness` `{value}` 0–255
- `POST /api/power` `{mode: on|off|wled}` (or legacy `{on}`). `wled` =
  Signinator stops sending and WLED runs its own effects.
- `POST /api/clear`
- `POST /api/config` `{mapping,wled,gamma,fps,brightness,color_correction}`
  (`wled.auto_geometry`: follow the controller's 2D matrix size, default on)
- `GET  /api/wled?host=` — read a WLED's setup (outputs, 2D matrix, realtime flags)
- `POST /api/wled/geometry` `{host?}` — size the sign to WLED's 2D matrix now
- `GET  /api/frame.bin` — current frame: uint16 w, uint16 h (big-endian), RGB bytes
- `GET  /health` · `GET /api/version`
- Effects (WLED-style, incl. music-reactive ♪ ones fed by the relay's audio):
  `GET /api/effects`, `POST /api/effect {fx, speed, intensity, palette, c1, c2, c3}`,
  `POST /api/preview/effect`. Light delay for music effects: `audio.delay_ms`.
- GIF library (`data/gifs`, defaults are original retro animations from
  `app/retro_gifs.py`): `GET /api/gifs`, `POST /api/gifs` (multipart `file`),
  `DELETE /api/gifs/{name}`, `POST /api/gifs/restore`, `GET /api/gifs/{name}`,
  `POST /api/gif {name, mode, rate}`, `POST /api/preview/gif`.
- Resting state (shown when music stops): `GET|POST /api/rest`
  `{look, after_music: rest|previous, use_current}`, `POST /api/rest/show`.
- Now playing (album art while music plays, then back to the last content):
  `POST /api/nowplaying` `{state: playing|paused|stopped, title, artist, album, source}`,
  `POST /api/nowplaying/art` (raw `image/jpeg|png` body, or multipart `file`),
  `GET /api/nowplaying`, `GET /api/nowplaying/art`, `POST /api/nowplaying/dismiss`.
  Optional `X-Token` when `nowplaying.token` is set. Sources: the ESP32 speaker
  (firmware branch `signinator-nowplaying` in ~/airplay-esp32) and a silent
  AirPlay 2 speaker on this server (`sudo bash deploy/install-airplay.sh`,
  read from the shairport-sync metadata pipe by `app/airplay_meta.py`).
- `GET  /api/state`
- `GET  /api/frame.png?scale=10` · `GET /api/stream.mjpg` (live preview)

## When the panels arrive
1. Flash **WLED** to the ESP32 (https://install.wled.me). Configure it as a 2D
   matrix, 64×8. Enable DDP (on by default, UDP 4048).
2. In Signinator **Setup** tab: set WLED host/IP, tick *Stream to WLED*, save.
3. Run **Calibration → Origin**: top-left pixel = red, +x = green, +y = blue.
   Watch the real panel and flip the mapping toggles (serpentine / flip H/V /
   chain serpentine) until the corner + directions match. Save mapping.
4. Send text — done.

## Wiring notes (hardware)
- Power: 512 WS2812 ≈ 30A @5V at full white. Use a dedicated 5V supply, inject
  5V+GND at **both ends** (and the panel seam). Common ground with the ESP32.
  Cap `brightness` in WLED to auto-limit current.
- Data: 3.3V→5V level shifter (74AHCT125), ~330Ω series resistor, 1000µF cap
  across 5V in.
- Chain: panel-1 DOUT → panel-2 DIN.
