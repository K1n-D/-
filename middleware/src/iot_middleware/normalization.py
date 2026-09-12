from datetime import datetime, timezone
import math
import uuid

def utc_now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def normalize(payload, protocol="MQTT"):
    if not isinstance(payload, dict): raise ValueError("payload must be a JSON object")
    if not payload.get("deviceCode") or not payload.get("pointCode"): raise ValueError("deviceCode/pointCode required")
    value = payload.get("value", payload.get("rawValue"))
    if value is None: raise ValueError("value required")
    try: value = float(value) if not isinstance(value,bool) else value
    except (TypeError, ValueError): raise ValueError("value must be numeric or boolean")
    # NaN/Infinity are not valid JSON literals; they would break every JSON
    # consumer downstream (browser response.json() refuses to parse them).
    if isinstance(value, float) and not math.isfinite(value): raise ValueError("value must be finite")
    return {"eventId": payload.get("eventId") or str(uuid.uuid4()), "deviceCode":payload["deviceCode"], "pointCode":payload["pointCode"], "value":value, "unit":payload.get("unit",""), "timestamp":payload.get("sentTime") or utc_now(), "quality":payload.get("quality","GOOD"), "sourceProtocol":protocol, "rawPayload":payload}
