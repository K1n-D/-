# 基于通信中间件的工业设备数据采集与监控系统

这是一个面向 Windows 本地运行的全链路演示项目，包含三个核心目录：

- `frontend-backend`：本地 Python 业务 API 和静态 Web 监控页面；接口边界与规划中的 Spring Boot/Vue 版本一致。
- `middleware`：本地 MQTT 主题总线、协议层 PINGREQ/PINGRESP、业务心跳监测、标准化和指数退避重连。
- `simulated-devices`：PLC/传感器数据、业务心跳、遗嘱消息和异常场景模拟（MQTT 直连接入方式）。
- `edge-collector`：Modbus TCP 边缘采集器，按点位表轮询 PLC/仪表并转换为 MQTT 遥测（真实工业接入方式），附带 Modbus 从站模拟器。

模拟设备还提供独立控制 API（`127.0.0.1:8091`），可在 Web 监控台的"虚拟设备"页面中动态添加、编辑、启停和删除设备；变更会经 MQTT 同步到中间件和设备状态页面。

Modbus 接入路径：`Modbus 从站(1502) → edge-collector 按点位表轮询 → MQTT → 中间件标准化 → 后端`。点位表在 `edge-collector/configs/point-table.yml`：每条配置声明寄存器地址、`scale_factor`（定点寄存器→工程值）、`dead_zone`（变化死区，低于死区不重复上报）和采集周期；采集器同时代发设备心跳并在采集失败时发布 OFFLINE 状态。`start-all.ps1` 会自动拉起从站模拟器与采集器，新增设备只需修改点位表并重启采集器。

当前运行时只依赖 Python 3.10+，不需要 Docker、Java、Maven、Node、MySQL 或第三方 Python 包即可验证通信核心。后续安装 Mosquitto、Paho MQTT、MySQL 和 Java 后，可按同样的 topic/API 边界替换对应适配器。

如需下载便携开发工具，可执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-tools.ps1
```

脚本将工具放到 `work/tools`，不会修改系统 PATH。当前网络受限时可使用 `-Resume` 续传；未完成的压缩包不要直接用于启动。

## 启动

在 PowerShell 中执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\check-environment.ps1
.\scripts\start-all.ps1
```

浏览器打开 `http://127.0.0.1:5173`。停止服务：

```powershell
.\scripts\stop-all.ps1
```

## 断连演示

```powershell
.\scripts\test-disconnect.ps1
```

该脚本停止设备 35 秒，观察中间件日志中的 `SUSPECTED/OFFLINE`，随后重启设备，观察 `ONLINE` 和数据恢复。也可以单独启动异常场景：

```powershell
$env:PYTHONPATH = "$PWD\middleware\src;$PWD\simulated-devices\src"
python -m sim_devices.main --scenario high-temperature
python -m sim_devices.main --scenario heartbeat-loss
```

## 测试

```powershell
.\scripts\run-tests.ps1
```

覆盖:MQTT 协议栈(通配符匹配、QoS 1 ACK/DUP 重发、retain 重放、遗嘱触发/抑制、keepalive 超时)、中间件去重与心跳序列、设备控制 API、后端消息处理(NaN 拒绝、告警生命周期、注册表过滤),以及可选的 Paho 互操作、MySQL schema 校验和 Maven 测试。

## 核心参数

- MQTT Keep Alive:30 秒,客户端自动发送 PINGREQ,Broker 返回 PINGRESP;超过 1.5 倍 Keep Alive 无报文的连接会被 Broker 强制断开并触发遗嘱。
- 业务心跳:10 秒一次;20 秒进入 SUSPECTED,30 秒进入 OFFLINE。
- 重连退避:1、2、4、8、16、32、60 秒,并加入 0.8~1.2 随机抖动。
- 数据 QoS 语义:至少一次投递 —— QoS 1 发布 5 秒内未收到 PUBACK 会带 DUP 标志重发;`eventId` 用于业务去重。
- 遗嘱消息:设备非正常断开时发布 retained OFFLINE 状态;正常 DISCONNECT 不触发遗嘱。
- 告警规则:`temperature` 测点超过 90 触发 SERIOUS 告警,回落到阈值以下自动恢复,并持久化到 `iot_alarm_record`。
- 历史数据:`/api/history` 优先查询 MySQL(`iot_history_data`),数据库不可用时回退内存最近 100 条。

常用环境变量:`IOT_ADMIN_USER` / `IOT_ADMIN_PASSWORD`(登录凭据,默认 admin/admin123)、`IOT_ALARM_TEMPERATURE_THRESHOLD`(告警阈值,默认 90)、`IOT_AUTH_ENABLED=1`(数据接口要求 Bearer Token)、`IOT_CORS_ORIGINS`(允许跨域来源,默认本机 5173)、`IOT_DB_*`(MySQL 连接与开关)。

日志位于 `logs/`,可直接用于测试报告截图和问题定位。
