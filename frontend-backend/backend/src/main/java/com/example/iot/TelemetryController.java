package com.example.iot;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import java.util.*;

@RestController
@RequestMapping("/api")
@CrossOrigin
public class TelemetryController {
    private final TelemetryStore store;
    public TelemetryController(TelemetryStore store) { this.store = store; }
    @GetMapping("/health") public Map<String,Object> health() { return Map.of("status","UP","runtime","spring-boot"); }
    @GetMapping("/devices") public Collection<Map<String,Object>> devices() { return store.devices(); }
    @GetMapping("/dashboard/statistics") public Map<String,Object> stats() { return store.stats(); }
    @GetMapping("/history") public List<TelemetryStore.Telemetry> history() { return store.history(); }
    @PostMapping("/auth/login") public Map<String,Object> login() { return Map.of("token","local-demo-token","user",Map.of("username","admin","role","ADMIN")); }
    @PostMapping("/internal/telemetry") public ResponseEntity<Map<String,Object>> ingest(@RequestBody Map<String,Object> body) {
        try {
            var t = new TelemetryStore.Telemetry(String.valueOf(body.getOrDefault("eventId",UUID.randomUUID())), String.valueOf(body.get("deviceCode")), String.valueOf(body.get("pointCode")), Double.parseDouble(String.valueOf(body.get("value"))), String.valueOf(body.getOrDefault("unit","")), String.valueOf(body.getOrDefault("timestamp","")), String.valueOf(body.getOrDefault("quality","GOOD")), String.valueOf(body.getOrDefault("sourceProtocol","MQTT")));
            return ResponseEntity.ok(Map.of("accepted",store.add(t)));
        } catch (Exception e) { return ResponseEntity.badRequest().body(Map.of("accepted",false,"error",e.getMessage())); }
    }
}
