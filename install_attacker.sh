#!/bin/bash
# ============================================================
# COMMANDER — установка главной атакующей ноды (atk-1)
#
# Использование:
#   DEFENDER_IP=192.168.10.5 bash install_attacker.sh
#
# После установки:
#   Открой http://<IP_ATK1>:5000/ui
#   Добавь IP воркеров через веб-интерфейс
# ============================================================

set -e
CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${CYAN}[commander]${NC} $1"; }
ok()    { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }

DEFENDER_IP=${DEFENDER_IP:-""}

if [ "$(id -u)" -eq 0 ]; then
    SERVICE_USER="root"
    BASE_HOME="/root"
else
    SERVICE_USER="$(id -un)"
    BASE_HOME="$HOME"
fi

INSTALL_DIR="${BASE_HOME}/attacker"
REPO="https://raw.githubusercontent.com/wester11/vkr_attacker/main"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     COMMANDER — УСТАНОВКА                ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── Системные зависимости ─────────────────────────────────────────────────────
info "Устанавливаю зависимости..."
sudo apt-get update -q
sudo apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-full \
    git curl wget apache2-utils nikto -q

# wrk
if ! command -v wrk &>/dev/null; then
    sudo apt-get install -y wrk -q 2>/dev/null || {
        info "Собираю wrk из исходников..."
        git clone --quiet https://github.com/wg/wrk.git /tmp/wrk_b
        cd /tmp/wrk_b && make -j2 -s && sudo cp wrk /usr/local/bin/ && cd -
    }
fi
ok "Системные пакеты установлены"

# ── Python окружение ──────────────────────────────────────────────────────────
VENV="${INSTALL_DIR}/.venv"
mkdir -p "$INSTALL_DIR/attacks"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install fastapi uvicorn requests --quiet
ok "Python окружение готово"

# ── Скачать файлы ─────────────────────────────────────────────────────────────
info "Скачиваю commander и скрипты атак..."
wget -q "${REPO}/commander/main.py"      -O "${INSTALL_DIR}/commander.py"
wget -q "${REPO}/attacks/brute.py"       -O "${INSTALL_DIR}/attacks/brute.py"
wget -q "${REPO}/attacks/sqli.py"        -O "${INSTALL_DIR}/attacks/sqli.py"
wget -q "${REPO}/attacks/ddos_spoof.py"  -O "${INSTALL_DIR}/attacks/ddos_spoof.py"
wget -q "${REPO}/attacks/slowloris.py"   -O "${INSTALL_DIR}/attacks/slowloris.py"
ok "Файлы загружены"

# ── Systemd сервис ────────────────────────────────────────────────────────────
cat > /etc/systemd/system/commander.service << SVCEOF
[Unit]
Description=Attack Commander
After=network.target

[Service]
ExecStart=${VENV}/bin/python ${INSTALL_DIR}/commander.py
WorkingDirectory=${INSTALL_DIR}
Environment=TARGET_IP=${DEFENDER_IP}
Environment=CMD_PORT=5000
Environment=WORKER_PORT=5001
Environment=PYTHONPATH=${INSTALL_DIR}
Restart=always
User=${SERVICE_USER}

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable commander --quiet
sudo systemctl restart commander
ok "Commander сервис запущен"

# ── Итог ──────────────────────────────────────────────────────────────────────
PUBLIC_IP=$(curl -s --max-time 4 ifconfig.me 2>/dev/null || curl -s --max-time 4 api.ipify.org 2>/dev/null)
MY_IP=${PUBLIC_IP:-$(hostname -I | awk '{print $1}')}
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              COMMANDER ГОТОВ                         ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  🎮 Веб-интерфейс:  ${CYAN}http://${MY_IP}:5000/ui${NC}"
echo -e "  📡 API статус:     ${CYAN}http://${MY_IP}:5000/status${NC}"
echo ""
echo -e "  Следующий шаг:"
echo -e "  1. Запусти ${CYAN}install_worker.sh${NC} на каждой из нод atk-2...atk-7"
echo -e "  2. Открой ${CYAN}http://${MY_IP}:5000/ui${NC}"
echo -e "  3. Добавь IP воркеров через форму в интерфейсе"
echo -e "  4. Жми кнопки атак — командер автоматически рассылает команды всем"
echo ""
