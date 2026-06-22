#!/usr/bin/env bash
# music-organiser — installer
#
# Usage:
#   bash install.sh          install / upgrade the service
#   bash install.sh --deps   also install Python packages via pip
#   bash install.sh --full   install system packages (apt) + Python packages + service
#
# Safe to re-run at any time. Idempotent.

set -euo pipefail

# ── parse args ────────────────────────────────────────────────────────────────
INSTALL_PY_DEPS=false
INSTALL_SYS_DEPS=false
for arg in "$@"; do
    case "${arg}" in
        --deps)  INSTALL_PY_DEPS=true ;;
        --full)  INSTALL_PY_DEPS=true; INSTALL_SYS_DEPS=true ;;
    esac
done

# ── colours ───────────────────────────────────────────────────────────────────
_green()  { printf '\033[0;32m  ✓  %s\033[0m\n' "$*"; }
_yellow() { printf '\033[0;33m  ⚠  %s\033[0m\n' "$*"; }
_red()    { printf '\033[0;31m\n  ✗  ERROR: %s\033[0m\n\n' "$*"; exit 1; }
_step()   { printf '\n\033[1;34m  ▶  %s\033[0m\n' "$*"; }
_info()   { printf '\033[2m     %s\033[0m\n' "$*"; }
_url()    { printf '\033[1;36m  →  %s\033[0m\n' "$*"; }

# ── paths ─────────────────────────────────────────────────────────────────────
PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="music-organiser"
SERVICE_DST="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
BIN_DST="${HOME}/.local/bin/${SERVICE_NAME}"
PORT=8082

# ── helpers ───────────────────────────────────────────────────────────────────
_lan_ip() {
    python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except Exception:
    print('localhost')
finally:
    s.close()
" 2>/dev/null || echo "localhost"
}

_has_cmd() { command -v "$1" &>/dev/null; }

_can_sudo() {
    # Check if user can sudo without prompting for a password
    sudo -n true 2>/dev/null
}

# ── banner ────────────────────────────────────────────────────────────────────
clear 2>/dev/null || true
echo ""
echo "  ╔═══════════════════════════════════════════════════╗"
echo "  ║          music-organiser — installer              ║"
echo "  ╚═══════════════════════════════════════════════════╝"
echo ""
echo "  This script will:"
echo "    1. Check your Python version"
echo "    2. Install the background service"
echo "    3. Create the 'music-organiser' control command"
echo "    4. Start the web UI on port ${PORT}"
echo ""

# ── 1. check OS / systemd ─────────────────────────────────────────────────────
_step "Checking system"

if ! _has_cmd systemctl; then
    _red "systemd not found. This installer requires a systemd-based Linux distro (Ubuntu, Debian, Fedora, etc.)"
fi

if ! systemctl --user show-environment &>/dev/null; then
    _red "Cannot connect to the user systemd session. Are you logged in as a regular user (not root)?"
fi
_green "systemd OK"

# ── 2. system packages (apt) ──────────────────────────────────────────────────
if [ "${INSTALL_SYS_DEPS}" = true ]; then
    _step "Installing system packages (requires sudo)"
    if ! _can_sudo; then
        _yellow "sudo not available without password — skipping apt installs"
        _info "If you need system packages, run: sudo apt install python3 python3-pip libchromaprint-tools"
    else
        sudo apt-get update -qq
        sudo apt-get install -y -qq python3 python3-pip libchromaprint-tools
        _green "system packages installed"
    fi
fi

# ── 3. Python version ─────────────────────────────────────────────────────────
_step "Checking Python"

if ! _has_cmd python3; then
    echo ""
    echo "  Python 3 is not installed."
    echo ""
    echo "  Fix it with:"
    echo "    sudo apt install python3 python3-pip"
    echo ""
    _red "Python 3 not found"
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJ=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MIN=$(python3 -c "import sys; print(sys.version_info.minor)")

if [ "${PY_MAJ}" -lt 3 ] || [ "${PY_MIN}" -lt 10 ]; then
    echo ""
    echo "  Python ${PY_VER} found, but 3.10 or newer is required."
    echo ""
    echo "  On Ubuntu 22.04+: sudo apt install python3.10"
    echo "  On Debian 12+:    sudo apt install python3"
    echo ""
    _red "Python 3.10+ required (found ${PY_VER})"
fi
_green "Python ${PY_VER}"

# ── 4. fpcalc (AcoustID fingerprinting) ───────────────────────────────────────
if ! _has_cmd fpcalc; then
    _yellow "fpcalc not found — AcoustID fingerprinting will be disabled"
    _info "Fix: sudo apt install libchromaprint-tools"
else
    _green "fpcalc found"
fi

# ── 5. Python packages ────────────────────────────────────────────────────────
if [ "${INSTALL_PY_DEPS}" = true ]; then
    _step "Installing Python packages"
    if ! _has_cmd pip3 && ! python3 -m pip --version &>/dev/null; then
        echo ""
        echo "  pip is not installed."
        echo "  Fix: sudo apt install python3-pip"
        echo ""
        _red "pip not found"
    fi
    python3 -m pip install --quiet --user -r "${PROJ_DIR}/requirements.txt"
    _green "Python packages installed"
else
    _info "skip pip (run with --deps or --full to install packages)"
fi

# ── 6. check critical imports ─────────────────────────────────────────────────
_step "Checking required Python packages"
MISSING_PKGS=""
for pkg in fastapi uvicorn mutagen; do
    if ! python3 -c "import ${pkg}" 2>/dev/null; then
        MISSING_PKGS="${MISSING_PKGS} ${pkg}"
    fi
done

if [ -n "${MISSING_PKGS}" ]; then
    echo ""
    echo "  Missing Python packages:${MISSING_PKGS}"
    echo ""
    echo "  Fix it by re-running:"
    echo "    bash install.sh --deps"
    echo ""
    _red "Missing required packages. Run: bash install.sh --deps"
fi
_green "all packages present"

# ── 7. validate web_ui.py ─────────────────────────────────────────────────────
_step "Validating web_ui.py"
if ! python3 -c "import ast; ast.parse(open('${PROJ_DIR}/web_ui.py').read())" 2>/dev/null; then
    _red "web_ui.py has a syntax error — the installation may be corrupted"
fi
_green "web_ui.py OK"

# ── 8. write systemd service ──────────────────────────────────────────────────
_step "Installing service"
mkdir -p "$(dirname "${SERVICE_DST}")"

cat > "${SERVICE_DST}" <<EOF
[Unit]
Description=music-organiser web UI
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=${PROJ_DIR}
ExecStart=/usr/bin/python3 ${PROJ_DIR}/web_ui.py
Restart=on-failure
RestartSec=3
KillMode=control-group
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=music-organiser
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

_green "service unit installed"

# ── 9. write control script ───────────────────────────────────────────────────
_step "Installing control script"
mkdir -p "${HOME}/.local/bin"

cat > "${BIN_DST}" <<'CTLEOF'
#!/usr/bin/env bash
# music-organiser — service control
# Usage: music-organiser [start|stop|restart|status|logs|logfile|open|url|health|enable|disable]

SERVICE="music-organiser"
PORT=8082

_ip() {
    python3 -c "
import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8',80)); print(s.getsockname()[0])
except: print('localhost')
finally: s.close()
" 2>/dev/null || echo "localhost"
}

_green()  { printf '\033[0;32m  ✓  %s\033[0m\n' "$*"; }
_yellow() { printf '\033[0;33m  ⚠  %s\033[0m\n' "$*"; }
_red()    { printf '\033[0;31m  ✗  %s\033[0m\n' "$*"; }
_dim()    { printf '\033[2m     %s\033[0m\n'    "$*"; }
_bold()   { printf '\033[1m%s\033[0m\n'         "$*"; }

URL="http://$(_ip):${PORT}"
CMD="${1:-status}"

_running() { systemctl --user is-active --quiet "${SERVICE}" 2>/dev/null; }
_health()  { curl -sf "http://localhost:${PORT}/api/health" 2>/dev/null || true; }

_wait_up() {
    printf '     waiting'
    for i in $(seq 1 20); do
        sleep 0.5
        if _running && curl -sf "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
            echo ""; return 0
        fi
        printf '.'
    done
    echo ""
    return 1
}

case "${CMD}" in
start)
    if _running; then
        _yellow "already running — ${URL}"
    else
        systemctl --user start "${SERVICE}"
        if _wait_up; then
            _green "started"
            echo "     ${URL}"
        else
            _red "service started but not responding — run: music-organiser logs"
            exit 1
        fi
    fi
    ;;
stop)
    if _running; then systemctl --user stop "${SERVICE}"; _yellow "stopped"
    else _dim "not running"; fi
    ;;
restart)
    systemctl --user reset-failed "${SERVICE}" 2>/dev/null || true
    systemctl --user restart "${SERVICE}"
    if _wait_up; then
        _green "restarted — ${URL}"
    else
        _red "not responding after restart — run: music-organiser logs"
        exit 1
    fi
    ;;
status)
    echo ""
    if _running; then
        _green "${SERVICE} — running"
        echo "     ${URL}"
        H=$(_health)
        if [ -n "${H}" ]; then
            UP=$(echo "${H}"   | python3 -c "import json,sys; u=json.load(sys.stdin).get('uptime_seconds',0); print(f'{u//3600:02d}h{(u%3600)//60:02d}m{u%60:02d}s')" 2>/dev/null || echo "?")
            LIB=$(echo "${H}"  | python3 -c "import json,sys; print(f\"{json.load(sys.stdin).get('library_files',0):,}\")" 2>/dev/null || echo "?")
            SESS=$(echo "${H}" | python3 -c "import json,sys; print(f\"{json.load(sys.stdin).get('session_files',0):,}\")" 2>/dev/null || echo "?")
            VER=$(echo "${H}"  | python3 -c "import json,sys; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null || echo "?")
            _dim "uptime ${UP}  |  library ${LIB} files  |  session ${SESS} files  |  v${VER}"
        fi
    else
        _red "${SERVICE} — stopped"
        _dim "run: music-organiser start"
    fi
    echo ""
    ;;
logs)
    echo "  following journal (Ctrl-C to exit)"
    journalctl --user -u "${SERVICE}" -f --output=cat
    ;;
logfile)
    F="${HOME}/.local/share/music-organiser/web_ui.log"
    [ -f "${F}" ] && tail -n 80 -f "${F}" || { _red "log file not found yet — try: music-organiser logs"; exit 1; }
    ;;
open)
    if command -v xdg-open &>/dev/null; then xdg-open "${URL}"
    elif command -v open &>/dev/null; then open "${URL}"
    else echo "${URL}"; fi
    ;;
url)    echo "${URL}" ;;
health)
    H=$(_health)
    [ -z "${H}" ] && { _red "no response from ${URL}/api/health"; exit 1; }
    echo "${H}" | python3 -m json.tool
    ;;
enable)  systemctl --user enable  "${SERVICE}"; _green "will auto-start on login" ;;
disable) systemctl --user disable "${SERVICE}"; _yellow "auto-start disabled" ;;
*)
    _bold "music-organiser"
    echo ""
    echo "  start     start the service"
    echo "  stop      stop the service"
    echo "  restart   restart (use after updates)"
    echo "  status    show running state and stats"
    echo "  logs      follow live log (Ctrl-C to exit)"
    echo "  logfile   follow log file"
    echo "  open      open in browser"
    echo "  url       print the URL"
    echo "  health    JSON health check"
    echo "  enable    auto-start on login"
    echo "  disable   remove auto-start"
    echo ""
    echo "  ${URL}"
    echo ""
    ;;
esac
CTLEOF

chmod +x "${BIN_DST}"
_green "control script installed → ${BIN_DST}"

# PATH check
if ! echo "${PATH}" | grep -q "${HOME}/.local/bin"; then
    _yellow "~/.local/bin is not in your PATH"
    _info "Add this line to your ~/.bashrc or ~/.zshrc:"
    _info "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    _info "Then reload your shell: source ~/.bashrc"
fi

# ── 10. free port if needed ───────────────────────────────────────────────────
if ss -tlnp 2>/dev/null | grep -q ":${PORT}"; then
    _yellow "port ${PORT} is in use — freeing it"
    fuser -k "${PORT}/tcp" 2>/dev/null || true
    sleep 1
fi

# ── 11. start service ─────────────────────────────────────────────────────────
_step "Starting service"
systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}" 2>/dev/null || true
systemctl --user stop   "${SERVICE_NAME}" 2>/dev/null || true
systemctl --user reset-failed "${SERVICE_NAME}" 2>/dev/null || true
sleep 0.5
systemctl --user start "${SERVICE_NAME}"

printf '     waiting for service'
STARTED=false
for i in $(seq 1 24); do
    sleep 0.5
    if systemctl --user is-active --quiet "${SERVICE_NAME}" && \
       curl -sf "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
        echo ""
        STARTED=true
        break
    fi
    printf '.'
done

if [ "${STARTED}" = false ]; then
    echo ""
    _yellow "service started but health check timed out"
    _info "Check what went wrong:"
    _info "  music-organiser logs"
    _info "  music-organiser status"
fi

# ── 12. done ──────────────────────────────────────────────────────────────────
LAN_IP=$(_lan_ip)
URL="http://${LAN_IP}:${PORT}"

echo ""
echo "  ╔═══════════════════════════════════════════════════╗"
if [ "${STARTED}" = true ]; then
echo "  ║               installation complete               ║"
else
echo "  ║      installation complete (service pending)      ║"
fi
echo "  ╠═══════════════════════════════════════════════════╣"
printf  "  ║   open browser  →  %-31s║\n" "${URL}"
echo "  ╠═══════════════════════════════════════════════════╣"
echo "  ║                                                   ║"
echo "  ║   music-organiser start    — start                ║"
echo "  ║   music-organiser stop     — stop                 ║"
echo "  ║   music-organiser status   — show stats           ║"
echo "  ║   music-organiser logs     — live log             ║"
echo "  ║                                                   ║"
echo "  ╚═══════════════════════════════════════════════════╝"
echo ""
