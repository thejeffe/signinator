"""GIF library: animated GIFs (and still pictures) stored in DATA_DIR/gifs.

Defaults are original retro animations from retro_gifs.py, written once; a
default the user deletes is remembered and not brought back.
"""
from __future__ import annotations
import io
import json
import logging
import os
import re
import threading

from PIL import Image, ImageSequence

log = logging.getLogger("signinator.gifs")

EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp")
MAX_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 600
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def clean_name(s: str) -> str:
    s = re.sub(r"\.[a-z0-9]{2,5}$", "", (s or "").strip().lower())
    s = re.sub(r"[^a-z0-9_-]+", "-", s).strip("-_")
    return s[:48] or "gif"


class Library:
    def __init__(self, folder: str):
        self.dir = folder
        self._lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        self._seed()

    # ---- defaults ----
    def _meta_path(self):
        return os.path.join(self.dir, ".library.json")

    def _meta(self) -> dict:
        try:
            with open(self._meta_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"seeded": [], "removed": []}

    def _save_meta(self, m):
        tmp = self._meta_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(m, f)
        os.replace(tmp, self._meta_path())

    def _seed(self):
        from .retro_gifs import DEFAULTS
        m = self._meta()
        changed = False
        for name, make in DEFAULTS.items():
            if name in m["seeded"] or name in m["removed"]:
                continue
            try:
                with open(os.path.join(self.dir, name + ".gif"), "wb") as f:
                    f.write(make())
                m["seeded"].append(name)
                changed = True
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't make default GIF %s: %s", name, e)
        if changed:
            self._save_meta(m)

    # ---- library ----
    def path(self, name: str):
        if not NAME_RE.match(name or ""):
            return None
        for ext in EXTS:
            p = os.path.join(self.dir, name + ext)
            if os.path.exists(p):
                return p
        return None

    def list(self) -> list:
        m = self._meta()
        out = []
        for fn in sorted(os.listdir(self.dir)):
            base, ext = os.path.splitext(fn)
            if ext.lower() not in EXTS or not NAME_RE.match(base):
                continue
            p = os.path.join(self.dir, fn)
            try:
                with Image.open(p) as im:
                    frames = getattr(im, "n_frames", 1)
                    size = im.size
            except Exception:  # noqa: BLE001
                continue
            out.append({"name": base, "label": base.replace("-", " ").replace("_", " "),
                        "builtin": base in m["seeded"], "frames": frames,
                        "width": size[0], "height": size[1], "type": ext.lstrip(".").lower(),
                        "mtime": int(os.path.getmtime(p))})
        out.sort(key=lambda g: (not g["builtin"], g["label"]))
        return out

    def add(self, filename: str, data: bytes, name: str = "") -> str:
        if len(data) > MAX_BYTES:
            raise ValueError("file is over 8 MB")
        try:
            im = Image.open(io.BytesIO(data))
            n = 0
            for _ in ImageSequence.Iterator(im):
                n += 1
                if n > MAX_FRAMES:
                    raise ValueError("more than %d frames" % MAX_FRAMES)
            fmt = (im.format or "").lower()
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ValueError("not a picture: %s" % e)
        ext = {"gif": ".gif", "png": ".png", "jpeg": ".jpg", "webp": ".webp"}.get(fmt)
        if not ext:
            raise ValueError("use GIF, PNG, JPG or WEBP")
        base = clean_name(name or filename)
        with self._lock:
            final, k = base, 2
            while self.path(final):
                final = "%s-%d" % (base[:44], k)
                k += 1
            with open(os.path.join(self.dir, final + ext), "wb") as f:
                f.write(data)
        return final

    def delete(self, name: str) -> bool:
        p = self.path(name)
        if not p:
            return False
        os.remove(p)
        m = self._meta()
        if name in m["seeded"] and name not in m["removed"]:
            m["removed"].append(name)
            self._save_meta(m)
        return True

    def restore_defaults(self) -> int:
        m = self._meta()
        n = len(m["removed"])
        m["removed"] = []
        m["seeded"] = [s for s in m["seeded"] if self.path(s)]
        self._save_meta(m)
        self._seed()
        return n
