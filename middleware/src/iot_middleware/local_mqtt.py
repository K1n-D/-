"""Small MQTT 3.1.1 implementation used for the offline Windows demo.

It intentionally implements only the packet types needed by this project, but
uses the real MQTT wire format so Paho and other standard MQTT clients can
interoperate without an external broker installation.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid

HOST, PORT = "127.0.0.1", 1883


def match(topic: str, pattern: str) -> bool:
    """Return whether an MQTT topic matches a +/# subscription filter."""
    t, p = topic.split("/"), pattern.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return i == len(p) - 1
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(t) == len(p)


def _remaining_length(value: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = value % 128
        value //= 128
        if value:
            digit |= 0x80
        encoded.append(digit)
        if not value:
            return bytes(encoded)


def _read_remaining(sock: socket.socket) -> int:
    multiplier, value = 1, 0
    for _ in range(4):
        raw = sock.recv(1)
        if not raw:
            raise ConnectionError("socket closed while reading remaining length")
        digit = raw[0]
        value += (digit & 127) * multiplier
        if not digit & 128:
            return value
        multiplier *= 128
    raise ValueError("invalid MQTT remaining length")


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks, remaining = [], size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed while reading packet")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_packet(sock: socket.socket) -> tuple[int, bytes]:
    first = _read_exact(sock, 1)[0]
    return first, _read_exact(sock, _read_remaining(sock))


def _packet(first: int, payload: bytes = b"") -> bytes:
    return bytes([first]) + _remaining_length(len(payload)) + payload


def _utf(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _take_utf(payload: bytes, index: int) -> tuple[str, int]:
    size = int.from_bytes(payload[index:index + 2], "big")
    index += 2
    return payload[index:index + size].decode("utf-8"), index + size


def _json_payload(payload: bytes) -> dict:
    try:
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else {"value": value}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"rawPayload": payload.decode("utf-8", errors="replace")}


class Broker:
    def __init__(self, host=HOST, port=PORT):
        self.host, self.port = host, port
        self.clients: dict[str, dict] = {}
        self.retained: dict[str, dict] = {}
        self.lock = threading.RLock()

    def serve(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(50)
        server.settimeout(1)
        print(f"[broker] MQTT 3.1.1 listening on {self.host}:{self.port}", flush=True)
        threading.Thread(target=self._keepalive_watchdog, daemon=True).start()
        try:
            while True:
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._client, args=(conn,), daemon=True).start()
        finally:
            server.close()

    def _keepalive_watchdog(self):
        """Disconnect clients that miss their keepalive deadline.

        Without this, a half-open connection stays in self.clients forever:
        it is only noticed when the next publish to that client fails.
        """
        while True:
            time.sleep(1)
            now = time.time()
            with self.lock:
                stale = [client for client in self.clients.values()
                         if client["keepalive"] and now - client.get("last_seen", now) > client["keepalive"] * 1.5 + 1]
            for client in stale:
                try:
                    client["socket"].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    @staticmethod
    def _send(item: dict, packet: bytes):
        try:
            with item["send_lock"]:
                item["socket"].sendall(packet)
        except OSError:
            item["closed"] = True

    def _client(self, conn: socket.socket):
        item = None
        clean_disconnect = False
        try:
            first, payload = _read_packet(conn)
            if first >> 4 != 1:
                raise ValueError("first MQTT packet must be CONNECT")
            client_id, will, keepalive = self._parse_connect(payload)
            item = {"socket": conn, "send_lock": threading.Lock(), "clientId": client_id,
                    "subscriptions": [], "will": will, "closed": False, "keepalive": keepalive}
            with self.lock:
                previous = self.clients.get(client_id)
                if previous:
                    previous["closed"] = True
                    try: previous["socket"].shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                self.clients[client_id] = item
            self._send(item, _packet(0x20, b"\x00\x00"))
            while not item["closed"]:
                item["last_seen"] = time.time()
                first, payload = _read_packet(conn)
                packet_type, flags = first >> 4, first & 0x0F
                if packet_type == 8:  # SUBSCRIBE
                    self._subscribe(item, payload)
                elif packet_type == 3:  # PUBLISH
                    packet_id = self._publish(item, flags, payload)
                    if flags >> 1 & 0x03 == 1 and packet_id is not None:
                        self._send(item, _packet(0x40, packet_id.to_bytes(2, "big")))
                elif packet_type == 12:  # PINGREQ
                    self._send(item, _packet(0xD0))
                elif packet_type == 14:  # DISCONNECT
                    clean_disconnect = True
                    break
                elif packet_type == 4:  # PUBACK from a client (QoS 2 not used here)
                    continue
                else:
                    raise ValueError(f"unsupported MQTT packet type {packet_type}")
        except (ConnectionError, OSError, ValueError, UnicodeError):
            pass
        finally:
            if item:
                with self.lock:
                    if self.clients.get(item["clientId"]) is item:
                        self.clients.pop(item["clientId"], None)
                # Only a received DISCONNECT suppresses the will.  A failed
                # send sets item["closed"] as well, and that is exactly the
                # abnormal-disconnect case where the will must still fire.
                if item.get("will") and not clean_disconnect:
                    will = item["will"]
                    self.publish(will["topic"], will["payload"], qos=1, retain=will["retain"])
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _parse_connect(payload: bytes) -> tuple[str, dict | None, int]:
        protocol, index = _take_utf(payload, 0)
        if protocol != "MQTT" or index + 4 > len(payload):
            raise ValueError("unsupported MQTT protocol")
        level, flags, keepalive = payload[index], payload[index + 1], int.from_bytes(payload[index + 2:index + 4], "big")
        index += 4
        client_id, index = _take_utf(payload, index)
        will = None
        if flags & 0x04:
            will_topic, index = _take_utf(payload, index)
            will_raw, index = _take_utf(payload, index)
            will = {"topic": will_topic, "payload": _json_payload(will_raw.encode("utf-8")), "retain": bool(flags & 0x20)}
        if flags & 0x80:  # username
            _, index = _take_utf(payload, index)
        if flags & 0x40:  # password
            _, index = _take_utf(payload, index)
        if level != 4:
            raise ValueError("only MQTT 3.1.1 is supported")
        return client_id or str(uuid.uuid4()), will, keepalive

    def _subscribe(self, item: dict, payload: bytes):
        packet_id = int.from_bytes(payload[:2], "big")
        index, filters = 2, []
        while index < len(payload):
            topic, index = _take_utf(payload, index)
            if index >= len(payload):
                raise ValueError("invalid SUBSCRIBE payload")
            requested_qos = min(payload[index], 1)
            index += 1
            item["subscriptions"].append(topic)
            filters.append((topic, requested_qos))
        self._send(item, _packet(0x90, packet_id.to_bytes(2, "big") + bytes(qos for _, qos in filters)))
        with self.lock:
            retained = [(topic, value) for topic, value in self.retained.items()
                        if any(match(topic, pattern) for pattern, _ in filters)]
        for topic, value in retained:
            self._send(item, self._publish_packet(topic, value, qos=1, retain=True))

    def _publish(self, sender: dict, flags: int, payload: bytes) -> int | None:
        qos = (flags >> 1) & 0x03
        index = 0
        topic, index = _take_utf(payload, index)
        packet_id = None
        if qos:
            packet_id = int.from_bytes(payload[index:index + 2], "big")
            index += 2
        self.publish(topic, _json_payload(payload[index:]), qos=min(qos, 1), retain=bool(flags & 1))
        return packet_id

    @staticmethod
    def _publish_packet(topic: str, payload: dict, qos=1, retain=False) -> bytes:
        body = _utf(topic)
        if qos:
            body += b"\x00\x01"
        body += json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return _packet(0x30 | ((min(qos, 1) & 3) << 1) | int(retain), body)

    def publish(self, topic, payload, qos=1, retain=False):
        if retain:
            with self.lock:
                self.retained[topic] = payload
        with self.lock:
            targets = [client for client in self.clients.values()
                       if any(match(topic, pattern) for pattern in client["subscriptions"])]
        packet = self._publish_packet(topic, payload, qos=qos, retain=retain)
        for client in targets:
            self._send(client, packet)


class Client:
    def __init__(self, client_id, host=HOST, port=PORT, keepalive=30, will=None,
                 on_message=None, on_state=None, on_control=None):
        self.client_id, self.host, self.port, self.keepalive = client_id, host, port, keepalive
        self.will, self.on_message, self.on_state, self.on_control = will, on_message, on_state, on_control
        self.sock: socket.socket | None = None
        self.running = False
        self._reader = None
        self._send_lock = threading.Lock()
        self._packet_id = 0
        self._inflight: dict[int, tuple[bytes, float]] = {}  # packet_id -> (packet, sent_time)

    def connect(self, timeout=5):
        self.sock = socket.create_connection((self.host, self.port), timeout)
        self.sock.settimeout(None)
        flags = 0x02  # clean session = true (the bundled broker keeps no session state)
        if self.will:
            flags |= 0x04 | (0x08 if self.will.get("qos", 1) == 1 else 0) | (0x20 if self.will.get("retain", True) else 0)
        body = _utf("MQTT") + bytes([4, flags]) + int(self.keepalive).to_bytes(2, "big") + _utf(self.client_id)
        if self.will:
            body += _utf(self.will["topic"]) + _utf(json.dumps(self.will["payload"], ensure_ascii=False, separators=(",", ":")))
        self.sock.sendall(_packet(0x10, body))
        first, payload = _read_packet(self.sock)
        if first != 0x20 or payload != b"\x00\x00":
            raise ConnectionError("MQTT broker rejected CONNECT")
        self.running = True
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        threading.Thread(target=self._resend_loop, daemon=True).start()
        if self.on_state:
            self.on_state("CONNECTED")

    def _send(self, packet: bytes):
        if not self.sock or not self.running:
            return
        try:
            with self._send_lock:
                self.sock.sendall(packet)
        except OSError:
            self._closed()

    def _next_packet_id(self) -> int:
        self._packet_id = (self._packet_id % 65535) + 1
        return self._packet_id

    def subscribe(self, topic):
        packet_id = self._next_packet_id()
        self._send(_packet(0x82, packet_id.to_bytes(2, "big") + _utf(topic) + b"\x01"))

    def publish(self, topic, payload, qos=1, retain=False):
        body = _utf(topic)
        packet_id = None
        if qos:
            packet_id = self._next_packet_id()
            body += packet_id.to_bytes(2, "big")
        body += json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        packet = _packet(0x30 | ((min(qos, 1) & 3) << 1) | int(retain), body)
        if qos and packet_id is not None:
            self._inflight[packet_id] = (packet, time.time())
        self._send(packet)

    def _resend_loop(self, interval=2.0, timeout=5.0):
        """Redeliver unacknowledged QoS 1 publishes with the DUP flag set.

        This upgrades the client side to real at-least-once semantics for the
        publish leg; deduplication downstream stays the subscriber's job.
        """
        while self.running:
            time.sleep(interval)
            now = time.time()
            for packet_id, (packet, sent) in list(self._inflight.items()):
                if now - sent < timeout:
                    continue
                dup_packet = bytes([packet[0] | 0x08]) + packet[1:]
                self._inflight[packet_id] = (dup_packet, now)
                self._send(dup_packet)

    def _read(self):
        try:
            while self.running and self.sock:
                # Poll with a timeout instead of blocking forever so a clean
                # disconnect() can stop this thread before the socket closes;
                # closing a socket with a recv() pending is an abortive close
                # on Windows and would swallow the outbound DISCONNECT packet.
                try:
                    self.sock.settimeout(0.5)
                    first = self.sock.recv(1)
                except socket.timeout:
                    continue
                except (ConnectionError, OSError):
                    raise
                if not first:
                    raise ConnectionError("socket closed while reading packet")
                self.sock.settimeout(None)
                packet_type = first[0] >> 4
                payload = _read_exact(self.sock, _read_remaining(self.sock))
                if packet_type == 3:
                    qos = (first[0] >> 1) & 0x03
                    topic, index = _take_utf(payload, 0)
                    packet_id = None
                    if qos:
                        packet_id = int.from_bytes(payload[index:index + 2], "big")
                        index += 2
                    if self.on_message:
                        self.on_message(topic, _json_payload(payload[index:]))
                    if qos == 1 and packet_id is not None:
                        self._send(_packet(0x40, packet_id.to_bytes(2, "big")))
                elif packet_type == 13 and self.on_control:
                    self.on_control("PINGRESP")
                elif packet_type == 4:  # PUBACK: our QoS 1 publish was acknowledged
                    if len(payload) >= 2:
                        self._inflight.pop(int.from_bytes(payload[:2], "big"), None)
                    if self.on_control:
                        self.on_control("PUBACK")
                elif self.on_control:
                    self.on_control({1: "CONNACK", 9: "SUBACK"}.get(packet_type, str(packet_type)))
        except (ConnectionError, OSError, ValueError, UnicodeError):
            pass
        self._closed()

    def start_keepalive(self):
        def loop():
            while self.running:
                time.sleep(max(1, self.keepalive / 2))
                if self.running:
                    self._send(_packet(0xC0))
        threading.Thread(target=loop, daemon=True).start()

    def _closed(self):
        if not self.running:
            return
        self.running = False
        self._inflight.clear()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        if self.on_state:
            self.on_state("DISCONNECTED")

    def disconnect(self):
        if not self.running:
            return
        self._send(_packet(0xE0))
        if not self.running:  # the send failed; _closed() already cleaned up
            return
        # Stop the reader before closing the socket so the DISCONNECT packet
        # is actually delivered (see the note in _read about abortive closes).
        self.running = False
        reader = self._reader
        if reader and reader is not threading.current_thread() and reader.is_alive():
            reader.join(timeout=2)
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        if self.on_state:
            self.on_state("DISCONNECTED")
