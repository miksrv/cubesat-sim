"""The ``cubesat`` command.

A thin MQTT client: it publishes ``set_profile`` and waits for the matching
``cubesat/host/status``. It needs no privileges, only a reachable broker — the
same path the Telegram bot, the dashboard and a LoRa uplink all use.

Not implemented yet. HOSTD can apply profiles now, so this is the next thing
worth writing; until then the actions are reachable over MQTT directly.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    del argv
    sys.stderr.write(
        "cubesat: the CLI is not implemented yet. Until it lands, publish the "
        "action directly:\n"
        "  mosquitto_pub -t cubesat/command -m "
        "'{\"command\":\"set_profile\",\"params\":{\"profile\":\"DEMO\"}}'\n"
    )
    return 1
