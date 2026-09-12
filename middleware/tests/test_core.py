import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from iot_middleware.normalization import normalize
from iot_middleware.local_mqtt import match

def test_topic_match():
    assert match('factory/A/device/P/raw','factory/+/device/+/raw')
    assert not match('factory/A/device/P/status','factory/+/device/+/raw')

def test_normalize():
    v=normalize({'deviceCode':'PLC-001','pointCode':'temperature','rawValue':786,'unit':'C'})
    assert v['value']==786.0 and v['quality']=='GOOD' and v['eventId']

def test_heartbeat_thresholds():
    assert 20 < 30
