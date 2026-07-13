"""TCP/IP transport — lokalni UDS server (BL library test app)."""
from __future__ import annotations
import logging, socket, struct, threading
from typing import Optional
from transport.transport import AbstractTransport, TransportError, crc16_ccitt

log = logging.getLogger(__name__)

class TCPTransport(AbstractTransport):
    """UDS over TCP/IP. Frame: [0xAA][0x55][LEN16][PAYLOAD][CRC16-CCITT]"""

    def __init__(self):
        super().__init__()
        self._sock = None; self._rx_thread = None; self._running = False
        self._event = threading.Event(); self._last = None; self._lock = threading.Lock()

    @property
    def name(self): return "TCP/IP"

    def connect(self, host="127.0.0.1", port=13400, **kw):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((host, port))
            self._sock.settimeout(0.1)
            self._running = True
            self._rx_thread = threading.Thread(target=self._rx, daemon=True)
            self._rx_thread.start()
            log.info("TCP: %s:%d", host, port)
        except Exception as e:
            raise TransportError(f"TCP {host}:{port}: {e}") from e

    def disconnect(self):
        self._running = False
        if self._rx_thread: self._rx_thread.join(1.0)
        if self._sock:
            try: self._sock.close()
            except: pass

    def is_connected(self): return self._sock is not None and self._running

    def _frame(self, p):
        return b"\xAA\x55" + struct.pack(">H", len(p)) + p + struct.pack(">H", crc16_ccitt(p))

    def send(self, payload):
        if not self.is_connected(): raise TransportError("TCP not connected")
        with self._lock: self._sock.sendall(self._frame(payload))

    def send_and_wait(self, payload, timeout=1.0):
        self._event.clear(); self._last = None
        self.send(payload)
        if not self._event.wait(timeout): raise TransportError(f"TCP timeout {timeout}s")
        return self._last

    def _rx(self):
        buf = bytearray()
        while self._running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk: self._running = False; break
                buf.extend(chunk); self._drain(buf)
            except socket.timeout: continue
            except Exception as e:
                if self._running and self._error_callback: self._error_callback(str(e))
                break

    def _drain(self, buf):
        while True:
            i = buf.find(b"\xAA\x55")
            if i < 0: buf.clear(); return
            if i: del buf[:i]
            if len(buf) < 6: return
            n = struct.unpack(">H", buf[2:4])[0]; total = 4 + n + 2
            if len(buf) < total: return
            payload = bytes(buf[4:4+n])
            crc_r = struct.unpack(">H", buf[4+n:total])[0]
            del buf[:total]
            if crc_r != crc16_ccitt(payload): continue
            self._last = payload; self._event.set()
            if self._response_callback: self._response_callback(payload)
