#!/usr/bin/env bash

set -euo pipefail

SERVICES=(
    "cubesat-adcs.service"
    "cubesat-obc.service"
    "cubesat-eps.service"
    "cubesat-payload.service"
    "cubesat-telemetry.service"
)

# Restart all CubeSat services (e.g. after a system update)
echo "Restarting all CubeSat services..."
echo ""

for svc in "${SERVICES[@]}"; do
    echo "→ $svc"
    sudo systemctl restart "$svc"
    echo "  restarted"

    # Show short status
    sudo systemctl --no-pager status "$svc" --lines=3
    echo ""
done

echo "All services restarted."
echo ""
echo "To check logs in real time, use the command:"
echo "  journalctl -u cubesat-obc.service -f"
echo "  (replace the service name as needed)"
