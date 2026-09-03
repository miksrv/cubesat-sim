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
    # selects the handler. COMMS re-publishes an uplink onto this same topic, so
    # nothing downstream needs to know which *link* a command arrived on. Which
    # mesh channel it must have arrived on is COMMS' own rule, applied before
    # any of this — see comms/service.py -> _refuse_uplink.
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
    # Retained since 2026-09-01, and it is what "the dashboard shows the last
    # photograph" rests on: DEMO and EXPO keep no photo history at all — the
    # frame is written to a tmpfs, published here and deleted — so the broker's
    # retained copy is the only place a page opened later can find it. mosquitto
    # runs with `persistence false`, so that copy is RAM and never the card.
    #
    # Two consequences worth holding on to. The message is large for a retained
    # topic (a 1920x1080 JPEG, base64: half a megabyte or so), which is fine in
    # RAM and would not be on a card. And a retained frame outlives the session
    # it was taken in, so PAYLOAD clears it when the mission changes rather than
    # letting a visitor meet the last demo's photograph as if it were current.
    "payload_photo": "cubesat/payload/photo",
    "comms_status": "cubesat/comms/status",
    "dhs_status": "cubesat/dhs/status",
    # The wide telemetry row, exactly as it would be written to the database —
    # every column of dhs.schema.TELEMETRY_COLUMNS, assembled by DHS from the
    # cached subsystem payloads on its own tick.
    #
    # It is on the bus rather than only in SQLite for two reasons. It is the sole
    # carrier of the host's own CPU, RAM, swap, disk, uptime and SoC temperature:
    # those are collected by psutil inside DHS and appear in no other message, so
    # before this topic existed a dashboard could only learn them by polling the
    # archive — which meant a profile that does not record could not show them at
    # all. And since 2026-09-01 DEMO and EXPO deliberately do not record (Q7),
    # so this is where the charts' history comes from: DASHBOARD keeps a bounded
    # in-memory ring of these messages and serves /api/telemetry out of it.
    #
    # Retained, so a browser opening mid-session gets the current row at once
    # instead of waiting a whole DHS tick — 30 s in NOMINAL — for the host
    # metrics to appear. mission_id is null when no mission is open, which is
    # the normal case in DEMO and EXPO.
    "dhs_telemetry": "cubesat/dhs/telemetry",
    "comms_data": "cubesat/comms/data",
    # One event per radio transaction — a received message, or a transmission
    # attempt with whether it left. Not retained: this is a log line, not a
    # state, and a browser replaying a stale "last packet" would show traffic
    # that is not happening. DHS records these into radio_log; COMMS still
    # persists nothing.
    "comms_radio": "cubesat/comms/radio",
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
        TOPICS["payload_photo"],
        TOPICS["comms_status"],
        TOPICS["dhs_status"],
        TOPICS["dhs_telemetry"],
    }
)


# ── what cubesat/payload/photo carries ──────────────────────────────────────
#
# The values on that topic's own fields, here rather than in a service because
# two services now speak them: PAYLOAD writes them, and since 2026-09-03 COMMS
# reads them to answer ``!photo`` over the radio with the frame's size and
# number. This file is already the readable definition of the MQTT contract, and
# a shared vocabulary living inside one of the two services would make the other
# import it — which is how a bus architecture quietly grows a dependency graph.

#: ``kind``: a photograph somebody asked for, against one a mission took of
#: itself on its 300 s cadence. One topic, two payload shapes — see the topic's
#: own comment above for why only one of them carries the image. The distinction
#: is load-bearing for the radio ack too: an ack window is 10 s wide against a
#: 300 s cadence, so roughly one ``!photo`` in thirty would otherwise be answered
#: with the mission's own frame — a plausible wrong number rather than an error.
#:
#: ``KIND_MISSION`` was ``"timelapse"`` until 2026-09-01, and that rename is the
#: sort that breaks a consumer silently: the ground segment already lost every
#: frame once to a ``kind`` mismatch it invented on its own side. Change it here
#: and in cubesat-groundstation's decodePhoto in the same breath.
KIND_PHOTO = "photo"
KIND_MISSION = "mission_frame"

#: ``status``.
STATUS_SUCCESS = "SUCCESS"
STATUS_ERROR = "ERROR"

#: ``reason_code``: why a capture did not happen, in one word, published
#: alongside the sentence a person reads. The sentence cannot travel over the
#: radio at all — a beacon field may not contain a space, and a truncated
#: English clause is exactly the plausible wrong answer this project refuses —
#: so the cause is published as a code as well. Three, because there are three
#: ways to be told no and they send somebody to three different places: the
#: mission state, the card, the camera.
REASON_STATE = "state"
REASON_NOSPACE = "nospace"
REASON_CAMERA = "camera"


def envelope(**fields: Any) -> str:
    """Serialise a payload, stamping it with the current wall-clock time.

    Every published payload carries ``timestamp`` as a Unix float. The DS1307
    RTC on the UPS HAT keeps this trustworthy with no network, which is what
    makes an offline mission's timestamps worth recording at all.
    """
    return json.dumps({"timestamp": time.time(), **fields})
