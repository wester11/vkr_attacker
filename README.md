# Attacker System

Распределённая система генерации атак для тестирования Defender.  
1 командер (atk-1) + до 6 воркеров (atk-2…atk-7).

---

## Архитектура

```
Ноутбук (браузер)
      │
      ▼
 atk-1 :5000/ui   ← Commander
      │  хранит список IP воркеров, толкает команды (push)
      ├──▶ atk-2 :5001   Worker
      ├──▶ atk-3 :5001   Worker
      ├──▶ atk-4 :5001   Worker
      └──▶ ...
```

Воркеры **не знают** про командера — просто слушают на порту 5001.  
Командер сам отправляет команды всем добавленным нодам одновременно.

---

## Установка

### atk-1 — Commander

```bash
DEFENDER_IP=192.168.10.5 bash install_attacker.sh
```

> Замени `192.168.10.5` на **внутренний** IP defender-VM.

После установки откроется:
- **Веб-интерфейс**: `http://<IP_ATK1>:5000/ui`
- **API**: `http://<IP_ATK1>:5000/status`

---

### atk-2 … atk-7 — Workers

На каждой ноде — одна команда, **никаких параметров**:

```bash
bash install_worker.sh
```

Воркер запустится и напишет свой IP в конце:

```
[✓] Worker готов
    📡 Этот воркер слушает на: 192.168.10.3:5001
```

---

### Параллельная установка воркеров с ноутбука

Если не хочется заходить на каждую ноду вручную:

```bash
for i in 2 3 4 5 6 7; do
  ssh ubuntu@atk-${i} "wget -qO install_worker.sh \
    https://raw.githubusercontent.com/wester11/vkr_attacker/main/install_worker.sh \
    && bash install_worker.sh" &
done
wait
echo "Все воркеры готовы"
```

---

## Подключение воркеров к командеру

1. Открой `http://<IP_ATK1>:5000/ui`
2. В поле **«Добавить воркера»** введи внутренний IP ноды (напр. `192.168.10.3`)
3. Нажми **«Добавить»** — нода появится в списке со статусом `online`
4. Повтори для каждого воркера

Или через curl:

```bash
curl -X POST http://<IP_ATK1>:5000/workers/add \
  -H 'Content-Type: application/json' \
  -d '{"ip": "192.168.10.3"}'
```

---

## Управление атаками

### Через веб-интерфейс

Открой `http://<IP_ATK1>:5000/ui` — там кнопки.

### Через curl

```bash
CMD="http://<IP_ATK1>:5000"

# Установить цель
curl -X POST $CMD/target \
  -H 'Content-Type: application/json' \
  -d '{"target": "192.168.10.5"}'

# Запустить атаку (на командер + все воркеры одновременно)
curl -X POST $CMD/attack/start \
  -H 'Content-Type: application/json' \
  -d '{"attack_type": "flood"}'

# Остановить
curl -X POST $CMD/attack/stop

# Статус
curl $CMD/status
```

---

## Типы атак

| Тип | Инструмент | Описание |
|-----|-----------|----------|
| `flood` | `ab` | HTTP-флуд — 500 000 запросов, 200 потоков |
| `ddos` | Python | DDoS с поддельными X-Forwarded-For (20 потоков, случайные IP) |
| `scan` | `nikto` | Сканирование уязвимостей |
| `brute` | Python | Брутфорс `/login` — 20 req/s со случайными кредами |
| `sqli` | Python | SQL-инъекции по 15 пейлоадам на 4 эндпоинтах |
| `slowloris` | Python | Удержание 200 соединений открытыми |
| `flash` | `wrk` | Легитимный трафик всплеском (4 потока, 50 конн.) |
| `slow` | `wrk` | Медленный флуд `/search` (2 потока, 20 конн.) |

---

## Структура файлов

```
attacker-system/
├── install_attacker.sh   # установка командера (atk-1)
├── install_worker.sh     # установка воркера  (atk-2...7)
├── commander/
│   └── main.py           # FastAPI сервер командера, порт 5000
├── worker/
│   └── agent.py          # FastAPI агент воркера, порт 5001
└── attacks/
    ├── brute.py          # брутфорс /login
    ├── sqli.py           # SQL-инъекции
    ├── ddos_spoof.py     # DDoS с поддельными IP
    └── slowloris.py      # slowloris
```

---

## API воркера (порт 5001)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/status` | статус воркера |
| POST | `/run` | запустить атаку `{"attack_type": "flood", "target": "..."}` |
| POST | `/stop` | остановить атаку |

## API командера (порт 5000)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/status` | общий статус + все воркеры |
| GET | `/workers` | список воркеров |
| POST | `/workers/add` | добавить воркера `{"ip": "..."}` |
| DELETE | `/workers/{ip}` | удалить воркера |
| POST | `/attack/start` | запустить `{"attack_type": "...", "target": "..."}` |
| POST | `/attack/stop` | остановить все |
| POST | `/target` | установить цель `{"target": "..."}` |
| GET | `/ui` | веб-интерфейс |

---

## Порядок запуска (кратко)

| Шаг | Где | Команда |
|-----|-----|---------|
| 1 | atk-1 | `DEFENDER_IP=... bash install_attacker.sh` |
| 2 | atk-2…7 | `bash install_worker.sh` |
| 3 | Браузер | открыть `http://atk-1:5000/ui` |
| 4 | UI | добавить IP каждого воркера |
| 5 | UI | жать кнопки атак |
