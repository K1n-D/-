# 通信中间件

`python -m iot_middleware.broker` 启动本地 MQTT-like Broker；`python -m iot_middleware.main` 启动中间件。实现了主题匹配、协议层 PINGREQ/PINGRESP、业务心跳状态机、遗嘱消息、QoS1 语义下的标准消息转发和指数退避重连。
