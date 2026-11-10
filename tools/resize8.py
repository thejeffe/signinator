#!/usr/bin/env python3
"""Resize an image to 8px tall (or any height) for the Signinator panel.

Preserves aspect ratio and transparency. Works on your own images -- it just
scales whatever file you point it at; it doesn't fetch or generate artwork.

Usage:
    python3 tools/resize8.py INPUT [OUTPUT] [--height N] [--preview N]

    INPUT        source image (png/jpg/gif/webp/...)
    OUTPUT       output PNG path (default: INPUT with _8px.png suffix)
    --height N   target height in px (default: 8)
    --preview N  also write a NxN-scaled *_preview.png so you can see it

Examples:
    python3 tools/resize8.py logo.png
    python3 tools/resize8.py logo.png trojan.png --height 8 --preview 16
"""
from __future__ import annotations
import argparse
import os
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: pip install Pillow  (use the app's venv)")


def resize(path: str, height: int) -> "Image.Image":
    img = Image.open(path)
    try:
        img.seek(0)   # first frame of animated inputs
    except Exception:
        pass
    img = img.convert("RGBA")
    if img.height != height:
        r = height / img.height
        img = img.resize((max(1, round(img.width * r)), height), Image.LANCZOS)
    return img


def main(argv=None):
    ap = argparse.ArgumentParser(description="Resize an image for the LED panel.")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--height", type=int, default=8)
    ap.add_argument("--preview", type=int, default=0,
                    help="also write a scaled preview at this factor")
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        ap.error(f"no such file: {args.input}")

    img = resize(args.input, args.height)
    out = args.output
    if not out:
        stem, _ = os.path.splitext(args.input)
        out = f"{stem}_{args.height}px.png"
    img.save(out, "PNG")
    print(f"wrote {out}  ({img.width}x{img.height})")

    if args.preview > 1:
        p = img.resize((img.width * args.preview, img.height * args.preview),
                       Image.NEAREST)
        stem, _ = os.path.splitext(out)
        pp = f"{stem}_preview.png"
        p.save(pp, "PNG")
        print(f"wrote {pp}  ({p.width}x{p.height})")


if __name__ == "__main__":
    main()
