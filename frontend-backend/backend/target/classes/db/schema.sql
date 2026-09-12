CREATE TABLE IF NOT EXISTS iot_device (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  device_code VARCHAR(64) NOT NULL UNIQUE,
  device_name VARCHAR(128) NOT NULL,
  protocol VARCHAR(32) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN',
  last_heartbeat TIMESTAMP NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS iot_history_data (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  event_id VARCHAR(128) NOT NULL UNIQUE,
  device_code VARCHAR(64) NOT NULL,
  point_code VARCHAR(64) NOT NULL,
  value_decimal DECIMAL(20,6) NULL,
  value_text VARCHAR(128) NULL,
  unit VARCHAR(32),
  quality VARCHAR(20) NOT NULL,
  source_protocol VARCHAR(32) NOT NULL,
  collect_time TIMESTAMP NOT NULL,
  INDEX idx_device_point_time(device_code, point_code, collect_time),
  INDEX idx_collect_time(collect_time)
);

CREATE TABLE IF NOT EXISTS iot_heartbeat_log (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  device_code VARCHAR(64) NOT NULL,
  client_id VARCHAR(128),
  sequence_no BIGINT,
  sent_time TIMESTAMP NULL,
  received_time TIMESTAMP NOT NULL,
  latency_ms BIGINT,
  status VARCHAR(20) NOT NULL,
  payload JSON,
  INDEX idx_heartbeat_device_time(device_code, received_time)
);

CREATE TABLE IF NOT EXISTS iot_device_status_log (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  device_code VARCHAR(64) NOT NULL,
  old_status VARCHAR(20),
  new_status VARCHAR(20) NOT NULL,
  reason VARCHAR(128),
  event_time TIMESTAMP NOT NULL,
  INDEX idx_status_device_time(device_code, event_time)
);

CREATE TABLE IF NOT EXISTS iot_alarm_record (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  device_code VARCHAR(64) NOT NULL,
  point_code VARCHAR(64),
  alarm_type VARCHAR(32) NOT NULL,
  alarm_level VARCHAR(20) NOT NULL,
  trigger_value DECIMAL(20,6),
  threshold_value DECIMAL(20,6),
  status VARCHAR(20) NOT NULL,
  trigger_time TIMESTAMP NOT NULL,
  recover_time TIMESTAMP NULL,
  INDEX idx_alarm_device_time(device_code, trigger_time)
);
