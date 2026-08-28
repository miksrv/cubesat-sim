"""Every MQTT topic string in the project.

Import ``TOPICS`` from here; never write a topic literal anywhere else. A typo
in a topic string produces silence, not an error, which is the most expensive
kind of bug this architecture can have.
"""

from __future__ import annotations

import json
import time
from typing import Any

TOPICS: dict[str, str] = {
    # Ground -> satellite. One topic for every command; the "command" field
    # selects the handler. COMMS re-publishes anything it receives over LoRa onto
    # this same topic, so nothing downstream needs to know which channel a
    # command arrived on.
    "command": "cubesat/command",
    # OBC -> HOSTD, and back. host/status reports the ACHIEVED profile
    # separately from the requested one.
    "host_command": "cubesat/host/command",
    "host_status": "cubesat/host/status",
    # Subsystem status
    "obc_status": "cubesat/obc/status",
    "eps_status": "cubesat/eps/status",
    "adcs_status": "cubesat/adcs/status",
    "payload_status": "cubesat/payload/status",
    "payload_data": "cubesat/payload/data",
    "payload_photo": "cubesat/payload/photo",
    "comms_status": "cubesat/comms/status",
    "dhs_status": "cubesat/dhs/status",
    "comms_data": "cubesat/comms/data",
    # Liveness. Every service publishes here on a fixed interval regardless of
    # its poll cadence; OBC declares a subsystem lost after a few misses. A
    # single shared topic means OBC subscribes once instead of eight times.
    "heartbeat": "cubesat/heartbeat",
}

#: Topics published with retain=True, so a service that starts late learns the
#: current situation immediately instead of waiting for the next publish.
RETAINED = frozenset(
    {
        TOPICS["host_status"],
        TOPICS["obc_status"],
        TOPICS["eps_status"],
        TOPICS["payload_status"],
        TOPICS["comms_status"],
        TOPICS["dhs_status"],
    }
)


def envelope(**fields: Any) -> str:
    """Serialise a payload, stamping it with the current wall-clock time.

    Every published payload carries ``timestamp`` as a Unix float. The DS1307
    RTC on the UPS HAT keeps this trustworthy with no network, which is what
    makes an offline mission's timestamps worth recording at all.
    """
    return json.dumps({"timestamp": time.time(), **fields})
