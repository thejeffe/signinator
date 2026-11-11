"""Config loading + persistence for Signinator."""
from __future__ import annotations
import os
import threading
import yaml

_LOCK = threading.Lock()
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("SIGNINATOR_CONFIG", os.path.join(_BASE, "config.yaml"))

DEFAULTS = {
    "width": 64,
    "height": 8,
    "mapping": {
        "tile_w": 32, "tile_h": 8, "tiles_x": 2, "tiles_y": 1,
        "serpentine": True, "tile_chain_serpentine": False,
        "flip_h": False, "flip_v": False,
    },
    "wled": {"host": "", "port": 4048, "enabled": False, "auto_geometry": True},
    "fps": 40,
    "brightness": 128,
    "gamma": 2.2,
    "color_correction": {"r": 255, "g": 176, "b": 240},
    "default": {"kind": "text", "text": "SIGNINATOR", "color": "#00e5ff",
                "bg": "#000000", "speed": 24, "scroll": True},
    "server": {"host": "0.0.0.0", "port": 8080, "data_dir": "data"},
    "nowplaying": {"enabled": True, "mode": "fill", "hold": 60, "token": ""},
    "airplay": {"metadata_pipe": "/tmp/shairport-sync-metadata"},
    "rest": {"after_music": "rest", "look": None},
    "dither": {"darkest": False},
    "fps_auto": True,
    "audio": {"auto": True, "delay_ms": 1500, "offset_ms": 0},
    "mirror": {"hosts": []},
    "stereo": {"enabled": False, "url": "http://homeassistant.local:8123", "token": "", "entity": "",
               "off_after_s": 300, "only_own": True},
    "mqtt": {"enabled": False, "host": "homeassistant.local", "port": 1883, "username": "",
             "password": "", "base": "signinator", "prefix": "homeassistant"},
    "relay": {"target": "", "volume": 60, "codec": "alac", "volume_mode": "audio", "volume_range_db": 30, "audio_pipe": "/var/lib/signinator-airplay/audio"},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with _LOCK:
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                data = yaml.safe_load(f) or {}
        return _deep_merge(DEFAULTS, data)


def save(cfg: dict) -> None:
    with _LOCK:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        os.replace(tmp, CONFIG_PATH)


def base_dir() -> str:
    return _BASE
