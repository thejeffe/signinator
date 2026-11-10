"""Pixel mapping (logical image -> physical LED order) and DDP streaming to WLED."""
from __future__ import annotations
import socket
import struct
import time


def build_coords(mapping: dict, width: int, height: int):
    """Return list `coords` where coords[led_index] = (x, y) in logical space.

    mode "matrix" (default): straight row-major, x fastest, top-left origin.
    Use this when WLED already handles the physical 2D layout, so the host
    must NOT remap — just send the logical image as-is.

    mode "panels": host does the tile/serpentine mapping itself (for when WLED
    is a dumb strip). Models a grid of identical panels chained in reading
    order, rows optionally serpentine.
    """
    mode = mapping.get("mode", "matrix")
    flip_h = bool(mapping.get("flip_h", False))
    flip_v = bool(mapping.get("flip_v", False))

    if mode == "matrix":
        coords = []
        for y in range(height):
            for x in range(width):
                xx = width - 1 - x if flip_h else x
                yy = height - 1 - y if flip_v else y
                coords.append((xx, yy))
        return coords

    tw = int(mapping["tile_w"]); th = int(mapping["tile_h"])
    tx = int(mapping["tiles_x"]); ty = int(mapping["tiles_y"])
    serp = bool(mapping.get("serpentine", True))
    chain_serp = bool(mapping.get("tile_chain_serpentine", False))
    flip_h = bool(mapping.get("flip_h", False))
    flip_v = bool(mapping.get("flip_v", False))

    coords = []
    for tj in range(ty):
        col_order = range(tx)
        if chain_serp and tj % 2 == 1:
            col_order = reversed(range(tx))
        for ti in col_order:
            for ry in range(th):
                row = range(tw)
                if serp and ry % 2 == 1:
                    row = reversed(range(tw))
                for rx in row:
                    x = ti * tw + rx
                    y = tj * th + ry
                    if flip_h:
                        x = width - 1 - x
                    if flip_v:
                        y = height - 1 - y
                    coords.append((x, y))
    return coords


class DDPSender:
    """Minimal DDP (Distributed Display Protocol) sender for WLED realtime."""

    MAX_DATA = 1440  # bytes per packet (must be divisible by 3), fits a UDP MTU

    def __init__(self, host: str, port: int = 4048):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._seq = 0
        self._addr = None      # resolved IP, cached (host may be a DNS name)
        self._resolved_at = 0.0
        self.error = None

    def _target(self):
        """Resolve the host at most every 60 s (every 5 s while failing)."""
        now = time.monotonic()
        ttl = 60.0 if self._addr else 5.0
        if self._addr is None or now - self._resolved_at > ttl:
            self._resolved_at = now
            try:
                self._addr = socket.gethostbyname(self.host)
                self.error = None
            except OSError as e:
                self.error = "can't resolve %s: %s" % (self.host, e)
                if self._addr is None:
                    return None
        return self._addr

    def send(self, data: bytes) -> None:
        """Send an RGB byte stream (already in physical LED order)."""
        total = len(data)
        offset = 0
        while offset < total:
            chunk = data[offset:offset + self.MAX_DATA]
            last = (offset + len(chunk)) >= total
            self._send_packet(offset, chunk, push=last)
            offset += len(chunk)

    def _send_packet(self, offset: int, chunk: bytes, push: bool) -> None:
        self._seq = (self._seq % 15) + 1
        flags = 0x40  # version 1
        if push:
            flags |= 0x01  # PUSH: render this frame now
        # header: flags, seq, type, dest_id, offset(uint32), length(uint16)
        header = struct.pack(
            ">BBBBIH", flags, self._seq, 0x00, 0x01, offset, len(chunk)
        )
        addr = self._target()
        if addr is None:
            return
        try:
            self.sock.sendto(header + chunk, (addr, self.port))
        except OSError as e:
            self.error = str(e)  # best-effort; preview keeps working if WLED is down

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
