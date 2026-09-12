from __future__ import annotations
import json, logging, os, random, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .local_mqtt import Client
from .normalization import normalize, utc_now

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

class Middleware:
    def __init__(self):
        self.client_id="middleware-001"; self.client=None; self.devices={}; self.retries=0; self.running=True
        self.stats={"received":0,"normalized":0,"invalid":0,"duplicates":0,"reconnects":0,"heartbeats":0,"lastMessage":None,"pingResponses":0,"pingTimeouts":0}
        self.mqtt_state="DISCONNECTED"; self.last_ping_response=None; self.seen_events=set(); self.seen_order=deque(maxlen=10000)

    def start(self):
        while self.running:
            try:
                will={"topic":"factory/FACTORY-001/middleware/middleware-001/status","payload":{"clientId":self.client_id,"status":"OFFLINE","reason":"LAST_WILL","timestamp":utc_now()}}
                self.client=Client(self.client_id, keepalive=30, will=will, on_message=self.on_message, on_state=self.on_state, on_control=self.on_control); self.client.connect()
                for topic in ["factory/+/device/+/raw","factory/+/device/+/heartbeat"]: self.client.subscribe(topic)
                self.client.start_keepalive(); self.retries=0; self.client.publish("factory/FACTORY-001/middleware/middleware-001/status", {"clientId":self.client_id,"status":"ONLINE","timestamp":utc_now()}, retain=True)
                logging.info("middleware connected and subscribed")
                while self.client.running and self.running: time.sleep(1)
            except OSError as e: logging.warning("connect failed: %s",e)
            if self.running:
                self.retries += 1; delay=min(60,2**(self.retries-1))*random.uniform(.8,1.2); logging.warning("reconnect attempt=%s delay=%.1fs",self.retries,delay); time.sleep(delay); self.stats["reconnects"]+=1

    def on_state(self, state):
        self.mqtt_state=state; logging.info("mqtt state=%s",state)
    def on_control(self, control):
        if control == "PINGRESP":
            self.stats["pingResponses"] += 1; self.last_ping_response=time.time(); logging.debug("mqtt PINGRESP received")
    def on_message(self, topic, payload):
        self.stats["received"]+=1; self.stats["lastMessage"]=utc_now()
        if topic.endswith("/heartbeat"):
            self.stats["heartbeats"]+=1; self._heartbeat(payload); return
        try:
            data=normalize(payload); self.stats["normalized"]+=1
            event_id=data["eventId"]
            if event_id in self.seen_events:
                self.stats["duplicates"]+=1; logging.info("duplicate eventId=%s ignored",event_id); return
            if len(self.seen_order) == self.seen_order.maxlen:
                self.seen_events.discard(self.seen_order[0])
            self.seen_events.add(event_id); self.seen_order.append(event_id)
            if self.client and self.client.running: self.client.publish("factory/FACTORY-001/telemetry/normalized", data, qos=1)
        except ValueError as e: self.stats["invalid"]+=1; logging.warning("invalid payload: %s",e)

    def _heartbeat(self, payload):
        device=payload.get("deviceCode"); client_id=payload.get("clientId"); sequence=payload.get("sequenceNo")
        if not device or not client_id or sequence is None:
            self.stats["invalid"]+=1; logging.warning("invalid heartbeat identity: %s",payload); return
        try: sequence=int(sequence); sent=float(payload.get("sentEpoch",time.time()))
        except (TypeError, ValueError):
            self.stats["invalid"]+=1; logging.warning("invalid heartbeat sequence/time: %s",payload); return
        previous=self.devices.get(device,{}).get("sequenceNo")
        if previous is not None and sequence == previous:
            self.stats["duplicates"] += 1; logging.info("duplicate heartbeat device=%s sequence=%s ignored", device, sequence); return
        if previous is not None and sequence < previous:
            self.stats["invalid"]+=1; logging.warning("heartbeat sequence rollback device=%s previous=%s current=%s",device,previous,sequence); return
        now=time.time(); info=self.devices.setdefault(device,{})
        info.update({"lastHeartbeat":now,"status":"ONLINE","latencyMs":max(0,int((now-sent)*1000)),"sequenceNo":sequence,"clientId":client_id})
        forwarded={**payload,"receivedTime":utc_now(),"latencyMs":info["latencyMs"],"status":"ONLINE"}
        if self.client and self.client.running: self.client.publish(f"factory/FACTORY-001/device/{device}/heartbeat", forwarded, qos=1)

    def watchdog(self):
        while self.running:
            now=time.time()
            for device, info in list(self.devices.items()):
                age=now-info["lastHeartbeat"]; new="OFFLINE" if age>=30 else ("SUSPECTED" if age>=20 else "ONLINE")
                if new != info["status"]:
                    info["status"]=new; logging.warning("device=%s status=%s age=%.1fs",device,new,age)
                    if self.client and self.client.running: self.client.publish(f"factory/FACTORY-001/device/{device}/status", {"deviceCode":device,"status":new,"reason":"HEARTBEAT_TIMEOUT","timestamp":utc_now()}, retain=True)
            time.sleep(2)

if __name__ == "__main__":
    m=Middleware(); threading.Thread(target=m.watchdog,daemon=True).start()

    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path not in ("/health", "/metrics"):
                self.send_response(404); self.end_headers(); return
            payload={"status":"UP" if m.running else "STOPPED","mqttState":m.mqtt_state,"clientId":m.client_id,"stats":m.stats,"devices":m.devices}
            raw=json.dumps(payload,ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)

    health_port=int(os.getenv("MIDDLEWARE_HEALTH_PORT", "8090"))
    threading.Thread(target=lambda: ThreadingHTTPServer(("127.0.0.1",health_port),HealthHandler).serve_forever(),daemon=True).start()
    logging.info("health endpoint http://127.0.0.1:%s/health",health_port)
    m.start()
