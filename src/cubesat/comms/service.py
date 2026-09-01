"""COMMS — the link outward, and after the split, *only* that.

COMMS has two jobs and no others:

1. **Beacon over LoRa** — one compact line per cycle, assembled in
   ``beacon.py`` and transmitted through the Meshtastic node.
2. **Re-publish every inbound command verbatim onto ``cubesat/command``.**

There was a third — POSTing the packet to a cloud ground station and draining a
queue of commands left there. It is gone: no such deployment exists, and the
ground segment is being rebuilt as an interface over the satellite's own
dashboard rather than as a service the satellite reports into. LoRa and the
local dashboard are the only channels now.

**It persists nothing.** No database, no files, no photographs. That used to be
this service's job and it is now DHS's, for a use case rather than for tidiness:
in ``FLIGHT`` and in ``SAFE`` the radio goes off while the GNSS track must keep
recording, and with the recorder living inside the link service, turning the
link off meant losing the recording. Do not move persistence back here.

**The second job is the load-bearing one.** Nothing downstream knows or cares
which channel a command arrived on — OBC, PAYLOAD and COMMS itself handle a
command relayed off the radio exactly as they handle one a laptop published on
the LAN. That is what makes ``FLIGHT`` recoverable: Wi-Fi is down, there is no
SSH, and ``set_profile`` typed into a Meshtastic phone app is the way back.
COMMS does not interpret what it relays; it checks the shape and puts it on the
bus, and if the command was addressed to COMMS it comes back around the loop as
an ordinary MQTT message and is handled there.

**The profile is the envelope; the runtime flag lives inside it.** The active
profile's ``downlink`` block decides whether the radio may run at all, and
``set_comms_config`` can only turn it *off* — never turn a forbidden channel on.
A ground command that could widen the envelope would make the profile a
suggestion rather than a rule. The flag is also deliberately **not persisted**:
a restart returns to the profile's defaults, which is the same reasoning that
keeps the profile itself unrestored across a boot.

**``lora_enabled`` silences transmission and nothing else.** The inbox is polled
whenever the *profile* permits LoRa, whatever the runtime flag says. That is
what makes ``set_comms_config {"lora_enabled": false}`` sent over the radio
recoverable over the same radio, instead of a one-way door with a comment
explaining it. A profile with ``downlink.lora: false`` polls nothing, because
there the radio is not merely quiet — it is not part of the mission.

**Waking and transmitting are two different intervals, and that is the most
important thing in this file.** The cadence table says how often COMMS wakes —
polls the radio inbox and refreshes its status.
``config.BEACON_INTERVALS`` says how often it *transmits*. In ``SAFE`` those are
60 s and 600 s: the satellite listens ten times for every time it talks.

The asymmetry is the one the radio actually wants. Listening costs nothing — the
Heltec is receiving regardless and a poll is a memory read — while transmitting
costs airtime on a shared, duty-cycle-limited mesh and a current spike on a
battery that is already the reason we are in ``SAFE``.

It is also a correctness fix, not a tuning one. An earlier version gave ``comms``
a cadence of 0 in ``SAFE`` and called it "radio silent". That silenced receiving
too — and ``SAFE`` is reachable from ``FLIGHT`` through a subsystem fault, on a
profile where the radio is the only way in and there is no SSH. The state where
``recover`` is most needed would have been the one state deaf to it. **``SAFE``
never stops listening, and it still beacons — rarely.** A satellite that goes
quiet exactly when something is wrong is a satellite nobody can help.

**``CRITICAL`` gets one beacon, not a schedule.** It is the only state permitted
to power the host off, and it lasts about ten seconds. So entering it transmits a
single ``down=1`` line immediately, on the thread the state change arrived on,
and ``CRITICAL`` stays absent from ``BEACON_INTERVALS`` — there is nothing to
repeat. Without that message a satellite that shut itself down at 8 % battery
leaves a silence indistinguishable from a crash, a flat radio, or somebody
walking out of range. With it, the ground has a recorded event.

**The first ``comms_status`` goes out as soon as the connection is up.** OBC's
``DEPLOY`` waits for it as evidence that the radio answered *to the process that
owns it* — COMMS was once the one mission service with no status topic, and its
absence is what pushed bring-up onto heartbeats, which prove that a process
started and nothing more. So the node is probed and the status published in
``on_start``, before any profile has been announced and regardless of whether
this profile will end up permitting LoRa at all: whether the hardware is there
is a different question from whether it is allowed to transmit.

**And again on every reconnect.** A broker restart takes every retained message
with it, so a status published only when something changes would leave OBC with
no evidence at all — and fail the bring-up of a perfectly healthy satellite
because *mosquitto* bounced. ``on_connected`` costs one message and closes that.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

from cubesat.common import config
from cubesat.common import metrics as metrics_module
from cubesat.common import profiles as profiles_module
from cubesat.common.profiles import DownlinkSpec, ProfileConfig, ProfileError
from cubesat.common.service import Service
from cubesat.common.states import MissionState, Profile
from cubesat.common.topics import TOPICS
from cubesat.comms import beacon, compact, mesh
from cubesat.comms.mesh import MeshChannel
from cubesat.hal import registry
from cubesat.hal.interfaces import Radio

GET_TELEMETRY = "get_telemetry"
SET_COMMS_CONFIG = "set_comms_config"

#: The commands COMMS answers for. Everything else on ``cubesat/command``
#: belongs to OBC or PAYLOAD and is ignored in silence — those are not errors,
#: and a warning per ``take_photo`` would make every photograph look like a
#: fault on the radio's status topic.
HANDLED = frozenset({GET_TELEMETRY, SET_COMMS_CONFIG})

#: Parameters ``set_comms_config`` used to take and does not any more, each with
#: the reason. Named so that an old ground client gets an explanation instead of
#: silence — a command that changes nothing and says nothing is the hardest kind
#: to diagnose from the far end of a radio link.
RETIRED_PARAMS = {
    "aggregation_enabled": (
        "DHS owns persistence, and whether telemetry is written is decided by the profile"
    ),
    "api_enabled": (
        "the cloud ground station is gone; LoRa and the local dashboard are the only channels"
    ),
}

#: What an unresolved profile permits: nothing. Before OBC has published a
#: status, COMMS does not know which envelope it is inside, and assuming the
#: permissive one would transmit under a profile that forbids transmitting.
NO_DOWNLINK = DownlinkSpec()

#: How long after an accepted uplink the ack beacon goes out — long enough for
#: the command's effect to land, so the ``st=`` and ``pr=`` fields it carries
#: *are* the verdict (docs/concept.md → The radio command contract). One
#: pending slot, so extras collapse into the latest: that is the whole event
#: budget for now. Queries skip the wait — they ask about the present, and the
#: present delayed by ten seconds is a different present.
ACK_DELAY_SEC = 10.0

#: The commands COMMS answers itself, mapped to the short ``re=`` name a phone
#: reader sees — the verb they typed, not the canonical command it became.
QUERIES = {
    "ping": "ping",
    "get_position": "pos",
    "get_system": "sys",
    "get_environment": "env",
    "get_mission": "mission",
}


class CommsService(Service):
    name = "comms"
    cadence_key = "comms"
    #: Everything the packet and the beacon are assembled from. ``obc_status``
    #: is added by the base class and carries the mission state and the profile,
    #: which are two of the beacon's fields and the source of the downlink
    #: envelope. ``dhs_status`` is here for the mission id alone — COMMS does not
    #: own missions and does not record one, but a beacon that says which
    #: session it belongs to is what lets a ground station line the line up
    #: against the archive it collects later.
    subscriptions = ("command", "eps_status", "adcs_status", "payload_data", "dhs_status")

    def __init__(
        self,
        radio: Radio | None = None,
        profiles: ProfileConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        #: Monotonic, and only for scheduling transmissions. The beacon's own
        #: ``t=`` field is wall-clock time from the DS1307, which is a different
        #: question — "when was this observed" rather than "how long since we
        #: last spent airtime" — and a clock adjustment must not answer both.
        self._clock = clock
        self._mesh = MeshChannel(radio if radio is not None else registry.radio(), self.log)
        self._profiles = profiles if profiles is not None else profiles_module.load()

        # Messages arrive on paho's network thread while tick() transmits on the
        # main one, and both read the caches and publish the status. One lock
        # around both, for the same reason DHS takes one: two threads racing the
        # status publish is how a retained message ends up describing a state
        # that never existed.
        self._lock = threading.RLock()

        #: What the ground has asked for. Set from the profile's own
        #: ``downlink.beacon`` the moment one is announced, and never persisted —
        #: see the module docstring. True until then, which matters only for the
        #: seconds before OBC's first status: with no profile resolved,
        #: ``lora_enabled`` is false anyway, because the envelope is unknown.
        self._lora_requested = True

        #: What the active profile permits, resolved once per profile change
        #: rather than per tick, so a misconfigured profile is logged once.
        self._downlink = NO_DOWNLINK
        self._downlink_profile: Profile | None = None

        #: The latest payload from each subsystem, kept whole. The beacon takes
        #: four numbers out of these; ``get_telemetry`` answers with all of it.
        self._eps: dict[str, Any] | None = None
        self._adcs: dict[str, Any] | None = None
        self._science: dict[str, Any] | None = None
        #: From ``dhs_status``. None until DHS opens a mission.
        self._mission_id: Any = None
        #: Rows recorded so far, same source — what ``!mission`` answers with.
        self._mission_rows: int | None = None

        self._last_uplink: float | None = None
        #: The one scheduled reply: (ack fields, monotonic due time). A single
        #: slot rather than a queue, so a burst of commands costs one
        #: transmission carrying the latest — the airtime budget by
        #: construction rather than by bookkeeping.
        self._pending_ack: tuple[dict[str, str], float] | None = None
        #: When the last beacon actually reached the air, on the monotonic
        #: clock. None means one has never gone out, and the first permitted
        #: wake transmits: a satellite that has just come up should say so.
        self._last_beacon: float | None = None
        #: The last status published, so the retained message is refreshed when
        #: something changes rather than once per cycle.
        self._published: dict[str, Any] | None = None

    # ── what is permitted ───────────────────────────────────────────────────

    @property
    def lora_enabled(self) -> bool:
        """Whether the radio may **transmit** right now.

        The profile first, then the runtime flag. Written this way round on
        purpose: the conjunction is what makes a ground command unable to widen
        the envelope, and it reads as the rule it implements.
        """
        return self._downlink.lora and self._lora_requested

    @property
    def lora_listening(self) -> bool:
        """Whether the radio inbox is polled. The **profile** alone decides.

        Deliberately not conjoined with ``_lora_requested``. Silencing a
        transmitter that can still hear is a recoverable state; silencing one
        that cannot is a locked door, and the key would be on the far side of
        it. A profile with ``downlink.lora: false`` polls nothing, because there
        the radio is not merely quiet — it is not part of the mission.
        """
        return self._downlink.lora

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_start(self) -> None:
        """Probe the radio and report in, whatever the profile turns out to be.

        Published here rather than on the first tick because DEPLOY is waiting
        for it inside a bounded window that is shorter than a nominal cadence.
        The probe is a real conversation with the node, so ``radio.present`` in
        that first message is evidence and not optimism.
        """
        with self._lock:
            self._mesh.open()
            self._publish_status()

    def on_connected(self) -> None:
        """Republish the retained status, every time the broker comes back.

        Called on the network thread after each successful connect. A broker
        restart takes every retained message with it, and this service only
        publishes on change — so without this, a reconnect would leave OBC with
        no ``comms_status`` at all and DEPLOY with no evidence the radio
        answered, on a satellite where nothing was actually wrong.
        """
        with self._lock:
            self._publish_status(force=True)

    def tick(self) -> None:
        """One wake: listen, maybe transmit, report in.

        Note what is *not* conditional here. Polling the inbox happens on every
        wake the profile permits, because listening is free and is the way back
        into a satellite that has gone wrong. Only the beacon is rationed.

        The whole wake is now under one lock. It used to release it around the
        cloud POST, which could block for an HTTP timeout while the going-down
        beacon waited on the broker thread with ten seconds of grace. With that
        channel gone, what the lock covers is a transmit and an inbox poll — a
        second or two at worst.
        """
        with self._lock:
            # Inbound first: a command that has been sitting in the radio's
            # inbox should be acted on this cycle, not after the beacon that is
            # about to spend the airtime. The ack goes before the scheduled
            # beacon on purpose: an ack *is* a full beacon, so sending it
            # satisfies the schedule and the schedule then defers itself.
            self._collect_uplink()
            self._maybe_ack()
            self._maybe_beacon()
            self._publish_status()

    def report_in(self) -> None:
        """DEPLOY wants fresh evidence, and this status publishes only on change.

        COMMS survives every profile switch by design — the radio listens in
        all profiles but ``MAINTENANCE`` — so it is the one service whose
        ``on_start`` report predates every DEPLOY after the first. Without
        this, a healthy radio failed the very first hardware bring-up: nothing
        about it had changed, so nothing was published, so it "never reported".
        """
        with self._lock:
            self._publish_status(force=True)

    def on_state_change(self, previous: MissionState | None, current: MissionState) -> None:
        """Say goodbye on the way into ``CRITICAL``.

        Called on the thread the state change arrived on, so the transmission
        happens now rather than at the next wake — which in ``LOW_POWER`` is a
        minute away and in ``CRITICAL`` never comes, because the host will have
        powered off.
        """
        if current is not MissionState.CRITICAL:
            return
        with self._lock:
            self._going_down_beacon()

    def _going_down_beacon(self) -> None:
        """One last line: where it was, what the battery was, and that it chose this.

        Gated on ``lora_listening`` — the profile — and **not** on
        ``lora_enabled``. Same reasoning as the inbox: a runtime flag somebody
        set an hour ago should not be able to silence the one message that
        explains a disappearance. A profile that forbids the radio still says
        nothing, because there the radio is not part of the mission at all.

        Failure is expected here more than anywhere else in this file: transmit
        current spikes can brown out the Heltec, and a pack at 8 % is where that
        is likeliest. It is logged and stepped over. DHS closing its mission
        cleanly matters more than this being heard, and OBC's flush grace must
        not be spent waiting on a radio.
        """
        if not self.lora_listening:
            return
        if self._beacon(going_down=True):
            self.log.info("going-down beacon sent")
            # It leaves like any other transmission, so it costs the budget like
            # any other — one rule, rather than a second unstated one. Moot in
            # practice: CRITICAL powers the host off, and the next start comes
            # up with no beacon history at all.
            self._last_beacon = self._clock()
        else:
            self.log.warning(
                "could not transmit the going-down beacon; powering off unheard"
            )
        self._publish_status()

    def on_stop(self) -> None:
        with self._lock:
            self._mesh.close()

    # ── inbound ─────────────────────────────────────────────────────────────

    def on_message(self, topic: str, data: dict[str, Any]) -> None:
        with self._lock:
            if topic == TOPICS["command"]:
                self._on_command(data)
            elif topic == TOPICS["obc_status"]:
                self._on_obc_status(data)
            elif topic == TOPICS["eps_status"]:
                self._eps = data
            elif topic == TOPICS["adcs_status"]:
                self._adcs = data
            elif topic == TOPICS["payload_data"]:
                self._science = data
            elif topic == TOPICS["dhs_status"]:
                self._on_dhs_status(data)

    def _on_obc_status(self, _data: dict[str, Any]) -> None:
        """Re-resolve the downlink envelope when the profile changes.

        The base class has already absorbed the profile and the mission state
        from this message. Only a change is acted on, so the profiles file is
        read once per profile rather than twice a minute.
        """
        profile = self.profile
        if profile is None or profile is self._downlink_profile:
            return
        self._downlink_profile = profile
        self._downlink = self._downlink_for(profile)
        # Entering a profile resets what the ground last asked for to that
        # profile's own starting state (2026-09-01). It has to work this way
        # round: `DEMO` says "listen, do not beacon", and a request carried over
        # from the trip before would make that setting true only until the first
        # time anybody ever turned the beacon on.
        #
        # It is not a widening of the envelope — `lora_enabled` is still the
        # conjunction — and it is not a lock either: `lora on` from the radio,
        # from SSH or from the dashboard console works immediately afterwards.
        self._lora_requested = self._downlink.beacon
        self.log.info(
            "profile %s permits lora=%s and starts the beacon %s",
            profile.value,
            self._downlink.lora,
            "on" if self._downlink.beacon else "off",
        )
        self._publish_status()

    def _downlink_for(self, profile: Profile) -> DownlinkSpec:
        """What this profile permits, read straight out of ``profiles.yaml``.

        Read here rather than carried on ``obc_status``: the envelope is a
        property of the profile, and a second copy of it on a message is a
        second place for it to be wrong.
        """
        try:
            return self._profiles.get(profile).downlink
        except ProfileError:
            # A profile OBC knows about and this profiles.yaml does not. The
            # conservative direction is silence: transmitting under an envelope
            # nobody has defined is how EXPO's radio ends up on in MAINTENANCE.
            self.log.warning(
                "profile %s is not defined here; no downlink permitted", profile.value
            )
            return NO_DOWNLINK

    def _on_dhs_status(self, data: dict[str, Any]) -> None:
        """Take the mission id from DHS, which owns it.

        Carried in the beacon so a ground station can line a received line up
        against the mission archive it collects later. COMMS neither opens nor
        records one.
        """
        mission = data.get("mission")
        self._mission_id = mission.get("id") if isinstance(mission, dict) else None
        rows = mission.get("rows") if isinstance(mission, dict) else None
        self._mission_rows = rows if isinstance(rows, int) else None

    def _on_command(self, data: dict[str, Any]) -> None:
        name = data.get("command")
        if not isinstance(name, str) or name not in HANDLED:
            return
        self.log.info("command %s", name)
        if name == GET_TELEMETRY:
            self._send_telemetry(data.get("request_id"))
        else:
            self._set_config(data.get("params"))

    # ── the two commands COMMS answers for ──────────────────────────────────

    def _send_telemetry(self, request_id: Any) -> None:
        """Answer with the whole cache on ``cubesat/comms/data``.

        Independent of the mission state and of both channel flags: this is a
        question asked over MQTT and answered over MQTT, and refusing it because
        the radio is off would make the one diagnostic that always works
        conditional on the thing being diagnosed.
        """
        self.publish(
            "comms_data",
            qos=1,
            request_id=request_id if isinstance(request_id, str) else None,
            **self.packet(),
        )

    def _set_config(self, params: Any) -> None:
        """Turn a permitted channel off, or back on, in memory only.

        Note what turning LoRa off over LoRa costs: the uplink stops being
        polled, so the way back on is MQTT or a restart. That is not an
        oversight — the flags reset to the profile's defaults on restart
        precisely so a power cycle is a way out of any state a command left.
        """
        if not isinstance(params, dict):
            self.log.warning("set_comms_config with no params; nothing changed")
            return
        if "lora_enabled" in params:
            self._lora_requested = bool(params["lora_enabled"])
        for name, reason in RETIRED_PARAMS.items():
            if name in params:
                self.log.warning("%s is no longer COMMS' to set: %s", name, reason)
        self.log.info(
            "lora now %s (profile permits %s)", self.lora_enabled, self._downlink.lora
        )
        self._publish_status()

    # ── the uplink ──────────────────────────────────────────────────────────

    def _collect_uplink(self) -> None:
        """Relay whatever came in over the radio since the last wake.

        Gated on ``lora_listening`` — the profile — and not on the runtime
        flag. This is the line that makes turning the transmitter off over the
        radio survivable.
        """
        if not self.lora_listening:
            return
        for message in self._mesh.receive():
            self.log.info(
                "LoRa message from %s (snr %s)", message.sender or "an unknown node", message.snr
            )
            # Before the relay, so a message that turns out to be gibberish is
            # still on the record: a radio session log that only holds the
            # messages that parsed would hide exactly the traffic somebody is
            # trying to debug. Link-quality fields are None where the node did
            # not report them, never substituted.
            self.publish(
                "comms_radio",
                direction="rx",
                text=message.text,
                bytes=len(message.text.encode("utf-8")),
                sender=message.sender,
                snr=message.snr,
                rssi=message.rssi,
                hops=message.hops,
            )
            self._relay("lora", message.text)

    def _relay(self, source: str, text: str) -> None:
        """Put one uplinked command on the bus, byte for byte as it arrived.

        Verbatim matters more than it looks. A field this build does not know
        about still reaches the service that does, and there is no re-encoding
        step that can quietly disagree with whoever composed the message.

        A compact line is the one exception, translated to canonical JSON
        *here*, once, on the way in (see ``compact.py``). The bare spelling and
        the ``!`` one part ways only when a line does not parse: ``!`` declared
        intent, so its typos are answered; a bare line that is not a command is
        somebody's chat, left alone.
        """
        if compact.is_compact(text):
            translated = compact.translate(text)
            if translated is None:
                self.log.warning("unknown compact uplink: %r", text[:120])
                self._schedule_ack("?", ok="0", err="unknown")
                return
            self._relay_compact(text, translated)
            return
        bare = compact.translate(text)
        if bare is not None:
            self._relay_compact(text, bare)
            return
        command = mesh.uplink_command(text, self.log, source=source)
        if command is None:
            return
        self._last_uplink = time.time()
        self.publish_raw("command", command, qos=1)
        self.log.info("relayed a command from the %s channel onto %s", source, TOPICS["command"])
        self._schedule_ack(self._command_name(command))

    def _relay_compact(self, text: str, translated: compact.Compact) -> None:
        self._last_uplink = time.time()
        if translated.command in QUERIES:
            # Answered from COMMS' own caches, immediately and without a relay:
            # the radio is the thing being asked. The beacon the answer rides
            # already carries state, battery and position, so a query reply is
            # the ordinary proof of life plus the fields that were asked for.
            short = QUERIES[translated.command]
            self._schedule_ack(short, extra=self._query_reply(translated.command), immediate=True)
            return
        self.publish_raw("command", translated.json, qos=1)
        self.log.info("relayed %r as %s", text, translated.json)
        self._schedule_ack(translated.command)

    @staticmethod
    def _command_name(command_json: str) -> str:
        # uplink_command has already vetted the JSON, so this cannot fail —
        # but the ack must never be the thing that breaks a relay.
        try:
            return str(json.loads(command_json)["command"])
        except Exception:  # pragma: no cover — vetted upstream
            return "?"

    def _schedule_ack(
        self,
        re: str,
        *,
        ok: str | None = None,
        err: str | None = None,
        extra: dict[str, str] | None = None,
        immediate: bool = False,
    ) -> None:
        """One out-of-schedule beacon, ``ACK_DELAY_SEC`` from now, saying ``re=``.

        The delay is what lets the ack carry the *outcome*: by the time it goes
        out, ``st=`` and ``pr=`` show what the command actually did, which is
        more honest than an ``ok=1`` COMMS cannot vouch for. ``ok``/``err``
        appear only where COMMS itself is the handler and actually knows.
        ``extra`` is a query's answer, riding the same slot.
        """
        fields = {"re": re}
        if ok is not None:
            fields["ok"] = ok
        if err is not None:
            fields["err"] = err
        if extra:
            fields.update(extra)
        self._pending_ack = (fields, self._clock() + (0.0 if immediate else ACK_DELAY_SEC))

    # ── the queries ─────────────────────────────────────────────────────────

    def _query_reply(self, command: str) -> dict[str, str]:
        """The fields a query answers with, from the caches COMMS already keeps.

        Every value that cannot be justified is absent, and an empty cache is
        ``ok=0 err=nodata`` rather than a line of zeros — the same
        withhold-rather-than-fabricate rule as everywhere else. ``age=`` is
        whole seconds since the source subsystem published, which is what makes
        a stale answer honest: PAYLOAD stopped by the profile still answers,
        with an age that says exactly how stale.
        """
        if command == "ping":
            return {}
        if command == "get_system":
            return self._system_reply()
        if command == "get_position":
            return self._position_reply()
        if command == "get_environment":
            return self._environment_reply()
        return self._mission_reply()

    def _system_reply(self) -> dict[str, str]:
        # Local psutil reads: no bus time, no cache, no age — always current.
        system = metrics_module.collect(str(config.DATA_DIR))
        fields = {
            "cpu": f"{system.cpu_percent:.0f}",
            "ram": f"{system.ram_percent:.0f}",
            "disk": f"{system.disk_percent:.0f}",
            "up": f"{system.uptime_seconds / 3600:.1f}h",
        }
        if system.cpu_temperature is not None:
            fields["tc"] = f"{system.cpu_temperature:.1f}"
        return fields

    def _position_reply(self) -> dict[str, str]:
        gnss = (self._adcs or {}).get("gnss")
        if not isinstance(gnss, dict) or not isinstance(gnss.get("lat"), (int, float)):
            return {"ok": "0", "err": "nodata"}
        # Unlike the scheduled beacon, a stale or fixless position is reported —
        # this is the lost-satellite query — because age= and fix= are here to
        # say precisely how much to trust it. The scheduled beacon has no room
        # for an age, so it stays live-fix-only.
        fields = {
            "lat": f"{gnss['lat']:.4f}",
            "lon": f"{gnss['lon']:.4f}",
            "fix": "1" if gnss.get("fix") else "0",
            "age": self._age_of(self._adcs),
        }
        if isinstance(gnss.get("alt"), (int, float)):
            fields["alt"] = f"{gnss['alt']:.0f}"
        if isinstance(gnss.get("satellites"), int):
            fields["sat"] = str(gnss["satellites"])
        return fields

    def _environment_reply(self) -> dict[str, str]:
        science = self._science
        if not isinstance(science, dict) or science.get("temperature") is None:
            return {"ok": "0", "err": "nodata"}
        fields = {"age": self._age_of(science)}
        for key, source, digits in (
            ("tc", "temperature", 1),
            ("rh", "humidity", 0),
            ("hpa", "pressure", 0),
            ("lux", "light", 0),
        ):
            value = science.get(source)
            if isinstance(value, (int, float)):
                fields[key] = f"{value:.{digits}f}"
        return fields

    def _mission_reply(self) -> dict[str, str]:
        if self._mission_id is None:
            return {"ok": "0", "err": "nodata"}
        fields = {"m": str(self._mission_id)}
        if self._mission_rows is not None:
            fields["rows"] = str(self._mission_rows)
        return fields

    @staticmethod
    def _age_of(payload: dict[str, Any] | None) -> str:
        """Whole seconds since the cached payload was published, as text."""
        timestamp = (payload or {}).get("timestamp")
        if not isinstance(timestamp, (int, float)):
            return "?"
        return str(max(0, round(time.time() - timestamp)))

    def _maybe_ack(self) -> None:
        """Transmit the pending reply, if it is due and speaking is permitted.

        The slot is cleared before the permission check: a satellite told to be
        quiet is quiet in its answers too — the reply is dropped, not queued
        for a louder day. A failed send is dropped for the same reason the
        going-down beacon steps over one: the next scheduled beacon carries the
        same state fields anyway.
        """
        if self._pending_ack is None:
            return
        fields, due = self._pending_ack
        if self._clock() < due:
            return
        self._pending_ack = None
        if not self.lora_enabled:
            return
        if self._beacon(reply=fields):
            self.log.info("ack sent: re=%s", fields["re"])
            self._last_beacon = self._clock()

    # ── the two channels ────────────────────────────────────────────────────

    def _beacon_interval(self) -> float | None:
        """Seconds between transmissions in this state, or None for none at all.

        A state the table does not name does not transmit. That covers ``BOOT``,
        ``STANDBY``, ``DEPLOY`` and ``CRITICAL``, and it is a refusal rather
        than a default on purpose: inventing an interval would be inventing a
        transmission policy for a state nobody wrote one for, and the table is
        meant to be the readable answer to "when does this thing talk?".

        Deliberately **not** scaled by the profile's ``cadence_scale``. That
        number exists so ``DIAG`` can poll sensors faster; airtime on a shared
        duty-cycle-limited mesh is not ours to speed up because a bench session
        would like more data.
        """
        state = self.mission_state
        if state is None:
            # No state yet, or one this build cannot name: the profile may
            # permit transmitting, but nothing here knows how often, and
            # guessing a rate is guessing at somebody else's airtime budget.
            return None
        return config.BEACON_INTERVALS.get(state.value)

    def _maybe_beacon(self) -> None:
        """Transmit, if this state permits it and enough time has passed.

        The interval is re-read every wake rather than latched when the last
        beacon went out, so a state change takes effect immediately: a satellite
        recovering from ``SAFE`` to ``NOMINAL`` does not wait out the remains of
        a ten-minute interval before it is heard from again.

        The clock advances only on a transmission that actually left — a send
        that failed spent no airtime, so it has no claim on the budget, and the
        next wake tries again instead of leaving a returning radio idle for the
        rest of the interval.
        """
        if not self.lora_enabled:
            return
        interval = self._beacon_interval()
        if interval is None:
            return
        now = self._clock()
        if self._last_beacon is not None and now - self._last_beacon < interval:
            return
        if self._beacon():
            self._last_beacon = now

    def _beacon(self, *, going_down: bool = False, reply: dict[str, str] | None = None) -> bool:
        """One line, one complete observation. See ``beacon.py`` for the format."""
        line = beacon.build(
            now=time.time(),
            state=self.mission_state.value if self.mission_state else None,
            profile=self.profile.value if self.profile else None,
            eps=self._eps,
            adcs=self._adcs,
            mission_id=self._mission_id,
            going_down=going_down,
            reply=reply,
        )
        sent = self._mesh.send(line)
        # Both outcomes go on the record: a transmission that failed spent no
        # airtime but says something about the link that a session log without
        # it would silently paper over. Publishing is not persistence — DHS
        # decides whether a row is written, exactly as it does for telemetry.
        self.publish(
            "comms_radio",
            direction="tx",
            kind="down" if going_down else "ack" if reply else "beacon",
            text=line,
            bytes=len(line.encode("utf-8")),
            sent=sent,
        )
        if not sent:
            return False
        self.log.debug("beacon sent (%d bytes): %s", len(line.encode("utf-8")), line)
        return True

    # ── the packet ──────────────────────────────────────────────────────────

    def packet(self) -> dict[str, Any]:
        """Everything COMMS knows, for a reader with no 240-byte problem.

        The subsystem payloads go in whole rather than field by field: a value
        measured today should not be lost because this file was written before
        it existed. The beacon's compression is a LoRa concession and stops at
        the radio.
        """
        return {
            "timestamp": time.time(),
            "obc_state": self.mission_state.value if self.mission_state else None,
            "profile": self.profile.value if self.profile else None,
            "mission_id": self._mission_id,
            "eps": self._eps or {},
            "adcs": self._adcs or {},
            "payload": self._science or {},
            # Collected here as well as by DHS: on a satellite the computer's own
            # temperature and free space are as much telemetry as the battery is,
            # and a ground client asking get_telemetry gets them from this packet.
            "system": metrics_module.collect(str(config.DATA_DIR)).as_dict(),
        }

    # ── outbound status ─────────────────────────────────────────────────────

    def _publish_status(self, *, force: bool = False) -> None:
        """The retained status: which channels are live, and did the radio answer.

        Published on every connect (``force``), whenever the channel is toggled,
        whenever the node stops or starts answering, and when an uplink arrives —
        but not once per wake. The message is retained, so a republish that says
        the same thing again buys nothing and would drown the one that says
        something new.

        ``lora_enabled`` is the **effective** value, the profile and the runtime
        flag already combined. A reader wants to know whether the radio is
        transmitting, not to have to fetch a profiles file to work it out.

        ``lora_listening`` is reported separately from ``lora_enabled`` for the
        same reason ``host_status`` reports the achieved profile separately from
        the requested one: since a silenced transmitter still hears, "quiet" and
        "deaf" are now genuinely different states, and this topic is the only
        place that difference is visible. Collapsing them would turn a
        debuggable radio into a mystery.
        """
        snapshot: dict[str, Any] = {
            "radio": self._mesh.describe(),
            "lora_enabled": self.lora_enabled,
            "lora_listening": self.lora_listening,
            "last_uplink": self._last_uplink,
        }
        if snapshot == self._published and not force:
            return
        self._published = snapshot
        self.publish("comms_status", qos=1, **snapshot)
