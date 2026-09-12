$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\middleware\src"
@'
import json, time
import paho.mqtt.client as mqtt

received = []
connected = []

def on_connect(client, userdata, flags, reason_code, properties=None):
    connected.append(str(reason_code))
    client.subscribe("interop/roundtrip", qos=1)
    client.publish("interop/roundtrip", json.dumps({"ok": True}), qos=1)

def on_message(client, userdata, message):
    received.append(json.loads(message.payload.decode()))

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="paho-interop-probe", protocol=mqtt.MQTTv311)
client.on_connect = on_connect
client.on_message = on_message
client.connect("127.0.0.1", 1883, keepalive=2)
client.loop_start()
deadline = time.time() + 5
while time.time() < deadline and not received:
    time.sleep(0.1)
client.loop_stop()
client.disconnect()
print("PAHO_CONNECTED=", connected)
print("PAHO_RECEIVED=", received)
assert connected == ["Success"] and received == [{"ok": True}]

will_seen = []
def on_will_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe("interop/will", qos=1)
def on_will_message(client, userdata, message):
    will_seen.append(json.loads(message.payload.decode()))
subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="paho-will-subscriber", protocol=mqtt.MQTTv311)
subscriber.on_connect = on_will_connect
subscriber.on_message = on_will_message
subscriber.connect("127.0.0.1", 1883, keepalive=5)
subscriber.loop_start()
time.sleep(0.5)
publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="paho-will-publisher", protocol=mqtt.MQTTv311)
publisher.will_set("interop/will", json.dumps({"status": "OFFLINE", "reason": "LAST_WILL"}), qos=1, retain=True)
publisher.connect("127.0.0.1", 1883, keepalive=5)
publisher.loop_start()
time.sleep(0.5)
publisher._sock_close()
deadline = time.time() + 4
while time.time() < deadline and not will_seen:
    time.sleep(0.1)
publisher.loop_stop()
subscriber.loop_stop()
subscriber.disconnect()
print("PAHO_WILL=", will_seen)
assert will_seen and will_seen[0]["reason"] == "LAST_WILL"
'@ | python -
