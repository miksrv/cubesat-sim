#!/usr/bin/env bash

set -euo pipefail

SERVICES=(
    "cubesat-adcs.service"
    "cubesat-obc.service"
    "cubesat-payload.service"
    "cubesat-telemetry.service"
)

# Start and enable autostart for all CubeSat services
echo "Starting and enabling autostart for all CubeSat services..."
echo ""

for svc in "${SERVICES[@]}"; do
    echo "→ $svc"

    # Enable autostart on system boot
    sudo systemctl enable "$svc" 2>/dev/null || true

    # Start service (if not already running)
    if ! sudo systemctl is-active --quiet "$svc"; then
        sudo systemctl start "$svc"
        echo "  started"
    else
        echo "  already running"
    fi

    # Show short status
    sudo systemctl --no-pager status "$svc" --lines=3
    echo ""
done

# All services processed
echo "All services processed."
echo ""
echo "To check logs in real time, use the command:"
echo "  journalctl -u cubesat-obc.service -f"
echo "  (replace the service name as needed)"
echo ""
echo "All services should start automatically after reboot."
