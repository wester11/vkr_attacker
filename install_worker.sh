#!/bin/bash
# ============================================================
# WORKER — установка атакующего воркера (atk-2 ... atk-7)
#
# Использование (никаких параметров не нужно):
#   bash install_worker.sh
#
# После установки:
#   Воркер слушает на порту 5001.
#   Открой UI командера и добавь IP этой ноды туда.
# ============================================================

set -e
CYAN='\033[0;36m'; GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${CYAN}[worker]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }

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
echo -e "${CYAN}║     WORKER — УСТАНОВКА                   ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── Системные зависимости ─────────────────────────────────────────────────────
info "Устанавливаю зависимости..."
sudo apt-get update -q
sudo apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-full \
    git curl wget apache2-utils nikto -q

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
info "Скачиваю worker и скрипты атак..."
wget -q "${REPO}/worker/agent.py"        -O "${INSTALL_DIR}/agent.py"
wget -q "${REPO}/attacks/brute.py"       -O "${INSTALL_DIR}/attacks/brute.py"
wget -q "${REPO}/attacks/sqli.py"        -O "${INSTALL_DIR}/attacks/sqli.py"
wget -q "${REPO}/attacks/ddos_spoof.py"  -O "${INSTALL_DIR}/attacks/ddos_spoof.py"
wget -q "${REPO}/attacks/slowloris.py"   -O "${INSTALL_DIR}/attacks/slowloris.py"
ok "Файлы загружены"

# ── Systemd сервис ────────────────────────────────────────────────────────────
cat > /etc/systemd/system/attacker-worker.service << SVCEOF
[Unit]
Description=Attack Worker Agent
After=network.target

[Service]
ExecStart=${VENV}/bin/python ${INSTALL_DIR}/agent.py
WorkingDirectory=${INSTALL_DIR}
Environment=WORKER_PORT=5001
Environment=PYTHONPATH=${INSTALL_DIR}
Restart=always
User=${SERVICE_USER}

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable attacker-worker --quiet
sudo systemctl restart attacker-worker
ok "Worker сервис запущен"

# ── Итог ──────────────────────────────────────────────────────────────────────
MY_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              WORKER ГОТОВ                            ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  📡 Этот воркер слушает на: ${CYAN}${MY_IP}:5001${NC}"
echo ""
echo -e "  Следующий шаг:"
echo -e "  Открой UI командера → поле «Добавить воркера» → введи: ${CYAN}${MY_IP}${NC}"
echo ""
