#!/usr/bin/env bash
# Installs the systemd service+timer that run scan+analyze on a recurring
# schedule. Written to run as root, matching the deploy target - adjust
# paths in deploy/*.service and deploy/*.timer first if installing under a
# different user or a different repo location than /root/tronik.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "This must be run as root (it writes to /etc/systemd/system/)." >&2
    exit 1
fi

if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
    echo "No .venv found at $REPO_DIR/.venv - set up the virtualenv first:" >&2
    echo "  python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt" >&2
    exit 1
fi

chmod +x "$REPO_DIR/scripts/run_scan_and_analyze.sh"

cp "$REPO_DIR/deploy/polymarket-companion.service" /etc/systemd/system/polymarket-companion.service
cp "$REPO_DIR/deploy/polymarket-companion.timer" /etc/systemd/system/polymarket-companion.timer

systemctl daemon-reload
systemctl enable --now polymarket-companion.timer

echo "Installed. Check status with:"
echo "  systemctl list-timers polymarket-companion.timer"
echo "  journalctl -u polymarket-companion.service -f"
