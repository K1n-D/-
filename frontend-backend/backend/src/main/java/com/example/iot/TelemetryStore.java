package com.example.iot;

import org.springframework.stereotype.Component;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;

@Component
public class TelemetryStore {
    public record Telemetry(String eventId, String deviceCode, String pointCode, double value, String unit, String timestamp, String quality, String sourceProtocol) {}
    private final Set<String> eventIds = ConcurrentHashMap.newKeySet();
    private final List<Telemetry> history = new CopyOnWriteArrayList<>();
    private final Map<String, Map<String,Object>> devices = new ConcurrentHashMap<>();
    public boolean add(Telemetry t) {
        if (!eventIds.add(t.eventId())) return false;
        history.add(t); devices.computeIfAbsent(t.deviceCode(), k -> new ConcurrentHashMap<>(Map.of("deviceCode", k, "status", "ONLINE")));
        devices.get(t.deviceCode()).put("lastDataTime", t.timestamp());
        return true;
    }
    public List<Telemetry> history() { return history.stream().skip(Math.max(0, history.size()-200)).toList(); }
    public Collection<Map<String,Object>> devices() { return devices.values(); }
    public Map<String,Object> stats() { long online=devices.values().stream().filter(d -> "ONLINE".equals(d.get("status"))).count(); return Map.of("deviceTotal", devices.size(), "online", online, "offline", devices.size()-online, "messages", history.size()); }
    public void touch(String code, String status) { devices.computeIfAbsent(code,k->new ConcurrentHashMap<>(Map.of("deviceCode",k))).put("status", status); devices.get(code).put("lastHeartbeat", Instant.now().toString()); }
}
