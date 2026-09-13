<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'

use([LineChart, GridComponent, TooltipComponent, DataZoomComponent, CanvasRenderer])

type Telemetry = { deviceCode: string; pointCode: string; value: number | boolean; unit?: string; timestamp: string; quality?: string }
type Device = { deviceCode: string; status?: string; lastHeartbeat?: string; lastDataTime?: string; points?: Record<string, Telemetry> }
type Alarm = { deviceCode: string; pointCode: string; level: string; value: number; status: string; time: number }
type Stats = { deviceTotal: number; online: number; offline: number; messages: number; alarms: number }
type ViewKey = 'dashboard' | 'devices' | 'history' | 'alarms' | 'config' | 'connection'

const api = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8080'
const token = ref(localStorage.getItem('iot-monitor-token') || '')
const username = ref(localStorage.getItem('iot-monitor-user') || 'admin')
const loginName = ref('admin'), loginPassword = ref('admin123'), loginBusy = ref(false), loginError = ref('')
const currentView = ref<ViewKey>('dashboard'), health = ref('连接中'), middleware = ref('local-mqtt')
const stats = ref<Stats>({ deviceTotal: 0, online: 0, offline: 0, messages: 0, alarms: 0 })
const devices = ref<Device[]>([]), telemetry = ref<Telemetry[]>([]), alarms = ref<Alarm[]>([]), error = ref(''), lastUpdated = ref('')
const pointRows = ref<any[]>([])
const ruleRows = ref<any[]>([])
const histDevice = ref(''), histPoint = ref(''), histBusy = ref(false), histRange = ref('')
const historyOption = ref<Record<string, unknown> | null>(null)
let stream: EventSource | undefined
let timer: number | undefined
const navItems: { key: ViewKey; label: string; icon: string }[] = [
  { key: 'dashboard', label: '运行总览', icon: '▦' }, { key: 'devices', label: '设备状态', icon: '◉' },
  { key: 'history', label: '历史数据', icon: '⌁' }, { key: 'alarms', label: '告警中心', icon: '!' },
  { key: 'config', label: '采集配置', icon: '⚙' }, { key: 'connection', label: '链路监控', icon: '⌘' }
]
const healthClass = computed(() => health.value === '正常' ? 'healthy' : health.value === '连接中' ? 'pending' : 'danger')
const pageTitle = computed(() => navItems.find(item => item.key === currentView.value)?.label || '运行总览')
const recentTelemetry = computed(() => telemetry.value.slice(0, 8))
const streamDevices = computed(() => [...new Set(telemetry.value.map(row => row.deviceCode))])
const streamPoints = computed(() => [...new Set(telemetry.value.filter(row => !histDevice.value || row.deviceCode === histDevice.value).map(row => row.pointCode))])
const sortedTelemetry = computed(() => telemetry.value.slice().sort((a, b) => +new Date(b.timestamp) - +new Date(a.timestamp)))
const focusSeries = computed(() => {
  const head = telemetry.value[0]
  if (!head) return null
  const points = telemetry.value
    .filter(row => row.deviceCode === head.deviceCode && row.pointCode === head.pointCode && typeof row.value === 'number')
    .sort((a, b) => +new Date(a.timestamp) - +new Date(b.timestamp))
    .slice(-40)
    .map(row => ({ time: formatTime(row.timestamp), value: Number(row.value) }))
  return { device: head.deviceCode, point: head.pointCode, unit: head.unit || '', points }
})
const AREA_GRADIENT = {
  type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
  colorStops: [{ offset: 0, color: 'rgba(63,157,136,.30)' }, { offset: 1, color: 'rgba(63,157,136,.02)' }]
}
const streamOption = computed(() => {
  if (!focusSeries.value) return null
  const series = focusSeries.value
  return {
    grid: { left: 52, right: 18, top: 14, bottom: 24 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v} ${series.unit}` },
    xAxis: { type: 'category', data: series.points.map(p => p.time), axisLine: { lineStyle: { color: '#d5e0e0' } }, axisLabel: { color: '#93a3a6', fontSize: 9, fontFamily: 'DM Mono, monospace' } },
    yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#eef3f3' } }, axisLabel: { color: '#93a3a6', fontSize: 9, fontFamily: 'DM Mono, monospace' } },
    series: [{
      type: 'line', data: series.points.map(p => p.value), smooth: true, showSymbol: false,
      lineStyle: { color: '#3f9d88', width: 2 }, areaStyle: AREA_GRADIENT
    }]
  }
})
function formatTime(value?: string | number) { return value ? new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '--' }
function formatDate(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '--' }
function statusLabel(status?: string) { return status === 'ONLINE' ? '在线' : status === 'SUSPECTED' ? '疑似离线' : status === 'OFFLINE' ? '离线' : '未知' }
function statusClass(status?: string) { return status === 'ONLINE' ? 'online' : status === 'SUSPECTED' ? 'suspected' : 'offline' }
function alarmLevel(level?: string) { return level === 'SERIOUS' ? '严重' : level === 'WARNING' ? '警告' : '提示' }
async function request(path: string, options?: RequestInit) { const headers = new Headers(options?.headers); if (token.value) headers.set('Authorization', `Bearer ${token.value}`); const response = await fetch(`${api}${path}`, { ...options, headers }); if (response.status === 401) { logout(); throw new Error('登录已过期') } if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json() }
async function login() { loginBusy.value = true; loginError.value = ''; try { const result = await request('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: loginName.value, password: loginPassword.value }) }); token.value = result.token; username.value = result.user?.username || loginName.value; localStorage.setItem('iot-monitor-token', token.value); localStorage.setItem('iot-monitor-user', username.value); await load() } catch { loginError.value = '账号或密码错误，请重试' } finally { loginBusy.value = false } }
function logout() { token.value = ''; localStorage.removeItem('iot-monitor-token'); localStorage.removeItem('iot-monitor-user') }
async function load() {
  if (!token.value) return
  try {
    const [h, s, d, t, a] = await Promise.all([
      request('/api/health'),
      request('/api/dashboard/statistics'),
      request('/api/devices'),
      request('/api/history'),
      request('/api/alarms'),
    ])
    health.value = h.status === 'UP' ? '正常' : '异常'
    middleware.value = h.broker || 'local-mqtt'
    stats.value = { ...stats.value, ...s }
    devices.value = Array.isArray(d) ? d : []
    telemetry.value = Array.isArray(t) ? t.slice(0, 120) : []
    alarms.value = Array.isArray(a) ? a.slice().reverse() : []
    lastUpdated.value = new Date().toLocaleTimeString('zh-CN')
    error.value = ''
  } catch (cause) {
    health.value = '异常'
    const message = cause instanceof Error ? cause.message : '未知错误'
    error.value = `无法连接后端服务（${api}），正在自动重试。${message}`
  }
}
async function ackAlarm(alarm: any) { try { await request('/api/alarms/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ deviceCode: alarm.deviceCode, pointCode: alarm.pointCode }) }); error.value = ''; await load() } catch (cause) { error.value = `确认失败（${cause instanceof Error ? cause.message : '未知错误'}）` } }
async function loadPoints() { try { pointRows.value = await request('/api/points') } catch { pointRows.value = [] } }
async function savePoint(row: any) {
  try {
    await request(`/api/points/${encodeURIComponent(row.deviceCode)}/${encodeURIComponent(row.pointCode)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scaleFactor: Number(row.scaleFactor), deadZone: Number(row.deadZone), collectIntervalMs: Number(row.collectIntervalMs), unit: row.unit, enabled: !!row.enabled, dataType: row.dataType, byteOrder: row.byteOrder })
    })
    error.value = ''
    await loadPoints()
  } catch (cause) { error.value = `保存失败（${cause instanceof Error ? cause.message : '未知错误'}）` }
}
function connectStream() {
  stream?.close()
  stream = new EventSource(`${api}/api/stream`)
  stream.onmessage = event => {
    try {
      const reading = JSON.parse(event.data)
      if (!reading.deviceCode) return
      telemetry.value = [reading, ...telemetry.value].slice(0, 300)
      const device = devices.value.find(item => item.deviceCode === reading.deviceCode)
      if (device) {
        device.points = { ...(device.points || {}), [reading.pointCode]: reading }
        device.lastDataTime = reading.timestamp
      }
    } catch { /* ignore malformed frames */ }
  }
  stream.onerror = () => { stream?.close(); setTimeout(connectStream, 5000) }
}
async function loadRules() { try { ruleRows.value = await request('/api/alarm-rules') } catch { ruleRows.value = [] } }
async function saveRule(row: any) {
  const payload: Record<string, unknown> = { threshold: Number(row.threshold), durationSec: Number(row.durationSec), hysteresis: Number(row.hysteresis), level: row.level, enabled: !!row.enabled, operator: row.operator }
  try {
    if (row.isNew) { await request('/api/alarm-rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ deviceCode: row.deviceCode || '*', pointCode: row.pointCode, ...payload }) }) }
    else { await request(`/api/alarm-rules/${row.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }) }
    error.value = ''; await loadRules()
  } catch (cause) { error.value = `规则保存失败（${cause instanceof Error ? cause.message : '未知错误'}）` }
}
async function deleteRule(row: any) { if (!row.id || !window.confirm(`删除规则 #${row.id}（${row.deviceCode}/${row.pointCode}）？`)) return; try { await request(`/api/alarm-rules/${row.id}`, { method: 'DELETE' }); error.value = ''; await loadRules() } catch (cause) { error.value = `删除失败（${cause instanceof Error ? cause.message : '未知错误'}）` } }
function addRuleRow() { ruleRows.value = [...ruleRows.value, { isNew: true, deviceCode: '*', pointCode: '', operator: '>', threshold: 90, durationSec: 10, hysteresis: 5, level: 'SERIOUS', enabled: true }] }
async function loadHistoryChart() {
  if (!histDevice.value || !histPoint.value) return
  const params = new URLSearchParams({ device: histDevice.value, point: histPoint.value, limit: '500' })
  if (histRange.value) {
    const minutes = Number(histRange.value)
    const to = new Date()
    const from = new Date(to.getTime() - minutes * 60000)
    params.set('from', from.toISOString().slice(0, 19) + 'Z')
    params.set('to', to.toISOString().slice(0, 19) + 'Z')
    params.set('interval', String(Math.max(1, Math.floor(minutes * 60 / 300))))
  }
  histBusy.value = true
  try {
    const rows: Telemetry[] = await request(`/api/history?${params.toString()}`)
    const ordered = rows.slice().reverse().filter(row => typeof row.value === 'number')
    const unit = rows[0]?.unit || ''
    historyOption.value = {
      grid: { left: 52, right: 20, top: 18, bottom: 42 },
      tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v} ${unit}` },
      xAxis: { type: 'category', data: ordered.map(row => formatTime(row.timestamp)), axisLine: { lineStyle: { color: '#d5e0e0' } }, axisLabel: { color: '#93a3a6', fontSize: 9, fontFamily: 'DM Mono, monospace' } },
      yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#eef3f3' } }, axisLabel: { color: '#93a3a6', fontSize: 9, fontFamily: 'DM Mono, monospace' } },
      dataZoom: [{ type: 'inside' }],
      series: [{
        type: 'line', data: ordered.map(row => Number(row.value)), smooth: true, showSymbol: false,
        lineStyle: { color: '#3f9d88', width: 2 }, areaStyle: AREA_GRADIENT
      }]
    }
    error.value = ''
  } catch { error.value = '历史曲线加载失败' } finally { histBusy.value = false }
}
watch([histDevice, histPoint], () => { histPoint.value = histPoint.value || '' })
watch(currentView, view => {
  if (view === 'config') loadPoints()
  if (view === 'alarms') loadRules()
})
onMounted(() => { if (token.value) { load(); connectStream() } ; timer = window.setInterval(load, 3000) }); onUnmounted(() => { if (timer) window.clearInterval(timer); stream?.close() })
</script>

<template>
  <div v-if="!token" class="login-page"><div class="login-visual"><div class="visual-grid"></div><div class="brand-mark"><span>◈</span><div><strong>ORBITAL</strong><small>INDUSTRIAL SYSTEMS</small></div></div><div class="visual-copy"><p class="kicker">INDUSTRIAL IOT CONTROL ROOM</p><h1>让每一次<br><em>设备心跳</em>都清晰可见</h1><p>实时采集、链路监控与异常响应，集中在一个可靠的本地工作台。</p></div><div class="visual-footer"><span><i></i> LOCAL EDGE NETWORK</span><span>FACTORY 001 / 2026</span></div></div><div class="login-panel"><div class="login-box"><div class="mobile-brand"><span>◈</span><strong>ORBITAL</strong></div><p class="kicker">SECURE ACCESS</p><h2>欢迎回来</h2><p class="login-subtitle">登录工业设备监控工作台</p><form @submit.prevent="login"><label>账号<input v-model="loginName" autocomplete="username" placeholder="请输入账号"></label><label>密码<input v-model="loginPassword" autocomplete="current-password" type="password" placeholder="请输入密码"></label><p v-if="loginError" class="form-error">{{ loginError }}</p><button class="primary-button" :disabled="loginBusy">{{ loginBusy ? '验证中…' : '进入监控台' }} <span>↗</span></button></form><div class="login-note"><span class="lock">◉</span><span>本地演示环境 · 数据仅保存在当前设备</span></div></div></div></div>
  <div v-else class="app-shell"><aside class="sidebar"><div class="side-brand"><span class="brand-symbol">◈</span><div><strong>ORBITAL</strong><small>INDUSTRIAL SYSTEMS</small></div></div><div class="site-selector"><span class="site-dot"></span><div><small>当前站点</small><strong>FACTORY 001</strong></div><span class="chevron">⌄</span></div><nav><p class="nav-label">工作台</p><button v-for="item in navItems" :key="item.key" :class="['nav-item', { active: currentView === item.key }]" @click="currentView = item.key"><span class="nav-icon">{{ item.icon }}</span>{{ item.label }}<b v-if="item.key === 'alarms' && stats.alarms">{{ stats.alarms }}</b></button></nav><div class="side-bottom"><div class="side-health"><span class="pulse"></span><div><small>系统状态</small><strong>全部服务正常</strong></div></div><button class="user-row" @click="logout"><span class="avatar">{{ username.slice(0, 1).toUpperCase() }}</span><span><strong>{{ username }}</strong><small>管理员账户</small></span><span class="logout-icon">↪</span></button></div></aside><main class="main-area"><header class="main-header"><div><p class="breadcrumb">FACTORY 001 <span>/</span> {{ pageTitle.toUpperCase() }}</p><h1>{{ pageTitle }}</h1></div><div class="header-actions"><span class="updated">最后同步 {{ lastUpdated || '--:--:--' }}</span><button class="icon-button" title="刷新数据" @click="load">↻</button><div class="connection-chip"><span :class="['status-dot', healthClass]"></span><span>后端 {{ health }}</span><span class="chip-divider"></span><span>MQTT {{ middleware }}</span></div></div></header><p v-if="error" class="error-banner"><span>!</span>{{ error }}</p>
  <template v-if="currentView === 'dashboard'"><section class="kpi-grid"><article class="kpi-card"><div class="kpi-top"><span>设备总数</span><span class="kpi-icon blue">◉</span></div><strong>{{ stats.deviceTotal }}</strong><small>已接入监控的设备</small></article><article class="kpi-card"><div class="kpi-top"><span>在线设备</span><span class="kpi-icon green">✓</span></div><strong class="green-text">{{ stats.online }}</strong><small>当前在线设备</small></article><article class="kpi-card"><div class="kpi-top"><span>标准化消息</span><span class="kpi-icon violet">⌁</span></div><strong>{{ stats.messages.toLocaleString() }}</strong><small>中间件累计处理</small></article><article class="kpi-card"><div class="kpi-top"><span>活动告警</span><span class="kpi-icon orange">!</span></div><strong class="orange-text">{{ stats.alarms || 0 }}</strong><small>{{ stats.alarms ? '需要及时关注' : '当前无待处理告警' }}</small></article></section><section class="dashboard-grid"><article class="surface chart-panel"><div class="surface-head"><div><p class="eyebrow">TELEMETRY STREAM</p><h2>实时数据流</h2></div><div class="legend"><span><i class="legend-line"></i>采集值</span><span class="range-pill">最近 1 小时⌄</span></div></div><div class="chart-meta"><strong>{{ focusSeries ? focusSeries.points[focusSeries.points.length - 1]?.value ?? '--' : '--' }}</strong><span>{{ focusSeries ? `${focusSeries.device} · ${focusSeries.point} · ${focusSeries.unit || 'value'}` : '等待数据' }}</span><b>LIVE</b></div><div class="stream-chart"><v-chart v-if="streamOption" :option="streamOption" autoresize /></div><div v-if="!streamOption" class="chart-empty">等待实时数据</div></article><article class="surface health-panel"><div class="surface-head"><div><p class="eyebrow">SYSTEM PULSE</p><h2>运行概况</h2></div><span class="health-badge"><i></i>运行中</span></div><div class="pulse-ring"><div><strong>{{ stats.online }}/{{ stats.deviceTotal || 0 }}</strong><small>设备在线</small></div></div><div class="health-list"><div><span>MQTT Broker</span><strong><i class="mini-dot"></i>已连接</strong></div><div><span>中间件心跳</span><strong><i class="mini-dot"></i>正常</strong></div><div><span>数据延迟</span><strong>&lt; 2 秒</strong></div></div></article></section><section class="dashboard-grid lower-grid"><article class="surface device-panel"><div class="surface-head"><div><p class="eyebrow">DEVICE FLEET</p><h2>设备状态</h2></div><button class="text-button" @click="currentView = 'devices'">查看全部 <span>→</span></button></div><div class="device-list"><div v-for="device in devices.slice(0, 4)" :key="device.deviceCode" class="device-row"><span :class="['device-indicator', statusClass(device.status)]"></span><div class="device-main"><strong>{{ device.deviceCode }}</strong><span>{{ Object.keys(device.points || {}).length }} 个测点 · 最近心跳 {{ formatTime(device.lastHeartbeat) }}</span></div><div class="device-reading"><strong>{{ device.points?.temperature?.value ?? device.points?.pressure?.value ?? '--' }}<small>{{ device.points?.temperature?.unit || device.points?.pressure?.unit || '' }}</small></strong><span :class="['status-label', statusClass(device.status)]">{{ statusLabel(device.status) }}</span></div></div><div v-if="!devices.length" class="empty-state">等待设备上报数据</div></div></article><article class="surface alarm-panel"><div class="surface-head"><div><p class="eyebrow">ATTENTION REQUIRED</p><h2>最近告警</h2></div><button class="text-button" @click="currentView = 'alarms'">告警中心 <span>→</span></button></div><div v-if="alarms.length" class="alarm-list"><div v-for="alarm in alarms.slice(0, 4)" :key="`${alarm.deviceCode}-${alarm.pointCode}-${alarm.time}`" class="alarm-row"><span :class="['alarm-severity', alarm.level === 'SERIOUS' ? 'serious' : 'warning']">!</span><div><strong>{{ alarm.deviceCode }} · {{ alarm.pointCode }}</strong><span>{{ alarm.value }} · {{ alarmLevel(alarm.level) }}</span></div><time>{{ formatTime(alarm.time * 1000) }}</time></div></div><div v-else class="clear-state"><span>✓</span><strong>一切正常</strong><small>当前没有需要处理的告警</small></div></article></section><section class="surface telemetry-panel"><div class="surface-head"><div><p class="eyebrow">LATEST READINGS</p><h2>最新采集</h2></div><button class="text-button" @click="currentView = 'history'">历史数据 <span>→</span></button></div><div class="table-scroll"><table><thead><tr><th>时间</th><th>设备</th><th>测点</th><th>采集值</th><th>质量</th></tr></thead><tbody><tr v-for="row in recentTelemetry" :key="`${row.deviceCode}-${row.pointCode}-${row.timestamp}`"><td>{{ formatTime(row.timestamp) }}</td><td class="mono">{{ row.deviceCode }}</td><td>{{ row.pointCode }}</td><td class="table-value">{{ row.value }} <small>{{ row.unit }}</small></td><td><span class="quality-tag">{{ row.quality || 'GOOD' }}</span></td></tr><tr v-if="!recentTelemetry.length"><td colspan="5" class="empty-state">等待标准化数据</td></tr></tbody></table></div></section></template>
  <template v-else-if="currentView === 'devices'"><section class="surface full-panel"><div class="surface-head"><div><p class="eyebrow">DEVICE FLEET</p><h2>全部设备</h2></div><span class="count-label">{{ devices.length }} 台设备</span></div><div class="table-scroll"><table class="wide-table"><thead><tr><th>设备编号</th><th>当前状态</th><th>测点数量</th><th>最近心跳</th><th>最近数据</th><th>当前读数</th></tr></thead><tbody><tr v-for="device in devices" :key="device.deviceCode"><td class="mono strong-cell">{{ device.deviceCode }}</td><td><span :class="['status-label', statusClass(device.status)]">{{ statusLabel(device.status) }}</span></td><td>{{ Object.keys(device.points || {}).length }} 个</td><td>{{ formatTime(device.lastHeartbeat) }}</td><td>{{ formatTime(device.lastDataTime) }}</td><td><span v-for="point in Object.keys(device.points || {}).slice(0, 2)" :key="point" class="reading-chip">{{ point }} {{ device.points?.[point]?.value }}{{ device.points?.[point]?.unit }}</span></td></tr><tr v-if="!devices.length"><td colspan="6" class="empty-state">暂无设备数据</td></tr></tbody></table></div></section></template>
  <template v-else-if="currentView === 'history'"><section class="surface full-panel"><div class="surface-head"><div><p class="eyebrow">NORMALIZED TELEMETRY</p><h2>历史数据</h2></div><span class="count-label">共 {{ sortedTelemetry.length }} 条</span></div><div class="chart-toolbar"><select class="cell-input" v-model="histDevice"><option value="">选择设备</option><option v-for="code in streamDevices" :key="code" :value="code">{{ code }}</option></select><select class="cell-input" v-model="histPoint"><option value="">选择测点</option><option v-for="point in streamPoints" :key="point" :value="point">{{ point }}</option></select><select class="cell-input" v-model="histRange"><option value="">最新记录</option><option value="15">最近 15 分钟</option><option value="60">最近 1 小时</option><option value="360">最近 6 小时</option><option value="1440">最近 24 小时</option></select><button class="text-button" :disabled="histBusy || !histDevice || !histPoint" @click="loadHistoryChart">{{ histBusy ? '加载中…' : '生成曲线' }} <span>→</span></button></div><v-chart v-if="historyOption" :option="historyOption" autoresize class="history-canvas" /><div v-if="!historyOption" class="chart-empty">选择设备与测点后生成曲线</div><div class="table-scroll"><table class="wide-table"><thead><tr><th>采集时间</th><th>设备</th><th>测点</th><th>数值</th><th>单位</th><th>数据质量</th></tr></thead><tbody><tr v-for="row in sortedTelemetry" :key="`${row.deviceCode}-${row.pointCode}-${row.timestamp}`"><td>{{ formatDate(row.timestamp) }}</td><td class="mono strong-cell">{{ row.deviceCode }}</td><td>{{ row.pointCode }}</td><td class="table-value">{{ row.value }}</td><td>{{ row.unit || '--' }}</td><td><span class="quality-tag">{{ row.quality || 'GOOD' }}</span></td></tr><tr v-if="!sortedTelemetry.length"><td colspan="6" class="empty-state">暂无历史数据</td></tr></tbody></table></div></section></template>
  <template v-else-if="currentView === 'alarms'"><section class="surface full-panel"><div class="surface-head"><div><p class="eyebrow">ALERT MANAGEMENT</p><h2>告警中心</h2></div><span :class="['count-label', { alert: stats.alarms }]">{{ stats.alarms ? `${stats.alarms} 条活动告警` : '系统运行平稳' }}</span></div><section class="surface rule-panel"><div class="surface-head"><div><p class="eyebrow">ALARM RULES</p><h2>告警规则</h2></div><button class="text-button" @click="addRuleRow">+ 新增规则</button></div><div class="table-scroll"><table class="wide-table"><thead><tr><th>设备(*=全部)</th><th>测点</th><th>条件</th><th>阈值</th><th>持续 (秒)</th><th>滞回</th><th>级别</th><th>启用</th><th>操作</th></tr></thead><tbody><tr v-for="(rule, index) in ruleRows" :key="rule.id ?? `new-${index}`"><td><input class="cell-input" :disabled="!rule.isNew" v-model="rule.deviceCode"></td><td><input class="cell-input" v-model="rule.pointCode"></td><td><select class="cell-input" v-model="rule.operator"><option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option></select></td><td><input class="cell-input" type="number" step="0.1" v-model="rule.threshold"></td><td><input class="cell-input" type="number" step="1" min="0" v-model="rule.durationSec"></td><td><input class="cell-input" type="number" step="0.5" min="0" v-model="rule.hysteresis"></td><td><select class="cell-input" v-model="rule.level"><option value="SERIOUS">严重</option><option value="WARNING">警告</option><option value="INFO">提示</option></select></td><td><input type="checkbox" v-model="rule.enabled"></td><td><button class="text-button" @click="saveRule(rule)">保存</button><button v-if="!rule.isNew" class="text-button danger" @click="deleteRule(rule)">删除</button></td></tr><tr v-if="!ruleRows.length"><td colspan="9" class="empty-state">暂无告警规则</td></tr></tbody></table></div></section><div v-if="alarms.length" class="alarm-table"><div v-for="alarm in alarms" :key="`${alarm.deviceCode}-${alarm.pointCode}-${alarm.time}`" class="alarm-card"><span :class="['alarm-severity', alarm.level === 'SERIOUS' ? 'serious' : 'warning']">!</span><div class="alarm-card-main"><strong>{{ alarm.deviceCode }} <span>/</span> {{ alarm.pointCode }}</strong><p>检测值 <b>{{ alarm.value }}</b> · {{ alarmLevel(alarm.level) }}阈值<span v-if="alarm.ack"> · 已确认</span></p></div><button v-if="alarm.status === 'ACTIVE' && !alarm.ack" class="text-button" @click="ackAlarm(alarm)">确认</button><span :class="['alarm-status', alarm.status === 'ACTIVE' ? 'active' : 'resolved']">{{ alarm.status === 'ACTIVE' ? '活动' : '已恢复' }}</span><time>{{ formatDate(new Date(alarm.time * 1000).toISOString()) }}</time></div></div><div v-else class="empty-large"><span>✓</span><h3>暂无告警记录</h3><p>所有设备均在设定范围内运行</p></div></section></template>
  <template v-else-if="currentView === 'config'"><section class="surface full-panel"><div class="surface-head"><div><p class="eyebrow">POINT TABLE</p><h2>采集配置</h2></div><span class="count-label">共 {{ pointRows.length }} 个测点 · 缩放 = 寄存器值 × 系数 · 修改后重启采集器生效</span></div><div class="table-scroll"><table class="wide-table"><thead><tr><th>设备编号</th><th>测点</th><th>寄存器</th><th>类型</th><th>数据类型</th><th>字节序</th><th>缩放系数</th><th>死区</th><th>周期 (ms)</th><th>单位</th><th>启用</th><th>操作</th></tr></thead><tbody><tr v-for="row in pointRows" :key="`${row.deviceCode}/${row.pointCode}`"><td class="mono strong-cell">{{ row.deviceCode }}</td><td>{{ row.pointCode }}</td><td class="mono">{{ row.register }}</td><td>{{ row.registerType }}</td><td><select class="cell-input" v-model="row.dataType"><option value="uint16">uint16</option><option value="int16">int16</option><option value="int32">int32</option><option value="float32">float32</option></select></td><td><select class="cell-input" v-model="row.byteOrder"><option>ABCD</option><option>CDAB</option><option>BADC</option><option>DCBA</option></select></td><td><input class="cell-input" type="number" step="0.001" v-model="row.scaleFactor"></td><td><input class="cell-input" type="number" step="0.001" min="0" v-model="row.deadZone"></td><td><input class="cell-input" type="number" step="100" min="200" v-model="row.collectIntervalMs"></td><td><input class="cell-input" v-model="row.unit"></td><td><input type="checkbox" v-model="row.enabled"></td><td><button class="text-button" @click="savePoint(row)">保存 <span>→</span></button></td></tr><tr v-if="!pointRows.length"><td colspan="12" class="empty-state">暂无点位配置（数据库不可用或无记录）</td></tr></tbody></table></div></section></template>
  <template v-else><section class="connection-grid"><article class="surface connection-card large"><div class="surface-head"><div><p class="eyebrow">SERVICE HEALTH</p><h2>链路状态</h2></div><span class="health-badge"><i></i>全部正常</span></div><div class="service-row"><span class="service-icon mqtt">⌁</span><div><strong>MQTT Broker</strong><small>127.0.0.1:1883</small></div><b>CONNECTED</b></div><div class="service-row"><span class="service-icon middleware">◈</span><div><strong>通信中间件</strong><small>心跳与重连监控 · 8090</small></div><b>CONNECTED</b></div><div class="service-row"><span class="service-icon backend">⌘</span><div><strong>业务后端</strong><small>数据持久化与 API · 8080</small></div><b>UP</b></div></article><article class="surface metrics-card"><div class="surface-head"><div><p class="eyebrow">MESSAGE PIPELINE</p><h2>消息管线</h2></div></div><div class="metric-line"><span>累计消息</span><strong>{{ stats.messages.toLocaleString() }}</strong></div><div class="metric-line"><span>当前设备</span><strong>{{ stats.deviceTotal }}</strong></div><div class="metric-line"><span>在线率</span><strong>{{ stats.deviceTotal ? Math.round(stats.online / stats.deviceTotal * 100) : 0 }}%</strong></div><div class="metric-line"><span>刷新间隔</span><strong>3s</strong></div></article></section></template><footer class="main-footer"><span>ORBITAL INDUSTRIAL SYSTEMS</span><span>LOCAL EDGE MONITOR · MQTT 3.1.1</span></footer></main></div>
</template>
