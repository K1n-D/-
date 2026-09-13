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
  ack_time TIMESTAMP NULL,
  ack_by VARCHAR(64) NULL,
  INDEX idx_alarm_device_time(device_code, trigger_time)
);

-- 点位表:采集的事实源。edge-collector 启动时从这里读取采集配置,
-- 监控台的"采集配置"页面负责维护。
CREATE TABLE IF NOT EXISTS iot_point_config (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  device_code VARCHAR(64) NOT NULL,
  point_code VARCHAR(64) NOT NULL,
  slave_id INT NOT NULL DEFAULT 1,
  register INT NOT NULL,
  register_type VARCHAR(16) NOT NULL DEFAULT 'holding',
  data_type VARCHAR(16) NOT NULL DEFAULT 'uint16',
  byte_order VARCHAR(8) NOT NULL DEFAULT 'ABCD',
  register_count INT NOT NULL DEFAULT 1,
  scale_factor DECIMAL(12,8) NOT NULL DEFAULT 1,
  dead_zone DECIMAL(12,6) NOT NULL DEFAULT 0,
  collect_interval_ms INT NOT NULL DEFAULT 1000,
  unit VARCHAR(32) NOT NULL DEFAULT '',
  enabled TINYINT NOT NULL DEFAULT 1,
  UNIQUE KEY uk_device_point(device_code, point_code)
);

INSERT IGNORE INTO iot_point_config
  (device_code, point_code, slave_id, register, register_type, scale_factor, dead_zone, collect_interval_ms, unit)
VALUES
  ('MODBUS-PLC-001', 'temperature', 1, 0, 'holding', 0.1, 0.1, 1000, 'C'),
  ('MODBUS-PLC-001', 'pressure',    1, 1, 'holding', 0.001, 0.002, 1000, 'MPa'),
  ('MODBUS-TANK-001', 'liquid_level', 1, 2, 'holding', 0.1, 0.2, 2000, '%');

-- 告警规则:阈值/滞回/持续时间,由后端规则引擎消费。
CREATE TABLE IF NOT EXISTS iot_alarm_rule (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  device_code VARCHAR(64) NOT NULL,
  point_code VARCHAR(64) NOT NULL,
  operator VARCHAR(8) NOT NULL DEFAULT '>',
  threshold DECIMAL(20,6) NOT NULL,
  duration_sec INT NOT NULL DEFAULT 0,
  hysteresis DECIMAL(20,6) NOT NULL DEFAULT 0,
  level VARCHAR(20) NOT NULL DEFAULT 'SERIOUS',
  enabled TINYINT NOT NULL DEFAULT 1,
  UNIQUE KEY uk_rule(device_code, point_code, operator)
);

INSERT IGNORE INTO iot_alarm_rule
  (device_code, point_code, operator, threshold, duration_sec, hysteresis, level)
VALUES
  ('*', 'temperature', '>', 90, 10, 5, 'SERIOUS');
