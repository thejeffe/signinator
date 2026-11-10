"""A small MQTT 3.1.1 client (QoS 0 only): enough to talk to Home Assistant's
broker without installing anything. One thread uses it at a time: connect(),
then call poll() in a loop (it also sends the keep-alive pings)."""
from __future__ import annotations
import select
import socket
import struct
import threading
import time

REFUSED = {1: "wrong MQTT version", 2: "client id rejected", 3: "broker unavailable",
           4: "wrong user name or password", 5: "not authorized"}


class MQTTError(Exception):
    pass


def _length(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _s(v) -> bytes:
    b = v.encode("utf-8") if isinstance(v, str) else bytes(v)
    return struct.pack("!H", len(b)) + b


class Client:
    def __init__(self, host, port=1883, client_id="", username="", password="",
                 will=None, keepalive=30):
        self.host, self.port = host, int(port)
        self.client_id, self.username, self.password = client_id, username or "", password or ""
        self.will = will                 # (topic, payload bytes, retain)
        self.keepalive = int(keepalive)
        self.sock = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_tx = self._last_rx = time.monotonic()
        self._pid = 0

    # ---- low level ----
    def _send(self, header: int, body: bytes):
        if self.sock is None:
            raise MQTTError("not connected")
        with self._lock:
            self.sock.sendall(bytes([header]) + _length(len(body)) + body)
            self._last_tx = time.monotonic()

    def _packet(self):
        """One complete packet from the buffer, or None."""
        buf = self._buf
        n, mult, i = 0, 1, 1
        while True:
            if i >= len(buf):
                return None
            b = buf[i]
            n += (b & 127) * mult
            mult *= 128
            i += 1
            if not b & 128:
                break
            if i > 4:
                raise MQTTError("bad packet from the broker")
        if len(buf) < i + n:
            return None
        head, body = buf[0], bytes(buf[i:i + n])
        del buf[:i + n]
        return head, body

    def _read(self, timeout: float) -> bool:
        r, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
        if not r:
            return False
        data = self.sock.recv(65536)
        if not data:
            raise MQTTError("the broker closed the connection")
        self._buf += data
        self._last_rx = time.monotonic()
        return True

    # ---- API ----
    def connect(self, timeout=8.0):
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(15)
        flags = 0x02                                   # clean session
        payload = _s(self.client_id)
        if self.will:
            topic, msg, retain = self.will
            flags |= 0x04 | (0x20 if retain else 0)
            payload += _s(topic) + _s(msg)
        if self.username:
            flags |= 0x80
            payload += _s(self.username)
            if self.password:
                flags |= 0x40
                payload += _s(self.password)
        self._send(0x10, _s("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive) + payload)
        end = time.monotonic() + timeout
        while (p := self._packet()) is None:
            left = end - time.monotonic()
            if left <= 0:
                raise MQTTError("no answer from the broker")
            self._read(left)
        head, body = p
        if head >> 4 != 2 or len(body) < 2:
            raise MQTTError("unexpected answer from the broker")
        if body[1]:
            raise MQTTError("broker refused: " + REFUSED.get(body[1], "code %d" % body[1]))

    def local_ip(self) -> str:
        try:
            return self.sock.getsockname()[0]
        except (OSError, AttributeError):
            return ""

    def publish(self, topic: str, payload, retain=False):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._send(0x30 | (1 if retain else 0), _s(topic) + bytes(payload))

    def subscribe(self, topics):
        self._pid = self._pid % 65535 + 1
        self._send(0x82, struct.pack("!H", self._pid) + b"".join(_s(t) + b"\x00" for t in topics))

    def poll(self, timeout=0.5) -> list:
        """Wait up to timeout for messages; returns [(topic, payload bytes)]."""
        self._read(timeout)
        msgs = []
        while True:
            p = self._packet()
            if p is None:
                break
            head, body = p
            if head >> 4 == 3:                         # PUBLISH
                qos = (head >> 1) & 3
                tl = struct.unpack("!H", body[:2])[0]
                topic = body[2:2 + tl].decode("utf-8", "replace")
                pos = 2 + tl
                if qos:
                    if qos == 1:
                        self._send(0x40, body[pos:pos + 2])    # PUBACK
                    pos += 2
                msgs.append((topic, body[pos:]))
        now = time.monotonic()
        if now - self._last_tx >= self.keepalive / 2:
            self._send(0xC0, b"")                      # PINGREQ
        if now - self._last_rx > self.keepalive * 1.5:
            raise MQTTError("the broker stopped answering")
        return msgs

    def close(self, clean=True):
        if self.sock is None:
            return
        try:
            if clean:
                self._send(0xE0, b"")                  # DISCONNECT (no last will)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None
