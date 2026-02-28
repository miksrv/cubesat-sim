#!/usr/bin/env bash

set -euo pipefail

SERVICES=(
    "cubesat-adcs.service"
    "cubesat-obc.service"
    "cubesat-eps.service"
    "cubesat-payload.service"
    "cubesat-telemetry.service"
)

# Stop and disable autostart for all CubeSat services
echo "Stopping and disabling autostart for all CubeSat services..."
echo ""

for svc in "${SERVICES[@]}"; do
    echo "→ $svc"

    # Stop service if it is running
    if sudo systemctl is-active --quiet "$svc"; then
        sudo systemctl stop "$svc"
        echo "  stopped"
    else
        echo "  already stopped"
    fi

    # Disable autostart
    sudo systemctl disable "$svc" 2>/dev/null || true
    echo "  autostart disabled"

    # Show status
    sudo systemctl --no-pager status "$svc" --lines=3 || true
    echo ""
done

# All services stopped and disabled
echo "All services stopped and autostart disabled."
echo ""
echo "To completely remove services from the system (if needed):"
echo "  sudo systemctl disable --now <service>"
echo "  sudo rm /etc/systemd/system/<service>"
echo "  sudo systemctl daemon-reload"