#!/usr/bin/env bash
# Install CubeSat Sim on a Raspberry Pi.
#
# Enables only the always-on tier. Everything else is started and stopped by
# HOSTD as a profile is applied — a profile is the unit of operation, not a
# service. Runtime directories are created by systemd from the units'
# StateDirectory/RuntimeDirectory/LogsDirectory, so this script never mkdirs one.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PROJECT_DIR}/venv"
ALWAYS_ON=(cubesat-hostd.service cubesat@obc.service cubesat@eps.service)

echo "==> Python environment"
python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
# The rpi extra carries the hardware-only packages; they are expected to fail on
# anything that is not a Raspberry Pi.
"${VENV}/bin/pip" install --quiet -e "${PROJECT_DIR}[rpi]"

echo "==> mosquitto listeners"
# Two listeners: localhost TCP for the satellite's own services, WebSockets for
# browsers, with an ACL on the second alone. The dashboard's page talks to the
# broker directly, so there is no MQTT-to-WebSocket bridge in this project —
# see config/mosquitto/cubesat.conf for why the split is per-listener.
sudo cp "${PROJECT_DIR}/config/mosquitto/cubesat.conf" /etc/mosquitto/conf.d/cubesat.conf
sudo cp "${PROJECT_DIR}/config/mosquitto/acl.conf" /etc/mosquitto/conf.d/cubesat-acl.conf
# Restarted rather than reloaded: mosquitto does not pick up a new listener on
# SIGHUP. Fails loudly on a bad config file, which is the wanted behaviour — a
# broker that did not come back is the one failure that strands everything.
sudo systemctl restart mosquitto

echo "==> systemd units"
sudo cp "${PROJECT_DIR}"/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload

echo "==> Enabling the always-on tier"
for unit in "${ALWAYS_ON[@]}"; do
    sudo systemctl enable --now "${unit}"
done

echo
echo "Installed. The satellite is in the default profile (HOSTED)."
echo
# The `cubesat` CLI is not written yet, so this says what actually works today.
# Advertising a command that does not exist is the worst possible first
# impression of a system whose whole point is not claiming things it cannot do.
echo "Switch profiles (the CLI is not written yet — see ROADMAP P2):"
echo "  mosquitto_pub -h localhost -t cubesat/command \\"
echo "    -m '{\"command\":\"set_profile\",\"params\":{\"profile\":\"DEMO\"}}'"
echo
echo "Watch what HOSTD did with it:"
echo "  journalctl -u cubesat-hostd -f"
echo
echo "Read the achieved profile:"
echo "  mosquitto_sub -h localhost -t cubesat/host/status -C 1 | python3 -m json.tool"
echo
echo "The interface is deployed separately, from the cubesat-groundstation"
echo "checkout on a machine that can build it:"
echo "  ./scripts/deploy-dashboard.sh ../cubesat-groundstation ${USER}@\$(hostname)"
