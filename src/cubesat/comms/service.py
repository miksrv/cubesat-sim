"""COMMS — the link outward, and after the split, *only* that.

COMMS has two jobs and no others:

1. **Beacon over LoRa** — one compact line per cycle, assembled in
   ``beacon.py`` and transmitted through the Meshtastic node.
2. **Re-publish every inbound command onto ``cubesat/command``.**

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
which *link* a command arrived on — OBC, PAYLOAD and COMMS itself handle a
command relayed off the radio exactly as they handle one a laptop published on
the LAN. That is what makes ``FLIGHT`` recoverable: Wi-Fi is down, there is no
SSH, and ``profile hosted`` typed into a Meshtastic phone app is the way back.
If the command was addressed to COMMS it comes back around the loop as an
ordinary MQTT message and is handled there.

**Over the air the vocabulary is the compact table and nothing else**
(2026-09-03). A line off the radio is a compact verb or it is not a command:
hand-composed JSON used to be relayed verbatim and no longer is, so
``compact.py`` is the whole of what this satellite understands on the air, while
``cubesat/command`` still carries the full vocabulary for the dashboard, the CLI
and any other broker client. Two things go with the JSON path: ``set_profile``
over the radio takes a profile and nothing else, because the compact spelling
has no room for ``ttl_minutes`` or ``mission_label`` — the profile's own TTL and
a mission named after its start time are the defaults that answer for both — and
``delete_mission``, which has no compact spelling on purpose and now therefore
no over-the-air path at all.

**On the radio side that is one mesh channel and no other.** An uplink counts
only if it arrived on ``config.LORA_CHANNEL_INDEX`` — the private ``CubeSat``
channel with its own key. Everything else the node hears, the public primary
channel and direct messages included, is dropped in ``_collect_uplink`` before
it reaches ``cubesat/command`` *or* ``cubesat/comms/radio``, with one log line
and nothing transmitted in reply. Until 2026-09-02 this node sat alone with the
operator's on a private frequency slot; the mesh preset that gave it a ground
link again also put it in a room with several hundred strangers, whose ordinary
English chat — ``ping``, ``photo``, ``safe``, ``profile flight`` — is this
satellite's command vocabulary. See ``_refuse_uplink``.

**The profile is the envelope; the runtime flag lives inside it.** The active
profile's ``downlink`` block decides whether the radio may run at all, and
``set_comms_config`` can only turn it *off* — never turn a forbidden channel on.
A ground command that could widen the envelope would make the profile a
suggestion rather than a rule. The flag is also deliberately **not persisted**:
a restart returns to the profile's defaults, which is the same reasoning that
keeps the profile itself unrestored across a boot.

**``beacon_enabled`` rations the schedule and nothing else.** The inbox is
polled whenever the *profile* permits LoRa, whatever the runtime flag says. That
is what makes ``set_comms_config {"beacon_enabled": false}`` sent over the radio
recoverable over the same radio, instead of a one-way door with a comment
explaining it. A profile with ``downlink.lora: false`` polls nothing, because
there the radio is not merely quiet — it is not part of the mission.

**And an answer is not a beacon** (2026-09-03). Replies are gated on
``lora_listening`` — the profile — exactly like the inbox and like the
going-down beacon, so *every profile that runs COMMS answers the commands it
accepts*. The flag governs one thing: whether state goes out on a schedule,
unasked. Three instances inside five minutes on 2026-09-02 are why, all in
``DEMO``, where the profile's own default is quiet: ``!sys`` was answered from
the caches and the answer dropped; ``!photo`` took a photograph and said nothing;
and ``!beacon off`` had its own confirmation dropped by the flag it had just
set, which makes "the transmitter is off now" and "the command never arrived"
look identical to somebody holding a phone. A ``!`` line that fails to parse was
already answered, so the satellite was answering typos and swallowing successes.

The flag was called ``lora_enabled`` until that change, and the rename is not
cosmetic: ``beacon on|off`` was itself renamed from ``lora on|off`` on
2026-09-01 *because the old name said the wrong thing*, and a flag that no longer
decides whether LoRa transmits at all is the same lie one level down. The old
spelling is still accepted **on the way in** — the dashboard deployed on
2026-09-02 sends it — and still published alongside the new one, deprecated.

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

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cubesat.common import config
from cubesat.common import metrics as metrics_module
from cubesat.common import profiles as profiles_module
from cubesat.common.profiles import DownlinkSpec, ProfileConfig, ProfileError
from cubesat.common.service import Service
from cubesat.common.states import MissionState, Profile
from cubesat.common.topics import KIND_PHOTO, STATUS_ERROR, TOPICS
from cubesat.comms import beacon, compact
from cubesat.comms.mesh import MeshChannel
from cubesat.hal import registry
from cubesat.hal.interfaces import Radio, RadioMessage

GET_TELEMETRY = "get_telemetry"
SET_COMMS_CONFIG = "set_comms_config"
TAKE_PHOTO = "take_photo"

#: How much of a rejected uplink is quoted in the log. Truncating *here* is fine
#: and truncating a payload is not: this is a line for a human about something
#: that is being discarded anyway, which is the one place a shortened copy
#: cannot mislead.
LOG_EXCERPT_CHARS = 120

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
#: *are* the verdict (docs/concept.md → The radio command contract). Queries
#: skip the wait — they ask about the present, and the present delayed by ten
#: seconds is a different present.
ACK_DELAY_SEC = 10.0

#: How many replies may be waiting at once. Beyond this the **oldest** is
#: dropped, which is the same judgement the bound itself is made of: at one
#: reply per wake and a 30 s cadence in ``NOMINAL``, eight is already four
#: minutes of backlog, and an answer that arrives four minutes late describes a
#: satellite that has moved on. It is a memory bound as well — a flooded channel
#: must not grow this list — but the airtime and the staleness are what set the
#: number. A person at a phone keyboard cannot produce eight distinct commands
#: with side effects inside one wake; anything that does is not a conversation.
MAX_PENDING_ACKS = 8

#: The commands COMMS answers itself out of its own caches. These are the ones
#: that **collapse**: a query is a snapshot of the present, so a newer one
#: answers the older question too, and five nearly identical telemetry lines in
#: a row are airtime spent saying the same thing on a shared mesh. Every other
#: command keeps its own reply — see ``_schedule_ack``.
QUERIES = frozenset(
    {"ping", "get_position", "get_system", "get_environment", "get_mission"}
)


@dataclass
class _Ack:
    """One reply waiting for its turn on the air.

    ``fields`` is what was known when the command was accepted. What the reply
    can only learn *later* is not stored here at all: ``st=`` and ``pr=`` come
    from the caches at transmit time, and a ``take_photo`` ack reads PAYLOAD's
    own message about the frame — see ``photo_since``, which is the wall-clock
    moment the command was relayed, so a photograph published *before* it (the
    retained one the broker replays on every reconnect, or a mission frame) can
    never be reported as this command's outcome.
    """

    re: str
    due: float
    fields: dict[str, str] = field(default_factory=dict)
    #: A state query, so a newer query replaces it. False for anything with an
    #: effect: ``!photo`` twice is two photographs and deserves two answers.
    collapses: bool = False
    photo_since: float | None = None


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
    #: ``payload_photo`` is here for the ``!photo`` ack alone (2026-09-03) and
    #: is the one subscription whose payload must **not** be kept — see
    #: ``_on_payload_photo``.
    subscriptions = (
        "command",
        "eps_status",
        "adcs_status",
        "payload_data",
        "payload_photo",
        "dhs_status",
    )

    def __init__(
        self,
        radio: Radio | None = None,
        profiles: ProfileConfig | None = None,
        *,
        channel: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        #: Monotonic, and only for scheduling transmissions. The beacon's own
        #: ``t=`` field is wall-clock time from the DS1307, which is a different
        #: question — "when was this observed" rather than "how long since we
        #: last spent airtime" — and a clock adjustment must not answer both.
        self._clock = clock
        #: The one mesh channel a command may arrive on — the same index the
        #: driver transmits on, because a satellite that answers where it does
        #: not listen is a satellite nobody can hold a conversation with. Read
        #: once here rather than per message, and injectable so a test can name
        #: a channel without depending on which one is shipped.
        self._command_channel = channel if channel is not None else config.LORA_CHANNEL_INDEX
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
        #: ``beacon_enabled`` is false anyway, because the envelope is unknown.
        self._beacon_requested = True

        #: The ``at`` of the boot report already transmitted, so the retained
        #: obc_status redelivered on a reconnect does not say it twice.
        self._boot_reported: Any = None

        #: What the active profile permits, resolved once per profile change
        #: rather than per tick, so a misconfigured profile is logged once.
        self._downlink = NO_DOWNLINK
        self._downlink_profile: Profile | None = None

        #: The latest payload from each subsystem, kept whole. The beacon takes
        #: four numbers out of these; ``get_telemetry`` answers with all of it.
        #: ``payload_photo`` is deliberately **not** among them: it carries a
        #: whole image, and what is kept of it is five small fields — see
        #: ``_on_payload_photo``.
        self._eps: dict[str, Any] | None = None
        self._adcs: dict[str, Any] | None = None
        self._science: dict[str, Any] | None = None
        #: The last thing PAYLOAD said about a *requested* photograph, reduced
        #: to what an ack can carry. None until one is taken.
        self._photo: dict[str, Any] | None = None
        #: From ``dhs_status``. None until DHS opens a mission.
        self._mission_id: Any = None
        #: Rows recorded so far, same source — what ``!mission`` answers with.
        self._mission_rows: int | None = None

        self._last_uplink: float | None = None
        #: The replies waiting to be transmitted, oldest first. It was a single
        #: slot until 2026-09-03, which made the airtime budget out of losing
        #: things: two commands inside ten seconds and the first one's
        #: confirmation vanished without a word, which for ``!photo`` — a
        #: command with a physical side effect — is exactly the outcome the
        #: reply rule exists to prevent. The budget is now "one reply per wake",
        #: kept by ``_maybe_ack``; what collapses is only the state queries,
        #: which genuinely answer each other. See ``MAX_PENDING_ACKS``.
        self._pending_acks: list[_Ack] = []
        #: When the last beacon actually reached the air, on the monotonic
        #: clock. None means one has never gone out, and the first permitted
        #: wake transmits: a satellite that has just come up should say so.
        self._last_beacon: float | None = None
        #: The last status published, so the retained message is refreshed when
        #: something changes rather than once per cycle.
        self._published: dict[str, Any] | None = None

    # ── what is permitted ───────────────────────────────────────────────────

    @property
    def beacon_enabled(self) -> bool:
        """Whether the **scheduled** beacon may transmit right now.

        The profile first, then the runtime flag. Written this way round on
        purpose: the conjunction is what makes a ground command unable to widen
        the envelope, and it reads as the rule it implements.

        It governs the schedule and only the schedule. A reply to a command a
        person just sent, and the going-down beacon, are gated on
        ``lora_listening`` instead — a satellite in a position to hear a question
        is in a position to answer it.
        """
        return self._downlink.lora and self._beacon_requested

    @property
    def lora_listening(self) -> bool:
        """Whether the radio inbox is polled. The **profile** alone decides.

        Deliberately not conjoined with ``_beacon_requested``. Silencing a
        transmitter that can still hear is a recoverable state; silencing one
        that cannot is a locked door, and the key would be on the far side of
        it. A profile with ``downlink.lora: false`` polls nothing, because there
        the radio is not merely quiet — it is not part of the mission.

        Since 2026-09-03 this is also the gate on replies, which is why "the
        satellite always answers" has a boundary and it is stated here: a
        profile with ``downlink.lora: false`` says nothing at all. Today that is
        ``MAINTENANCE`` alone, which runs no COMMS, so in practice the rule
        reads *every profile that runs COMMS answers commands*.
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

    def _on_boot_report(self, report: Any) -> None:
        """Say once, on the air, how this run began — if it began interestingly.

        OBC publishes the verdict of the resume rule on ``obc_status``
        (``obc/resume.py``); this is where it becomes a transmission. Three
        things bound it:

        * **Only when something resumable was interrupted.** ``previous`` is
          null for an ordinary reboot in ``HOSTED``, and a desk reboot is not
          worth a shared mesh's airtime.
        * **Once per report.** Keyed on the report's own ``at``, so the retained
          ``obc_status`` arriving again on a reconnect does not re-transmit it.
          A COMMS restarted mid-trip does say it again — one line, and the
          alternative is persisting radio history to the card, which this
          service deliberately does not do.
        * **Gated on ``lora_listening``, not on ``beacon_enabled``.** Same rule
          as the going-down beacon and the acks: a runtime flag set an hour ago
          must not silence the message that explains what the satellite is now
          doing. A refusal is transmitted for exactly that reason — a satellite
          that silently declined to resume is indistinguishable from one that
          never woke up.
        """
        if not isinstance(report, dict):
            return
        previous = report.get("previous")
        if not isinstance(previous, str) or not previous:
            return
        stamp = report.get("at")
        if stamp == self._boot_reported:
            return
        self._boot_reported = stamp
        if not self.lora_listening:
            return
        fields = {"boot": previous, "rs": "1" if report.get("resumed") else "0"}
        reason = report.get("reason")
        if not report.get("resumed") and isinstance(reason, str) and reason:
            fields["why"] = reason
        if self._beacon(boot=fields):
            self.log.info("boot beacon sent: %s", fields)
            # It costs the airtime budget like any other transmission, so it
            # holds off the scheduled beacon that would otherwise follow it
            # seconds later saying the same thing more slowly.
            self._last_beacon = self._clock()
        else:
            self.log.warning("could not transmit the boot beacon")

    def _going_down_beacon(self) -> None:
        """One last line: where it was, what the battery was, and that it chose this.

        Gated on ``lora_listening`` — the profile — and **not** on
        ``beacon_enabled``. Same reasoning as the inbox, and as the reply that
        followed it here on 2026-09-03: a runtime flag somebody set an hour ago
        should not be able to silence the one message that explains a
        disappearance. A profile that forbids the radio still says
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
            elif topic == TOPICS["payload_photo"]:
                self._on_payload_photo(data)
            elif topic == TOPICS["dhs_status"]:
                self._on_dhs_status(data)

    def _on_payload_photo(self, data: dict[str, Any]) -> None:
        """Keep five small fields out of a message that carries a whole image.

        **Do not cache this payload the way the others are cached.**
        ``payload_photo`` carries ``photo_base64`` — the entire JPEG, close to a
        megabyte of it — and this service keeps "the latest payload from each
        subsystem, kept whole". Subscribing the ordinary way would park a base64
        copy of every photograph in the link service's memory for the sake of
        two integers, and the topic is *retained*, so the broker re-delivers the
        last one on every reconnect. The dict below is what survives the
        handler; the parsed message is transient and is what the two integers
        were extracted from.

        Only a ``take_photo`` answer is kept, and that is a positive rule rather
        than "not a mission frame": a mission photographs itself every 300 s, an
        ack window is 10 s wide, so roughly one ``!photo`` in thirty would
        otherwise be answered with somebody else's frame — a plausible wrong
        number rather than an error, which is the class this project refuses.
        A refusal carries no ``kind`` at all, and it is the half of the answer
        the operator most needs.
        """
        kind = data.get("kind")
        status = data.get("status")
        if kind != KIND_PHOTO and status != STATUS_ERROR:
            return
        self._photo = {
            # PAYLOAD's own stamp, not the arrival time: the retained frame
            # replayed on a reconnect must not read as an answer to a command
            # sent afterwards.
            "at": data.get("timestamp"),
            "ok": status != STATUS_ERROR,
            "size_bytes": data.get("size_bytes"),
            "sequence": data.get("sequence"),
            "reason_code": data.get("reason_code"),
        }

    def _on_obc_status(self, data: dict[str, Any]) -> None:
        """Re-resolve the downlink envelope when the profile changes.

        The base class has already absorbed the profile and the mission state
        from this message. Only a change is acted on, so the profiles file is
        read once per profile rather than twice a minute.
        """
        self._reconcile_downlink()
        # After the envelope, never before it: on the first status of a run this
        # message carries both the profile and the boot report, and a boot
        # beacon composed before `_downlink` is resolved would be gated on an
        # envelope nobody had read yet — silent, and marked as said.
        self._on_boot_report(data.get("boot"))

    def _reconcile_downlink(self) -> None:
        """Resolve what this profile permits, once per change."""
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
        # It is not a widening of the envelope — `beacon_enabled` is still the
        # conjunction — and it is not a lock either: `beacon on` from the radio,
        # from SSH or from the dashboard console works immediately afterwards.
        self._beacon_requested = self._downlink.beacon
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
        """Turn the scheduled beacon off, or back on, in memory only.

        **Turning it off over the radio costs nothing but the schedule.** The
        inbox keeps being polled — that is ``lora_listening``, the profile's
        call — and since 2026-09-03 the answer to a command goes out too, this
        command's own confirmation included. So there is no one-way door here:
        quiet is not deaf, and the satellite that was just silenced still says
        so. The flag resets to the profile's default on restart, which keeps a
        power cycle a way out of any state a command left.
        """
        if not isinstance(params, dict):
            self.log.warning("set_comms_config with no params; nothing changed")
            return
        # ``lora_enabled`` is the name this parameter had until 2026-09-03, and
        # it is accepted for as long as a client that predates the rename might
        # still be sending it: the dashboard build deployed on 2026-09-02 does,
        # and a satellite that stopped answering its own console on the day the
        # rename landed would have made the rename the fault. The new name wins
        # when both appear — a client that sends both is a client mid-upgrade,
        # and the spelling it learned last is the one it means.
        for name in ("lora_enabled", "beacon_enabled"):
            if name in params:
                self._beacon_requested = bool(params[name])
        for name, reason in RETIRED_PARAMS.items():
            if name in params:
                self.log.warning("%s is no longer COMMS' to set: %s", name, reason)
        self.log.info(
            "the beacon is now %s (profile permits lora=%s)",
            self.beacon_enabled,
            self._downlink.lora,
        )
        self._publish_status()

    # ── the uplink ──────────────────────────────────────────────────────────

    def _collect_uplink(self) -> None:
        """Relay whatever came in over the radio since the last wake.

        Gated on ``lora_listening`` — the profile — and not on the runtime
        flag. This is the line that makes turning the transmitter off over the
        radio survivable. Note that the channel filter below narrows what is
        *acted on*, never what is heard: the inbox is polled in full, for the
        reason written at ``lora_listening`` itself.

        **The credential is the channel, not the node.** Anyone holding the
        ``CubeSat`` key may command this satellite, and that is the decision
        rather than an accident — a key is portable, so an operator whose own
        node is flat loads the channel URL onto another one and carries on. An
        allowlist of node ids would make a dead battery a locked door with the
        key on the far side of it, which is the same mistake as a ``SAFE`` that
        cannot hear a ``recover``. It is also not even available: the two
        relayed chat packets seen on 2026-09-02 arrived with no ``fromId`` at
        all, so the sender field is empty on precisely the traffic that has to
        be refused.
        """
        if not self.lora_listening:
            return
        for message in self._mesh.receive():
            if message.channel != self._command_channel:
                self._refuse_uplink(message)
                continue
            # ``hops`` is on the record here and not only on ``comms_radio``
            # because the profiles where the radio matters are the ones that
            # persist least: the walk of 2026-09-05 proved the private channel
            # is relayed by a foreign node (docs/hardware-heltec-lora32-v4.md),
            # and it had to be proved from meshview, because ``HOSTED`` writes
            # no ``radio_log`` and this line carried only the sender and SNR.
            # 0 means heard directly; "not reported" means the node sent
            # neither hop field, which is not the same as zero.
            self.log.info(
                "LoRa message from %s (snr %s, hops %s)",
                message.sender or "an unknown node",
                message.snr,
                message.hops if message.hops is not None else "not reported",
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
            self._relay(message.text)

    def _refuse_uplink(self, message: RadioMessage) -> None:
        """Drop a message that arrived on somebody else's channel, in silence.

        **Silence outward and nothing onto MQTT.** No ack, no ``err=``, nothing
        transmitted — not even for a ``!`` line, whose contract exists so that
        *the operator* is never left wondering why nothing happened. Answering a
        stranger instead spends airtime on a shared band and teaches a mesh of
        several hundred nodes that this one talks back.

        **Refused before the ``comms_radio`` publish, not after**, and that
        order is the whole point of this method existing rather than a check
        further down. ``comms_radio`` is not a private surface: the dashboard's
        live Radio Link Log renders it — found on 2026-09-02, when a line typed
        into the public primary channel turned up in the widget — ``EXPO`` puts
        that dashboard in front of a room, and in ``FLIGHT`` and ``DIAG`` DHS
        writes those rows into ``radio_log`` on the card, from where they travel
        inside a mission export. Publishing a stranger's chat would mean
        displaying it to an audience and archiving it in our own flight record.
        On *our* channel the opposite rule holds and everything stays on the
        record, gibberish included: a malformed command from somebody holding
        the key is exactly what wants debugging.

        So the only trace is this line, and it carries the link facts without
        the text. A missing sender is rendered as missing — the null ``fromId``
        on relayed traffic is an unexplained observation, and a log that wrote
        "from None" or invented a name would read as an identification that
        never happened.
        """
        self.log.warning(
            "refused a LoRa message on channel %d: commands are taken from channel %d only "
            "(sender %s, snr %s, %d bytes; the text is not recorded)",
            message.channel,
            self._command_channel,
            message.sender if message.sender else "not reported",
            message.snr if message.snr is not None else "not reported",
            len(message.text.encode("utf-8")),
        )

    def _relay(self, text: str) -> None:
        """Put one uplinked line on the bus, if it is a command at all.

        **The compact table is the whole radio vocabulary** since 2026-09-03. A
        line is translated to canonical JSON *here*, once, on the way in (see
        ``compact.py``), and anything that table does not know is not a command.
        Hand-composed JSON used to be a third branch, relayed byte for byte;
        dropping it leaves one parser for the air instead of two, and nothing
        left that could disagree with ``compact.py`` about what a command is.

        The bare spelling and the ``!`` one part ways only when a line does not
        parse: ``!`` declared intent, so its typos are answered on the air; a
        bare line that is not a command costs a log line and no airtime.
        """
        if compact.is_compact(text):
            translated = compact.translate(text)
            if translated is None:
                self.log.warning("unknown compact uplink: %r", text[:LOG_EXCERPT_CHARS])
                self._schedule_ack("?", ok="0", err="unknown")
                return
            self._relay_compact(text, translated)
            return
        bare = compact.translate(text)
        if bare is not None:
            self._relay_compact(text, bare)
            return
        # Not a command in any spelling the radio has — JSON included, since
        # 2026-09-03. It is logged and it is not answered, and both halves are
        # chosen rather than left over.
        #
        # Logged, because this branch already sits behind the private-channel
        # filter: whoever typed the line holds the ``CubeSat`` key, so an
        # unrecognised one is far likelier an operator reaching for the JSON
        # that worked last week than a stranger's chat. This line is the trace
        # that person goes looking for, and it costs no airtime — which is the
        # resource that is actually scarce out here.
        #
        # Not answered, because a reply is precisely what ``!`` buys. Spending
        # an ``err=unknown`` on every stray line is what the bare spelling was
        # careful not to do on a shared band, and answering JSON in particular
        # would make a promise about it that the radio no longer keeps.
        self.log.warning("dropping an uplink that is not a command: %r", text[:LOG_EXCERPT_CHARS])

    def _relay_compact(self, text: str, translated: compact.Compact) -> None:
        self._last_uplink = time.time()
        if translated.command in QUERIES:
            # Answered from COMMS' own caches, immediately and without a relay:
            # the radio is the thing being asked. The beacon the answer rides
            # already carries state, battery and position, so a query reply is
            # the ordinary proof of life plus the fields that were asked for.
            self._schedule_ack(
                translated.verb, extra=self._query_reply(translated.command), query=True
            )
            return
        self.publish_raw("command", translated.json, qos=1)
        self.log.info("relayed %r as %s", text, translated.json)
        # ``re=`` names the verb that was typed, never the command it became.
        # `beacon on` came back as `re=set_comms_config` until 2026-09-03, which
        # asks a person on a phone to translate our vocabulary back into theirs
        # before they can believe the answer.
        self._schedule_ack(translated.verb, reads_photo=translated.command == TAKE_PHOTO)

    def _schedule_ack(
        self,
        re: str,
        *,
        ok: str | None = None,
        err: str | None = None,
        extra: dict[str, str] | None = None,
        query: bool = False,
        reads_photo: bool = False,
    ) -> None:
        """One out-of-schedule beacon, ``ACK_DELAY_SEC`` from now, saying ``re=``.

        The delay is what lets the ack carry the *outcome*: by the time it goes
        out, ``st=`` and ``pr=`` show what the command actually did, which is
        more honest than an ``ok=1`` COMMS cannot vouch for. ``ok``/``err``
        appear only where COMMS itself is the handler and actually knows, or
        where the handler said something the ack can read — ``reads_photo``
        being the one of those, resolved at transmit time in ``_photo_fields``.

        **What collapses and what does not.** A ``query`` is a snapshot of the
        present: it is answered without waiting, and a newer one replaces an
        unsent older one, because the fresh answer answers the earlier question
        too and five near-identical telemetry lines are airtime spent twice on a
        shared mesh. Everything else has an effect in the world — a photograph
        taken, a profile switched, a transmitter silenced — and keeps its own
        reply, because two photographs are two events and an operator who
        received one confirmation for two commands cannot tell which one it is
        about. That distinction is the fix: the single slot this replaced lost
        the first of any two commands sent inside ten seconds, silently.
        """
        fields = {"re": re}
        if ok is not None:
            fields["ok"] = ok
        if err is not None:
            fields["err"] = err
        if extra:
            fields.update(extra)
        if query:
            self._pending_acks = [ack for ack in self._pending_acks if not ack.collapses]
        self._pending_acks.append(
            _Ack(
                re=re,
                due=self._clock() + (0.0 if query else ACK_DELAY_SEC),
                fields=fields,
                collapses=query,
                photo_since=time.time() if reads_photo else None,
            )
        )
        while len(self._pending_acks) > MAX_PENDING_ACKS:
            # The oldest goes, and it is logged: an answer nobody will ever hear
            # is exactly the failure this queue exists to end, so it must not
            # also be invisible from the satellite's side.
            dropped = self._pending_acks.pop(0)
            self.log.warning(
                "the reply queue is full (%d); dropping the oldest unsent reply re=%s",
                MAX_PENDING_ACKS,
                dropped.re,
            )

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
        """Transmit **one** due reply, if the profile permits speaking at all.

        Gated on ``lora_listening`` and not on ``beacon_enabled`` (2026-09-03).
        An answer is not a beacon: a runtime flag somebody set an hour ago
        rations the *schedule*, and it must not be able to swallow the answer to
        a question somebody just asked — least of all the confirmation of
        ``beacon off`` itself, which is the one command whose success and whose
        total failure look identical from a phone. A profile that forbids the
        radio still says nothing, because there the radio is not part of the
        mission at all, and the queue is discarded rather than held: those
        replies would be answering commands that arrived under another profile.

        **One per wake is the airtime budget**, and it is deliberate: the queue
        exists so a reply is *late* rather than lost, not so a burst of commands
        can buy a burst of transmissions on a shared duty-cycle-limited mesh.
        The first *due* entry goes, not strictly the first — a query asked while
        a photo ack is still ripening is answered now rather than made to wait
        out somebody else's ten seconds.

        A failed send drops the reply for the same reason the going-down beacon
        steps over one: the next scheduled beacon carries the same state fields
        anyway, and retrying an answer to a question that is by then two minutes
        old spends airtime on a stale one.
        """
        if not self._pending_acks:
            return
        if not self.lora_listening:
            self._pending_acks.clear()
            return
        now = self._clock()
        position = next(
            (index for index, entry in enumerate(self._pending_acks) if entry.due <= now), None
        )
        if position is None:
            return
        ack = self._pending_acks.pop(position)
        fields = dict(ack.fields)
        if ack.photo_since is not None:
            fields.update(self._photo_fields(ack.photo_since))
        if self._beacon(reply=fields):
            self.log.info("ack sent: re=%s", ack.re)
            self._last_beacon = self._clock()

    def _photo_fields(self, since: float) -> dict[str, str]:
        """What PAYLOAD said about the photograph this ack is answering for.

        The general shape of a reply is that it reads the *handler's own*
        message, and this is the one command whose outcome is not a state field:
        a frame was written or it was not, and until 2026-09-03 ``!photo`` came
        back as an ordinary telemetry line that said nothing about it while the
        picture was visible in the dashboard.

        ``kb`` is the size the operator asked for and ``seq`` the frame's number
        within its mission (absent outside one — a photograph with no mission
        open is never filed and has no sequence). The mission id itself is
        already on every beacon as ``m=``, so it is not repeated. The free
        megabytes the contract also named are **not** here: they are on
        ``payload_status``, which COMMS does not hold, and the case they were
        wanted for arrives instead as ``err=nospace`` from the refusal itself.

        With nothing published since the command went out, the answer is
        ``err=noreply`` and deliberately no ``ok=``. Silence is COMMS' own
        observation and is worth reporting — PAYLOAD stopped by the profile, or
        a camera that never came back, look exactly like this — but ``ok=0``
        would be a verdict on a capture COMMS never saw, which is the invented
        number this project refuses.

        **Measured 2026-09-03, in `DIAG` on the satellite** (bench check V16, now
        closed). ``!photo`` from the phone at 20:20:40; PAYLOAD published the
        frame at 20:20:41.2 — a cold capture, camera opened for it — and the ack
        went out at 20:21:10 carrying ``re=photo ok=1 kb=485``. So the photograph
        was ready **29 seconds before** the reply was sent, with five services
        running and ADCS polling the 10 kHz bus at 2 Hz. The window is not set by
        the camera at all: it is ``ACK_DELAY_SEC`` on one side and the next COMMS
        wake on the other, and at the 30 s ``NOMINAL`` cadence that leaves roughly
        thirty times the margin the capture needs. ``err=noreply`` therefore means
        what it says — PAYLOAD did not answer — rather than "PAYLOAD was slow".
        """
        photo = self._photo
        at = (photo or {}).get("at")
        if photo is None or not isinstance(at, (int, float)) or at < since:
            return {"err": "noreply"}
        if not photo["ok"]:
            code = photo.get("reason_code")
            return {"ok": "0", "err": code if isinstance(code, str) and code else "failed"}
        fields = {"ok": "1"}
        size = photo.get("size_bytes")
        if isinstance(size, int) and not isinstance(size, bool):
            fields["kb"] = str(round(size / 1024))
        sequence = photo.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            fields["seq"] = str(sequence)
        return fields

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
        if not self.beacon_enabled:
            return
        interval = self._beacon_interval()
        if interval is None:
            return
        now = self._clock()
        if self._last_beacon is not None and now - self._last_beacon < interval:
            return
        if self._beacon():
            self._last_beacon = now

    def _beacon(
        self,
        *,
        going_down: bool = False,
        boot: dict[str, str] | None = None,
        reply: dict[str, str] | None = None,
    ) -> bool:
        """One line, one complete observation. See ``beacon.py`` for the format."""
        line = beacon.build(
            now=time.time(),
            state=self.mission_state.value if self.mission_state else None,
            profile=self.profile.value if self.profile else None,
            eps=self._eps,
            adcs=self._adcs,
            mission_id=self._mission_id,
            going_down=going_down,
            boot=boot,
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

        ``beacon_enabled`` is the **effective** value, the profile and the
        runtime flag already combined. A reader wants to know whether the radio
        is beaconing, not to have to fetch a profiles file to work it out.

        It is published under its old name ``lora_enabled`` as well, with the
        same value, and that duplication is temporary on purpose: the dashboard
        build deployed on 2026-09-02 reads the old key, and a satellite whose
        radio widget went blank on the day of a rename would have made the
        rename the fault. The mirror goes when the groundstation build that
        reads ``beacon_enabled`` is deployed — it is the only reader left, and
        nothing on the satellite consumes either key.

        ``lora_listening`` is reported separately from ``beacon_enabled`` for
        the same reason ``host_status`` reports the achieved profile separately
        from the requested one: since a silenced transmitter still hears, and
        since 2026-09-03 still answers, "quiet" and "deaf" are genuinely
        different states, and this topic is the only place that difference is
        visible. Collapsing them would turn a debuggable radio into a mystery.

        ``command_channel`` is the third of that set and exists for the same
        reason. Since the uplink filter landed, hearing a message and acting on
        one are different things too, and the index is what separates them —
        a satellite whose ground station is one channel out transmits and
        receives perfectly and simply never meets it, which is the hardest kind
        of radio fault to diagnose from the outside. It is on the status topic
        so the answer is one retained message rather than an SSH session, and
        it is the field that would show bench check V15 having gone the wrong
        way: every uplink refused, with the reported channel reading 0.
        """
        snapshot: dict[str, Any] = {
            "radio": self._mesh.describe(),
            "beacon_enabled": self.beacon_enabled,
            # Deprecated 2026-09-03, kept for the deployed dashboard. Same
            # value, always — never let these two disagree.
            "lora_enabled": self.beacon_enabled,
            "lora_listening": self.lora_listening,
            "command_channel": self._command_channel,
            "last_uplink": self._last_uplink,
        }
        if snapshot == self._published and not force:
            return
        self._published = snapshot
        self.publish("comms_status", qos=1, **snapshot)
