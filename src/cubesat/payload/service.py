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

    kind="photo"       a take_photo response, carrying photo_base64
    kind="timelapse"   one timelapse frame: path, size, sequence — no image

That asymmetry is deliberate and is argued out in ``payload/camera.py``: a
single photo is *for* the ground and the base64 is how it gets there, while five
hundred timelapse frames pushed through the broker would be hundreds of
megabytes competing with the telemetry the satellite exists to collect. The
frames are on disk, filed under their mission.

**A full card stops the camera, not the science.** The camera is the only
unbounded writer here, so it is the one that watches free space: below
``photos.min_free_mb`` a capture is refused with the room left in the reason and
a running timelapse stops itself, both visible in ``payload_status.storage``.
Nothing is deleted — retention belongs to DHS — and the science tick is not
gated on it, because a reading that is small, bounded and explains what went
wrong is the last thing to give up.

**PAYLOAD does not own missions.** DHS does, and PAYLOAD learns the id from the
retained ``dhs_status``. With no id — DHS not running, or no mission open yet —
photos are filed under ``photos/unfiled/`` and the log says so.
"""

from __future__ import annotations

import base64
import functools
import time
from typing import Any

from cubesat.common.service import Service
from cubesat.common.states import MissionState
from cubesat.common.topics import TOPICS
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
START_TIMELAPSE = "start_timelapse"
STOP_TIMELAPSE = "stop_timelapse"

#: The commands PAYLOAD answers for. Everything else on cubesat/command belongs
#: to OBC or COMMS and is ignored in silence: those are not errors, and a
#: warning per set_profile would make every profile change look like a fault.
HANDLED = frozenset({TAKE_PHOTO, START_TIMELAPSE, STOP_TIMELAPSE})

STATUS_SUCCESS = "SUCCESS"
STATUS_ERROR = "ERROR"


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
        would be absent until a device went silent or a timelapse started, and
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
        """Stop a timelapse the new state no longer permits.

        The frame loop re-asks the gate too, and that is the authority. This is
        the immediate stop: a descent into LOW_POWER should turn the camera off
        now, not at the end of the current interval, which on a slow timelapse
        could be minutes of a state that wanted the camera off.
        """
        if refusal(current) is None or not self._controller.timelapse.active:
            return
        self.log.warning("mission state is now %s; stopping the timelapse", current.value)
        self._controller.stop_timelapse()
        self._publish_status()

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
        if name == TAKE_PHOTO:
            self._take_photo(request_id, params)
        elif name == START_TIMELAPSE:
            self._start_timelapse(request_id, params)
        else:
            # stop_timelapse, and it is permitted from every mission state.
            self._stop_timelapse()

    # ── commands ────────────────────────────────────────────────────────────

    def _take_photo(self, request_id: str | None, params: dict[str, Any]) -> None:
        reason = refusal(self.mission_state)
        if reason is not None:
            self._refuse(request_id, reason)
            return
        try:
            capture = self._controller.capture(self._context(params))
        except StorageFull as exc:
            # A refusal, not a failure: the camera is fine and the card is not,
            # and the status published alongside says how much room is left.
            self._refuse(request_id, str(exc))
            self._publish_status()
            return
        except Exception as exc:
            self.log.exception("photo capture failed")
            self._note_camera(False)
            self._refuse(request_id, f"Photo capture failed: {exc}")
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

    def _start_timelapse(self, request_id: str | None, params: dict[str, Any]) -> None:
        interval = _interval_from(params)
        if interval is None:
            self._refuse(request_id, f"start_timelapse needs a positive interval_sec: {params!r}")
            return
        reason = refusal(self.mission_state)
        if reason is not None:
            self._refuse(request_id, reason)
            return
        if self._controller.timelapse.active:
            # Silently restarting would abandon a run somebody is watching, and
            # the two intervals would then be indistinguishable in the frames.
            self._refuse(request_id, "a timelapse is already running")
            return
        self._controller.start_timelapse(
            interval,
            # A callable, not a snapshot: a timelapse outlives the mission
            # state, the mission and the position it was started in.
            context=functools.partial(self._context, params),
            on_frame=self._publish_frame,
            on_finish=self._timelapse_finished,
        )
        self._publish_status()

    def _stop_timelapse(self) -> None:
        if self._controller.stop_timelapse():
            self._publish_status()
            return
        # Not an error: the retained status already says there is nothing
        # running, and a ground station repeating a stop is a good habit.
        self.log.info("stop_timelapse: no timelapse was running")

    def _publish_frame(self, capture: Capture) -> None:
        """One timelapse frame: where it is and what it is, never the image."""
        self._note_camera(True)
        self.publish("payload_photo", status=STATUS_SUCCESS, **_frame_fields(capture))

    def _timelapse_finished(self, reason: str) -> None:
        """Called on the frame thread when the run ends, however it ended."""
        self.log.info("timelapse finished (%s)", reason)
        self._publish_status()

    def _refuse(self, request_id: str | None, reason: str) -> None:
        """Say no on the topic the request would have been answered on.

        A refusal that only reached the log would leave a ground station waiting
        for a photo that was never coming, and the reason is the whole point:
        "not allowed in LOW_POWER" is an answer, "no response" is a fault.
        """
        self.log.warning("%s", reason)
        self.publish("payload_photo", request_id=request_id, status=STATUS_ERROR, reason=reason)

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
            timelapse=self._controller.timelapse.as_dict(),
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


def _interval_from(params: dict[str, Any]) -> float | None:
    """The requested timelapse interval, or None if there is not a usable one.

    Refused rather than defaulted: an interval is the whole content of a
    ``start_timelapse``, and guessing one would mean a garbled uplink silently
    starting a run at a rate nobody chose.
    """
    value = params.get("interval_sec")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)
