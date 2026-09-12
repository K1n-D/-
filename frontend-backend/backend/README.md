# Local backend

This Python demo backend consumes normalized MQTT data and exposes the monitoring APIs. It writes
telemetry, heartbeat and status records to the project-local MySQL instance when available, and
falls back to memory if the database is temporarily offline.

Install the optional database driver before starting the service:

```powershell
python -m pip install mysql-connector-python
```

The default database settings are `127.0.0.1:3306`, database `iot_monitor`, user `root`, and
password `root1234` for the local demonstration instance. Override them with `IOT_DB_HOST`,
`IOT_DB_PORT`, `IOT_DB_NAME`, `IOT_DB_USER`, `IOT_DB_PASSWORD`, or disable persistence with
`IOT_DB_ENABLED=0`.

