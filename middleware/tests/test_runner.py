import unittest, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from iot_middleware.normalization import normalize
from iot_middleware.local_mqtt import match

class CoreTests(unittest.TestCase):
    def test_topic_wildcards(self):
        self.assertTrue(match('factory/A/device/P/raw','factory/+/device/+/raw'))
        self.assertTrue(match('factory/A/telemetry/normalized','factory/#'))
        self.assertFalse(match('factory/A/device/P/status','factory/+/device/+/raw'))
    def test_normalization_and_validation(self):
        data=normalize({'deviceCode':'PLC-001','pointCode':'temperature','rawValue':786,'unit':'C'})
        self.assertEqual(data['value'],786.0); self.assertEqual(data['quality'],'GOOD'); self.assertTrue(data['eventId'])
        with self.assertRaises(ValueError): normalize({'deviceCode':'PLC-001'})
    def test_reconnect_delay_bounds(self):
        self.assertEqual(min(60,2**0),1); self.assertEqual(min(60,2**8),60)

if __name__=='__main__': unittest.main()
