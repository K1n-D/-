# 虚拟设备控制程序

模拟设备程序现在同时提供 MQTT 数据发布和本地 HTTP 控制接口。启动全链路后，控制接口位于 `http://127.0.0.1:8091`，前端“虚拟设备”页面使用该接口完成设备管理。

虚拟设备有独立控制前端，地址为 `http://127.0.0.1:5174`；它和工业监控台 `5173` 分开运行，共用同一套设备控制 API。

## 启动

```powershell
$env:PYTHONPATH = "$PWD\middleware\src;$PWD\simulated-devices\src"
python -m sim_devices.main --all --scenario normal
```

`start-all.ps1` 会自动启动三个默认设备：`PLC-001`、`TEMP-001`、`PRESS-001`。

## 控制接口

```text
GET    /api/devices                 查看全部设备和运行状态
POST   /api/devices                 添加设备
PUT    /api/devices/{deviceCode}    修改名称、场景、采集周期和测点
POST   /api/devices/{deviceCode}/start  启动设备
POST   /api/devices/{deviceCode}/stop   停止设备
POST   /api/devices/{deviceCode}/toggle切换启停
DELETE /api/devices/{deviceCode}    删除设备
GET    /health                      控制服务健康检查
```

添加设备示例：

```json
{
  "deviceCode": "SIM-004",
  "name": "4号装配线",
  "deviceType": "TEMPERATURE",
  "scenario": "normal",
  "enabled": true,
  "intervalSec": 1,
  "heartbeatIntervalSec": 10,
  "points": {
    "temperature": {"base": 65, "variation": 3, "unit": "C"}
  }
}
```

设备停止时会发布 retained `OFFLINE` 状态；设备重新启动后会发布 `ONLINE` 状态并恢复业务心跳和测点数据，因此修改会同步反映到中间件、后端和工业监控页面。
