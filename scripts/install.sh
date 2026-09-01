#!/usr/bin/env bash
# Install CubeSat Sim on a Raspberry Pi.
#
# Enables only the always-on tier. Everything else is started and stopped by
# HOSTD as a profile is applied — a profile is the unit of operation, not a
# service. Runtime directories are created by systemd-tmpfiles from
# config/tmpfiles.d/cubesat.conf, so this script never mkdirs one either.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PROJECT_DIR}/venv"
ALWAYS_ON=(cubesat-hostd.service cubesat@obc.service cubesat@eps.service)
# Must match the units' User= and the ownership in config/tmpfiles.d/cubesat.conf.
SERVICE_USER=cubesat

echo "==> apt prerequisites"
# libcamera's Python bindings exist only as apt packages, and on kernels ≥ 6.6
# the RPi.GPIO API is provided by the rpi-lgpio shim — neither is installable
# from PyPI, which is why the venv below shares the system site-packages.
sudo apt-get install -y python3-picamera2 python3-rpi-lgpio

echo "==> service account"
# The flight software runs as its own system account rather than as a person:
# a human login carries a home directory, a mail spool and whatever else the
# operator accumulates, none of which the satellite should be able to read.
# --system: no password ageing, no login shell, an id below the human range.
# The home directory is the state directory it already owns.
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    sudo useradd --system --home-dir /var/lib/cubesat --no-create-home \
        --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
# The hardware groups are granted per-unit through SupplementaryGroups, so they
# are not repeated here — the unit file stays the single readable statement of
# what the services may reach.
#
# The installing operator joins the service group instead, which is what makes
# scripts/deploy-dashboard.sh able to write the interface build into
# /var/lib/cubesat/dashboard without sudo. It takes a fresh login to take effect.
sudo usermod -aG "${SERVICE_USER}" "${USER}"

echo "==> runtime directories"
# NOT StateDirectory/LogsDirectory/RuntimeDirectory: those re-apply the starting
# unit's own User to the whole tree, and cubesat-hostd runs as root over the same
# three paths — so ownership followed whichever unit restarted last, and the
# unprivileged services lost the database, the photo tree, their logs and the I2C
# lock. The conf file carries the full account.
sudo cp "${PROJECT_DIR}/config/tmpfiles.d/cubesat.conf" /etc/tmpfiles.d/cubesat.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/cubesat.conf

echo "==> project ownership"
# Group-owned by the service account so the services can read the checkout and
# execute the venv, while the operator keeps write access for `git pull`.
sudo chown -R "${USER}:${SERVICE_USER}" "${PROJECT_DIR}"
sudo chmod -R g+rX "${PROJECT_DIR}"

echo "==> Python environment"
# --system-site-packages: picamera2, libcamera and the RPi.GPIO shim come from
# apt (above); everything pip-installable still lands in the venv.
python3 -m venv --system-site-packages "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
# The rpi extra carries the hardware-only packages; they are expected to fail on
# anything that is not a Raspberry Pi.
"${VENV}/bin/pip" install --quiet -e "${PROJECT_DIR}[rpi]"
# The venv is built after the chown above, so its new files need the same
# treatment — the services execute python from here.
sudo chgrp -R "${SERVICE_USER}" "${VENV}"
sudo chmod -R g+rX "${VENV}"

echo "==> mosquitto listeners"
# Two listeners: localhost TCP for the satellite's own services, WebSockets for
# browsers, with an ACL on the second alone. The dashboard's page talks to the
# broker directly, so there is no MQTT-to-WebSocket bridge in this project —
# see config/mosquitto/cubesat.conf for why the split is per-listener.
sudo cp "${PROJECT_DIR}/config/mosquitto/cubesat.conf" /etc/mosquitto/conf.d/cubesat.conf
# The ACL file deliberately does NOT go into conf.d/: mosquitto parses every
# file there as broker configuration, and an ACL file read that way kills the
# broker at startup ("Invalid bridge configuration" on the first topic line).
sudo cp "${PROJECT_DIR}/config/mosquitto/acl.conf" /etc/mosquitto/cubesat-acl.conf
sudo rm -f /etc/mosquitto/conf.d/cubesat-acl.conf
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
echo
echo "You were added to the '${SERVICE_USER}' group — log out and back in before"
echo "running that, or the rsync into /var/lib/cubesat/dashboard is denied."
