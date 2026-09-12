# 基于通信中间件的工业设备数据采集与监控系统

这是一个面向 Windows 本地运行的全链路演示项目，包含三个核心目录：

- `frontend-backend`：本地 Python 业务 API 和静态 Web 监控页面；接口边界与规划中的 Spring Boot/Vue 版本一致。
- `middleware`：本地 MQTT 主题总线、协议层 PINGREQ/PINGRESP、业务心跳监测、标准化和指数退避重连。
- `simulated-devices`：PLC/传感器数据、业务心跳、遗嘱消息和异常场景模拟。

模拟设备还提供独立控制 API（`127.0.0.1:8091`），可在 Web 监控台的“虚拟设备”页面中动态添加、编辑、启停和删除设备；变更会经 MQTT 同步到中间件和设备状态页面。

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

## 核心参数

- MQTT Keep Alive：30 秒，客户端自动发送 PINGREQ，Broker 返回 PINGRESP。
- 业务心跳：10 秒一次；20 秒进入 SUSPECTED，30 秒进入 OFFLINE。
- 重连退避：1、2、4、8、16、32、60 秒，并加入 0.8~1.2 随机抖动。
- 数据 QoS 语义：至少一次投递；`eventId` 用于业务去重。
- 遗嘱消息：设备非正常断开时发布 retained OFFLINE 状态。

日志位于 `logs/`，可直接用于测试报告截图和问题定位。
