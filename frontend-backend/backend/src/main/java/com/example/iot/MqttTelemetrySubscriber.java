package com.example.iot;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PreDestroy;
import org.eclipse.paho.client.mqttv3.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.util.Map;

/** Optional real MQTT subscriber. Disabled by default for the dependency-free local demo. */
@Component
public class MqttTelemetrySubscriber {
    private final TelemetryStore store;
    private final ObjectMapper mapper = new ObjectMapper();
    private MqttClient client;
    @Value("${app.mqtt.enabled:false}") private boolean enabled;
    @Value("${app.mqtt.host:tcp://127.0.0.1:1883}") private String host;
    @Value("${app.mqtt.client-id:java-backend-001}") private String clientId;
    @Value("${app.mqtt.keep-alive:30}") private int keepAlive;
    @Value("${app.mqtt.topic:factory/+/telemetry/normalized}") private String topic;

    public MqttTelemetrySubscriber(TelemetryStore store) { this.store = store; }

    @EventListener(ApplicationReadyEvent.class)
    public void connect() {
        if (!enabled) return;
        try {
            client = new MqttClient(host, clientId);
            var options = new MqttConnectOptions();
            options.setAutomaticReconnect(true); options.setCleanSession(false); options.setKeepAliveInterval(keepAlive);
            client.setCallback(new MqttCallback() {
                public void connectionLost(Throwable cause) { }
                public void messageArrived(String t, MqttMessage message) {
                    try {
                        Map<String,Object> body = mapper.readValue(message.getPayload(), new TypeReference<>() {});
                        store.add(new TelemetryStore.Telemetry(String.valueOf(body.get("eventId")), String.valueOf(body.get("deviceCode")), String.valueOf(body.get("pointCode")), Double.parseDouble(String.valueOf(body.get("value"))), String.valueOf(body.getOrDefault("unit","")), String.valueOf(body.getOrDefault("timestamp","")), String.valueOf(body.getOrDefault("quality","GOOD")), String.valueOf(body.getOrDefault("sourceProtocol","MQTT"))));
                    } catch (Exception ignored) { }
                }
                public void deliveryComplete(IMqttDeliveryToken token) { }
            });
            client.connect(options); client.subscribe(topic, 1);
        } catch (MqttException ignored) { }
    }

    @PreDestroy
    public void close() { try { if (client != null && client.isConnected()) client.disconnect(); } catch (MqttException ignored) { } }
}
