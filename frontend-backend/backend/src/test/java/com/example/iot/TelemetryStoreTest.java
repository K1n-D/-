package com.example.iot;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class TelemetryStoreTest {
    @Test
    void duplicateEventIdsAreIgnored() {
        var store = new TelemetryStore();
        var a = new TelemetryStore.Telemetry("evt-1", "PLC-001", "temperature", 78.2, "C", "2026-09-04T12:00:00Z", "GOOD", "MQTT");
        assertTrue(store.add(a));
        assertFalse(store.add(a));
        assertEquals(1, store.history().size());
    }

    @Test
    void statsReflectDeviceStatus() {
        var store = new TelemetryStore();
        store.touch("PLC-001", "ONLINE");
        store.touch("PLC-002", "OFFLINE");
        assertEquals(2, store.stats().get("deviceTotal"));
        assertEquals(1L, store.stats().get("online"));
        assertEquals(1L, store.stats().get("offline"));
    }
}
