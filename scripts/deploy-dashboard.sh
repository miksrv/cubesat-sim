#!/usr/bin/env bash
# Deploy the interface onto the satellite.
#
# The dashboard's UI lives in a separate repository, cubesat-groundstation, and
# arrives here as a **built artefact** rather than as source: building React on
# a Pi 4 costs minutes and a node_modules tree on the SD card to produce a few
# megabytes of static files. So it is built on a laptop and only `dist/` travels.
#
# Run this from the machine that has the groundstation checkout, not on the Pi.
#
#   ./scripts/deploy-dashboard.sh ../cubesat-groundstation mik@cubesat
#
# PUBLIC_SOURCE=live is what makes the bundle talk to this satellite: the source
# is swapped by a bundler alias, and the other half of that swap is the recorded
# mission the public demo replays.
set -euo pipefail

GROUNDSTATION="${1:?usage: $0 <path-to-cubesat-groundstation> [user@host]}"
TARGET="${2:-mik@cubesat}"

# Must match CUBESAT_DASHBOARD_ROOT in src/cubesat/common/config.py, which
# defaults to CUBESAT_DATA_DIR/dashboard. systemd-tmpfiles creates it at boot
# from config/tmpfiles.d/cubesat.conf — deliberately not from a unit's
# StateDirectory, which would re-chown the tree to whichever unit restarted
# last; this script only fills it.
REMOTE_ROOT="/var/lib/cubesat/dashboard"

CLIENT="${GROUNDSTATION%/}/client"
[ -d "${CLIENT}" ] || { echo "no client/ under ${GROUNDSTATION}" >&2; exit 1; }

echo "==> Building the interface (PUBLIC_SOURCE=live)"
( cd "${CLIENT}" && PUBLIC_SOURCE=live yarn build )

echo "==> Copying to ${TARGET}:${REMOTE_ROOT}"
# --delete so a rebuild does not leave the previous build's content-hashed
# assets behind for ever; index.html is what points at the new hashes, and the
# old files are unreachable the moment it is replaced.
#
# --no-perms and --omit-dir-times, because the tree is not the operator's: the
# directories are cubesat:cubesat with the setgid bit (tmpfiles.d), and the
# operator writes into them only through the group. -a alone would try to stamp
# dist/'s 755 onto the root (dropping the setgid bit that makes the next deploy
# possible) and to touch directory mtimes it does not own, and rsync then exits
# 23 with every file delivered — a failure that is not one (2026-09-01).
rsync -rl --no-perms --omit-dir-times --delete "${CLIENT}/dist/" "${TARGET}:${REMOTE_ROOT}/"

echo
echo "Deployed. The service picks it up with no restart — it reads from disk per"
echo "request. It runs in profiles whose 'dashboard' is true: DEMO, EXPO, DIAG."
