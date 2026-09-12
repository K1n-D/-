import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from iot_middleware.local_mqtt import Broker, Client, _packet, _utf, match


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class MatchTests(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(match("a/b/c", "a/b/c"))
        self.assertFalse(match("a/b/c", "a/b/c/d"))
        self.assertFalse(match("a/b/c/d", "a/b/c"))

    def test_single_level_wildcard(self):
        self.assertTrue(match("a/x/c", "a/+/c"))
        self.assertFalse(match("a/x/y/c", "a/+/c"))
        self.assertTrue(match("a/b", "+/b"))

    def test_multi_level_wildcard(self):
        self.assertTrue(match("a/b/c", "a/#"))
        self.assertTrue(match("a", "a/#"))  # parent level matches per spec
        self.assertFalse(match("x/b/c", "a/#"))


class BrokerIntegration:
    def setUp(self):
        self.port = free_port()
        self.broker = Broker(port=self.port)
        self.thread = threading.Thread(target=self.broker.serve, daemon=True)
        self.thread.start()
        self._clients = []
        time.sleep(0.2)

    def tearDown(self):
        for client in getattr(self, "_clients", []):
            try:
                client.disconnect()
            except Exception:
                pass
        # The serve loop has no stop flag; leaving the daemon thread running
        # is fine for the test process lifetime.


class BrokerTests(BrokerIntegration, unittest.TestCase):
    def _client(self, client_id, **kwargs):
        kwargs.setdefault("host", "127.0.0.1")
        kwargs["port"] = self.port
        client = Client(client_id, **kwargs)
        client.connect()
        self._clients.append(client)
        return client

    def test_pingreq_gets_pingresp(self):
        controls = []
        client = self._client("probe", keepalive=2, on_control=controls.append)
        client.start_keepalive()
        deadline = time.time() + 5
        while time.time() < deadline and controls.count("PINGRESP") < 1:
            time.sleep(0.1)
        self.assertGreaterEqual(controls.count("PINGRESP"), 1)

    def test_publish_is_delivered_to_subscriber(self):
        received = []
        subscriber = self._client("sub-1")
        subscriber.subscribe("factory/f1/device/+/raw")
        subscriber.on_message = lambda topic, payload: received.append((topic, payload))
        publisher = self._client("pub-1")
        publisher.publish("factory/f1/device/PLC-001/raw", {"value": 42})
        deadline = time.time() + 3
        while time.time() < deadline and not received:
            time.sleep(0.05)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "factory/f1/device/PLC-001/raw")
        self.assertEqual(received[0][1]["value"], 42)

    def test_retained_message_is_replayed_on_subscribe(self):
        publisher = self._client("pub-2")
        publisher.publish("state/device/PLC-001", {"status": "ONLINE"}, retain=True)
        received = []
        subscriber = self._client("sub-2")
        subscriber.on_message = lambda topic, payload: received.append((topic, payload))
        subscriber.subscribe("state/#")
        deadline = time.time() + 3
        while time.time() < deadline and not received:
            time.sleep(0.05)
        self.assertEqual(received and received[0][1]["status"], "ONLINE")

    def test_qos1_publish_is_acknowledged_and_cleared(self):
        controls = []
        publisher = self._client("pub-3", on_control=controls.append)
        publisher.publish("factory/f1/telemetry", {"value": 1}, qos=1)
        deadline = time.time() + 3
        while time.time() < deadline and controls.count("PUBACK") < 1:
            time.sleep(0.05)
        self.assertGreaterEqual(controls.count("PUBACK"), 1)
        self.assertEqual(publisher._inflight, {})

    def test_unacked_qos1_publish_is_redelivered_with_dup(self):
        # Fabricate a QoS 1 publish whose PUBACK never arrived (as if lost);
        # the resend loop must redeliver it with the DUP flag set, and the
        # broker's PUBACK for that redelivery must then clear the entry.
        publisher = self._client("pub-4")
        dup_sends = []
        original_send = publisher._send
        def spy(packet):
            if packet[0] & 0x08:
                dup_sends.append(packet)
            return original_send(packet)
        publisher._send = spy
        body = _utf("factory/f1/telemetry") + (1).to_bytes(2, "big") + b'{"value":1}'
        publisher._inflight[1] = (_packet(0x32, body), time.time() - 10)
        deadline = time.time() + 5
        while time.time() < deadline and not dup_sends:
            time.sleep(0.1)
        self.assertTrue(dup_sends, "resend loop did not redeliver with DUP flag")
        deadline = time.time() + 3
        while time.time() < deadline and 1 in publisher._inflight:
            time.sleep(0.05)
        self.assertNotIn(1, publisher._inflight, "PUBACK should clear the in-flight entry")

    def test_will_fires_on_abnormal_disconnect(self):
        received = []
        watcher = self._client("watch-1")
        watcher.subscribe("will/#")
        watcher.on_message = lambda topic, payload: received.append((topic, payload))
        will = {"topic": "will/device/PLC-001", "payload": {"status": "OFFLINE"}}
        dying = Client("dying-1", port=self.port, will=will)
        dying.connect()
        self._clients.append(dying)
        time.sleep(0.2)
        dying.sock.close()  # abnormal: no DISCONNECT packet
        deadline = time.time() + 3
        while time.time() < deadline and not received:
            time.sleep(0.05)
        self.assertTrue(received and received[0][1]["status"] == "OFFLINE", "will not published")

    def test_will_suppressed_on_clean_disconnect(self):
        received = []
        watcher = self._client("watch-2")
        watcher.subscribe("will/#")
        watcher.on_message = lambda topic, payload: received.append((topic, payload))
        will = {"topic": "will/device/PLC-002", "payload": {"status": "OFFLINE"}}
        leaving = Client("leaving-1", port=self.port, will=will)
        leaving.connect()
        self._clients.append(leaving)
        time.sleep(0.2)
        leaving.disconnect()
        time.sleep(1.0)
        self.assertEqual(received, [])

    def test_keepalive_timeout_disconnects_idle_client(self):
        client = self._client("idle-1", keepalive=1)  # never sends PINGREQ
        time.sleep(3.0)
        self.assertFalse(client.running, "broker should have dropped the idle client")


if __name__ == "__main__":
    unittest.main()
