"""PAYLOAD — the science instruments and the camera.

Two devices with almost nothing in common, in one subsystem because they are the
two things this satellite carries in order to *observe*: the SEN0501
environmental package at ``0x22``, read on the science cadence, and the Camera
Module V2, used only when someone asks.

**One dead device degrades the payload, it does not silence the subsystem.** A
camera that will not open must not cost us the environmental record, and a
sensor that has fallen off the bus must not cost us the ability to take a photo.
Each is probed at start, each is reported separately in ``payload_status``, and
the service stays up either way — a vanished process takes its heartbeat with
it, and then OBC cannot tell a broken device from a broken service.

**The first ``payload_status`` is OBC's evidence.** DEPLOY waits for it as proof
that PAYLOAD's hardware answered *to the process that owns it*, which is why it
is published in ``on_start`` — as soon as the broker connection is up, without
waiting a whole cadence — and why it carries ``sensor.present`` and
``camera.present`` rather than merely existing. A status message that only
proved a process had started would be a heartbeat with extra steps, and DEPLOY
already has heartbeats.

**Two payload shapes share ``cubesat/payload/photo``**, and the ``kind`` field
says which:

    kind="photo"          a take_photo response, carrying photo_base64
    kind="mission_frame"  one frame of an open mission: path, size, sequence

A refusal is the third shape and carries no ``kind`` at all: ``status="ERROR"``,
the sentence a person reads in ``reason``, and one word from ``camera.py`` in
``reason_code``. The code is not decoration — since 2026-09-03 COMMS reads this
topic to answer ``!photo`` over the radio, where a field may not contain a space
and the sentence therefore cannot travel. It also has to tell a ``take_photo``
answer from a mission frame that happened to be taken in the same ten seconds,
which is what ``kind`` is for.

That asymmetry is deliberate and is argued out in ``payload/camera.py``: a
single photo is *for* the ground and the base64 is how it gets there, while a
mission's frames pushed through the broker would be tens of megabytes competing
with the telemetry the satellite exists to collect. Those are on the card, filed
under their mission.

The topic is **retained**, and that is the whole mechanism behind "show the last
photograph" in the profiles that keep no history: a browser opening five minutes
after somebody pressed the button gets the picture from the broker's own memory.
Nothing on the satellite stores it — mosquitto runs with ``persistence false``,
so the retained frame lives in RAM and dies with the broker.

**A mission photographs itself; there is no timelapse command.** While DHS
reports an open mission and the state permits the camera, frames are taken every
``photos.mission_interval_sec`` and stop when the mission closes.
``start_timelapse``/``stop_timelapse`` were removed on 2026-09-01: the automatic
series is the use case they existed for, and an interval nobody has to choose is
one fewer thing to garble over a radio link.

**A full card stops the camera, not the science.** The camera is the only
unbounded writer here, so it is the one that watches free space: below
``photos.min_free_mb`` a capture is refused with the room left in the reason and
a running series stops itself, both visible in ``payload_status.storage``.
Nothing on the card is deleted — retention belongs to DHS — and the science tick
is not gated on it, because a reading that is small, bounded and explains what
went wrong is the last thing to give up.

**PAYLOAD does not own missions.** DHS does, and PAYLOAD learns the id from the
retained ``dhs_status``. With no id — DHS not running, or a profile that records
nothing — a photograph is written to the tmpfs, published as pixels and deleted:
see ``camera.py``. Nothing reaches the SD card, which is the point in ``DEMO``
and ``EXPO``.
"""

from __future__ import annotations

import base64
import functools
import time
from typing import Any

from cubesat.common.service import Service
from cubesat.common.states import MissionState
from cubesat.common.topics import (
    REASON_CAMERA,
    REASON_NOSPACE,
    REASON_STATE,
    STATUS_ERROR,
    STATUS_SUCCESS,
    TOPICS,
)
from cubesat.hal import registry
from cubesat.hal.interfaces import Camera, EnvironmentSensor
from cubesat.payload import science
from cubesat.payload.camera import (
    CameraController,
    Capture,
    CaptureContext,
    StorageFull,
    refusal,
)

TAKE_PHOTO = "take_photo"

#: The commands PAYLOAD answers for. Everything else on cubesat/command belongs
#: to OBC or COMMS and is ignored in silence: those are not errors, and a
#: warning per set_profile would make every profile change look like a fault.
#:
#: ``start_timelapse`` and ``stop_timelapse`` were here until 2026-09-01. A
#: mission now photographs itself on a configured cadence, which was the only
#: use those commands had, and one fewer verb on the wire is one fewer thing to
#: get wrong over a radio link.
HANDLED = frozenset({TAKE_PHOTO})


class PayloadService(Service):
    name = "payload"
    cadence_key = "payload"
    #: ``adcs_status`` is here for the sidecar's position field, and for nothing
    #: else. It is the only way PAYLOAD can know where a photo was taken —
    #: PAYLOAD owns no receiver — and a photo with no location is a photo that
    #: cannot be put on a map afterwards. It is not retained, so "if one is
    #: known" is literal: before ADCS publishes, there is no position and the
    #: sidecar says null.
    subscriptions = ("command", "dhs_status", "adcs_status")

    def __init__(
        self,
        sensor: EnvironmentSensor | None = None,
        camera: Camera | None = None,
    ) -> None:
        super().__init__()
        self._sensor = sensor if sensor is not None else registry.environment()
        self._controller = CameraController(
            camera if camera is not None else registry.camera(), log=self.log
        )
        #: What the last probe or read said. Not a cached truth — it is exactly
        #: what payload_status reports, and it changes when the hardware does.
        #: None means "not probed yet", which is a different claim from False:
        #: the first status can go out before the camera probe has finished
        #: (see on_start), and reporting an unprobed camera as absent would be
        #: exactly the plausible-wrong-number this project refuses to publish.
        self._sensor_present: bool | None = None
        self._camera_present: bool | None = None
        self._readings = 0
        self._last_read: float | None = None
        #: From dhs_status. None until DHS opens a mission.
        self._mission_id: int | None = None
        #: The last GNSS sub-object that carried a fix, with its own timestamp.
        self._position: dict[str, Any] | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_start(self) -> None:
        """Probe both devices, say which answered, and report in.

        The status goes out here rather than on the first tick because DEPLOY is
        waiting for it and the window is bounded. Probing is a real transaction
        with each device, so the flags in that first message are evidence and
        not optimism.
        """
        self._clear_retained_photo()
        self._sensor_present = self._probe("SEN0501 environment", self._sensor)
        if self._sensor_present:
            # Report in NOW, with the camera honestly null: the camera probe
            # below imports picamera2, which on a cold Pi costs longer than the
            # whole DEPLOY window (35 s measured on first hardware bring-up,
            # 2026-08-28), and the sensor answering is already the evidence
            # DEPLOY is waiting for.
            self._publish_status()
        # Probed through the controller, not the device: the probe opens the
        # camera, and the controller is what schedules giving it back — an
        # opened sensor left running is SoC heat until the next profile change.
        self._camera_present = self._probe("Camera Module V2", self._controller)
        if not (self._sensor_present or self._camera_present):
            # Nothing answered, so there is nothing to report in about. The gap
            # on payload_status is the honest signal — publishing anyway would
            # tell DEPLOY that hardware answered when none of it did.
            self.log.error("neither the environmental sensor nor the camera answered")
            return
        self._publish_status()

    def on_connected(self) -> None:
        """Republish the retained status after every connect, reconnects included.

        A broker restart takes every retained message with it, and PAYLOAD
        otherwise publishes only on change — so after one, `payload_status`
        would be absent until a device went silent or a mission opened, and
        DEPLOY would find no evidence for a subsystem that is working perfectly.
        Nothing is republished if nothing ever answered: the gap is still the
        honest signal.
        """
        if self._sensor_present or self._camera_present:
            self._publish_status()

    def report_in(self) -> None:
        """A DEPLOY this service survived: republish the evidence it has.

        PAYLOAD publishes status only on change, so a DEMO→EXPO switch — where
        the profile keeps it running — would otherwise leave DEPLOY with no
        fresh message inside its window. Same guard as ``on_connected``: if
        nothing ever answered, the gap stays the honest signal.
        """
        if self._sensor_present or self._camera_present:
            self._publish_status()

    def _clear_retained_photo(self) -> None:
        """Drop whatever photograph the broker is still holding from last time.

        ``payload_photo`` is retained so that a page opened minutes after a
        capture still shows it. The cost is that the frame outlives the session
        it was taken in — and HOSTD starts this service as a profile is applied,
        so a start is exactly where one session ends and the next begins. A
        visitor at an EXPO stand meeting the previous demonstration's photograph
        as though it were current is the failure this prevents.

        An empty payload with the retain flag is how MQTT says "forget this
        topic"; the flag itself comes from ``topics.RETAINED``.
        """
        self.publish_raw("payload_photo", "")

    def _probe(self, label: str, device: Any) -> bool:
        try:
            answered = bool(device.probe())
        except Exception:
            # A driver may raise rather than return False; either way the answer
            # to "is it there?" is no, and the service still comes up.
            self.log.exception("%s probe failed", label)
            return False
        if answered:
            self.log.info("%s answered", label)
        else:
            self.log.error("%s is not answering; its telemetry will be unavailable", label)
        return answered

    def tick(self) -> None:
        reading = science.read(self._sensor, self.log)
        self._note_sensor(reading is not None)
        if reading is None:
            # Nothing measured, so nothing published. The retained status now
            # says the sensor is absent, which is what a consumer needs; a
            # payload of nulls would look like a measured environment of zero.
            return
        self._readings += 1
        self._last_read = time.time()
        self.publish("payload_data", **science.payload_from(reading))
        self.log.debug(
            "%.2f degC, %.2f %%RH, %.0f hPa, %.2f lux, uv_raw=%s",
            reading.temperature,
            reading.humidity,
            reading.pressure,
            reading.light,
            reading.uv_raw,
        )

    def on_state_change(self, previous: MissionState | None, current: MissionState) -> None:
        """Follow the state: stop what it no longer permits, resume what it does.

        The frame loop re-asks the gate too, and that is the authority for
        stopping. This is the *immediate* stop — a descent into LOW_POWER should
        turn the camera off now, not up to five minutes later at the end of the
        current interval — and it is also the only thing that starts the series
        again after a recovery, because the loop that would have re-asked has by
        then already ended.
        """
        if refusal(current) is not None and self._controller.mission_photos.active:
            self.log.warning("mission state is now %s; stopping photography", current.value)
        self._reconcile_mission_photos()

    def on_stop(self) -> None:
        """Give the camera back and let the frame thread finish."""
        self._controller.close()
        close = getattr(self._sensor, "close", None)
        if close is not None:
            close()

    # ── inbound ─────────────────────────────────────────────────────────────

    def on_message(self, topic: str, data: dict[str, Any]) -> None:
        if topic == TOPICS["command"]:
            self._on_command(data)
        elif topic == TOPICS["dhs_status"]:
            self._on_dhs_status(data)
        elif topic == TOPICS["adcs_status"]:
            self._on_adcs_status(data)

    def _on_dhs_status(self, data: dict[str, Any]) -> None:
        """Take the mission id from DHS, which owns it.

        Retained, so it arrives on connect. Kept as the integer DHS reports —
        the id is a row id, and every topic that names one (``dhs_status``,
        ``comms_data``, and now the payload topics) carries the same type; it
        becomes a directory name only at the filesystem boundary, in
        ``CameraController.path_for``. An id that is not an integer is treated
        as no mission rather than guessed at.
        """
        mission = data.get("mission")
        raw = mission.get("id") if isinstance(mission, dict) else None
        mission_id = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        if mission_id == self._mission_id:
            return
        self.log.info("mission id %s -> %s", self._mission_id, mission_id)
        self._mission_id = mission_id
        # A mission opening is what starts photography, and its closing is what
        # ends it. Publishes the status itself when it acts.
        self._reconcile_mission_photos()
        self._publish_status()

    def _on_adcs_status(self, data: dict[str, Any]) -> None:
        """Remember the last position that was actually a fix.

        A fixless reading is skipped rather than stored: ADCS already reports the
        last known fix in that case, and overwriting a real position with the
        same values under ``fix: false`` would only lose the ``at`` timestamp
        that says how old the coordinate a photo was stamped with really is.
        """
        gnss = data.get("gnss")
        if not isinstance(gnss, dict) or not gnss.get("fix"):
            return
        self._position = {**gnss, "at": data.get("timestamp")}

    def _on_command(self, data: dict[str, Any]) -> None:
        name = data.get("command")
        if not isinstance(name, str) or name not in HANDLED:
            return
        request_id = data.get("request_id")
        request_id = request_id if isinstance(request_id, str) else None
        params = data.get("params")
        params = params if isinstance(params, dict) else {}
        self.log.info("command %s (request_id=%s)", name, request_id)
        self._take_photo(request_id, params)

    # ── commands ────────────────────────────────────────────────────────────

    def _take_photo(self, request_id: str | None, params: dict[str, Any]) -> None:
        reason = refusal(self.mission_state)
        if reason is not None:
            self._refuse(request_id, reason, REASON_STATE)
            return
        try:
            capture = self._controller.capture(self._context(params))
        except StorageFull as exc:
            # A refusal, not a failure: the camera is fine and the card is not,
            # and the status published alongside says how much room is left.
            self._refuse(request_id, str(exc), REASON_NOSPACE)
            self._publish_status()
            return
        except Exception as exc:
            self.log.exception("photo capture failed")
            self._note_camera(False)
            self._refuse(request_id, f"Photo capture failed: {exc}", REASON_CAMERA)
            return
        self._note_camera(True)
        self.publish(
            "payload_photo",
            request_id=request_id,
            status=STATUS_SUCCESS,
            **_frame_fields(capture),
            # The image itself, and only here. See the module docstring.
            photo_base64=base64.b64encode(capture.photo.path.read_bytes()).decode("ascii"),
        )
        if capture.mission_id is None:
            # No mission, so the frame was written to the tmpfs and has now been
            # delivered as pixels: there is nothing left to keep and nothing on
            # the card to clean up later. The read above is why this happens here
            # rather than inside capture().
            self._controller.discard(capture.photo.path)

    # ── the mission's own photography ───────────────────────────────────────

    def _reconcile_mission_photos(self) -> None:
        """Start or stop the frame thread to match the mission and the state.

        One reconciler rather than a start path and a stop path, because both
        conditions move on their own: DHS opens and closes missions on
        ``dhs_status``, the mission state descends and recovers on
        ``obc_status``, and every combination has to end in the same place. Two
        handlers that each knew half of it is how a series ends up running with
        no mission open, or a mission runs with the camera idle after a
        LOW_POWER it already recovered from.

        Idempotent: called on every message that could have changed either.
        """
        wanted = (
            self._mission_id is not None
            and refusal(self.mission_state) is None
            # A camera that never answered cannot photograph, and a frame thread
            # failing three times in a row to say so is noise.
            and self._camera_present is not False
        )
        running = self._controller.mission_photos.active
        if wanted == running:
            return
        if wanted:
            self._controller.start_mission_photos(
                # A callable, not a snapshot: the mission, the state and the
                # position all change over a run measured in hours.
                context=functools.partial(self._context, {}),
                on_frame=self._publish_frame,
                on_finish=self._photos_finished,
            )
        else:
            self._controller.stop_mission_photos()
        self._publish_status()

    def _publish_frame(self, capture: Capture) -> None:
        """One mission frame: where it is and what it is, never the image."""
        self._note_camera(True)
        self.publish("payload_photo", status=STATUS_SUCCESS, **_frame_fields(capture))

    def _photos_finished(self, reason: str) -> None:
        """Called on the frame thread when the run ends, however it ended."""
        self.log.info("mission photography finished (%s)", reason)
        self._publish_status()

    def _refuse(self, request_id: str | None, reason: str, code: str) -> None:
        """Say no on the topic the request would have been answered on.

        A refusal that only reached the log would leave a ground station waiting
        for a photo that was never coming, and the reason is the whole point:
        "not allowed in LOW_POWER" is an answer, "no response" is a fault.

        Two spellings of the same no, and both are needed. ``reason`` is the
        sentence a person reads, with the numbers in it — how many megabytes are
        left, which state refused. ``reason_code`` is one word from
        ``camera.py``, and it exists because the sentence cannot cross the radio:
        a beacon field may not contain a space, so ``!photo``'s ack carries the
        code (``err=nospace``) and the dashboard carries the sentence.
        """
        self.log.warning("%s", reason)
        self.publish(
            "payload_photo",
            request_id=request_id,
            status=STATUS_ERROR,
            reason=reason,
            reason_code=code,
        )

    # ── state ───────────────────────────────────────────────────────────────

    def _context(self, params: dict[str, Any]) -> CaptureContext:
        return CaptureContext(
            mission_id=self._mission_id,
            state=self.mission_state,
            position=self._position,
            overlay=bool(params.get("overlay")),
        )

    def _note_sensor(self, present: bool) -> None:
        if present != self._sensor_present:
            self._sensor_present = present
            self._publish_status()

    def _note_camera(self, present: bool) -> None:
        if present != self._camera_present:
            self._camera_present = present
            self._publish_status()

    def _publish_status(self) -> None:
        """The retained status: what answered, what is running, where it goes."""
        self.publish(
            "payload_status",
            qos=1,
            sensor={
                "device": "SEN0501",
                # The distinction DEPLOY needs: this says the sensor answered,
                # not that the process started.
                "present": self._sensor_present,
                "readings": self._readings,
                "last_read": self._last_read,
            },
            camera={"device": "Camera Module V2", "present": self._camera_present},
            # Why a satellite has stopped taking photos belongs on the topic
            # that describes the payload, not only in a log nobody is reading at
            # a science fair.
            storage=self._controller.storage().as_dict(),
            mission_photos=self._controller.mission_photos.as_dict(),
            mission_id=self._mission_id,
            photo_dir=str(self._controller.path_for(self._mission_id)),
        )


def _frame_fields(capture: Capture) -> dict[str, Any]:
    """The fields both kinds of ``payload_photo`` message share."""
    return {
        # What a consumer branches on. Present on every message, including the
        # single-photo one, so nothing has to infer the kind from a missing key.
        "kind": capture.kind,
        "file": capture.photo.path.name,
        "path": str(capture.photo.path),
        "size_bytes": capture.size_bytes,
        "mission_id": capture.mission_id,
        "sequence": capture.sequence,
        # The sidecar contents, echoed so a consumer does not need the file to
        # render the overlay. Null when no overlay was asked for.
        "overlay": capture.sidecar,
    }


