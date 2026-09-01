"""Capture policy: when a photo is allowed, where it is filed, and how a
timelapse runs without either flooding the broker or outliving the service.

**Capture is gated on the mission state.** ``NOMINAL`` and ``SCIENCE`` only;
``LOW_POWER`` and below refuse with a reason. The camera is the most expensive
thing PAYLOAD can do — the sensor draws power, the encode costs CPU and the
result costs disk — and ``LOW_POWER`` exists precisely to stop discretionary
work. ``stop_timelapse`` is permitted from *any* state, because stopping
something is never the dangerous direction: a gate that refuses to stop a
running timelapse in ``SAFE`` would keep the camera running in the one state
that most wanted it off.

**Photos are filed per mission**, under ``<data>/photos/<mission_id>/``, so a
gallery groups the way the charts do. PAYLOAD does not own missions — DHS does —
so the id arrives on ``dhs_status`` and is passed in here. When there is no id,
because DHS is not running or has not opened a mission yet, the photo is filed
under ``photos/unfiled/`` and the log says so. Inventing an id would put frames
into a mission that never existed; refusing the photo would lose a capture over
a bookkeeping detail. Neither is worth it.

**A timelapse publishes metadata, not pixels.** A single ``take_photo`` puts the
base64 JPEG on ``cubesat/payload/photo``, because that is how the Telegram bot
and the dashboard receive it and it is what the README documents. A timelapse
frame publishes only its path, size, sequence number and mission. Five hundred
frames at a few hundred kilobytes each is hundreds of megabytes pushed through
a broker that is simultaneously carrying the telemetry the satellite exists to
collect — on a Pi, over Wi-Fi, sharing a mesh radio's uplink. The frames are on
disk, filed under their mission, and that is where a gallery should read them
from. The ``kind`` field on every ``payload_photo`` message says which of the
two a consumer is looking at, so nothing has to infer it from the presence of a
base64 blob.

**The camera is the only unbounded writer on this satellite**, so it is the one
that has to watch the card. Below ``photos.min_free_mb`` a capture is refused
and a running timelapse stops itself, because the next write to fail on a full
card is the telemetry row the mission exists to record — and the recorder
tidying up later is no comfort when the card is full now. PAYLOAD deletes
nothing: retention is DHS's job, and a service that writes files and also
decides which ones to remove is one bug away from removing the wrong ones. The
science tick is deliberately not gated on it either: that reading is small,
bounded, and the thing most likely to explain what went wrong.

**One capture at a time.** The camera is a single exclusive resource and there
are two callers — a ground command arriving on the MQTT thread and the timelapse
thread — so every capture goes through one lock. Blocking rather than refusing:
a capture is a second or so, and turning a common overlap into a lost photo
would be a worse trade than a brief wait.
"""

from __future__ import annotations

import functools
import json
import logging
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cubesat.common import config
from cubesat.common.states import CAMERA_ALLOWED_STATES, MissionState
from cubesat.hal.interfaces import Camera, Photo

#: Where a photo goes when no mission is open. A real directory rather than the
#: photos root, so "we did not know the mission" stays visible afterwards
#: instead of looking like a filing mistake.
UNFILED = "unfiled"

#: The ``kind`` field on cubesat/payload/photo. One topic, two payload shapes —
#: see the module docstring for why only one of them carries the image.
KIND_PHOTO = "photo"
KIND_TIMELAPSE = "timelapse"

#: Free space is reported and compared in mebibytes, which is what
#: ``photos.min_free_mb`` means and what ``df -m`` prints.
BYTES_PER_MB = 1024 * 1024

#: How many frames in a row may fail before the timelapse gives up. One failure
#: is a hiccup worth riding out over a long run; three in a row is a camera that
#: has gone away, and continuing would fill the log for hours.
MAX_CONSECUTIVE_FRAME_FAILURES = 3

#: How long ``stop_timelapse`` waits for the frame thread. Longer than one
#: capture, so a stop issued mid-frame still returns having actually stopped.
TIMELAPSE_JOIN_TIMEOUT_SEC = 10.0


class StorageFull(Exception):
    """The free-space floor refused a capture.

    A refusal rather than a failure, and separate from every other exception a
    capture can raise, because the two want different words on the way out: the
    camera is fine, the card is not, and telling a ground station "capture
    failed" would send someone to look at the wrong thing.
    """


@dataclass(frozen=True)
class Storage:
    """Free space where the photos go, and whether it is blocking captures."""

    free_mb: float
    min_free_mb: float

    @property
    def blocked(self) -> bool:
        """Whether a capture is refused right now.

        Strictly below the floor. Exactly at it is still permitted: the floor is
        "keep this much free", so the reserve is intact until something eats
        into it — and the photo that does is the one refused.
        """
        return self.free_mb < self.min_free_mb

    def as_dict(self) -> dict[str, Any]:
        return {
            "free_mb": round(self.free_mb, 1),
            "min_free_mb": self.min_free_mb,
            "blocked": self.blocked,
        }


def refusal(state: MissionState | None) -> str | None:
    """Why a capture may not happen now, or None if it may.

    The wording is the README's, because it is what reaches the ground in the
    error payload and an operator reading it should see the state that refused.
    """
    if state is None:
        # Before the first obc/status. Refusing is the conservative direction:
        # the state might be SAFE, and a camera is not what SAFE wants.
        return "Photo capture not allowed: the mission state is not known yet"
    if state not in CAMERA_ALLOWED_STATES:
        return f"Photo capture not allowed: mission state is {state.value!r}"
    return None


@dataclass(frozen=True)
class CaptureContext:
    """What PAYLOAD knows from *other* services at the moment of a capture.

    Passed in rather than reached for, because a timelapse spans minutes: the
    mission can change, the state can change and the satellite can move between
    one frame and the next, and every frame should record the truth of its own
    moment.
    """

    mission_id: int | None = None
    state: MissionState | None = None
    #: The last known GNSS sub-object from ``adcs_status``, or None.
    position: dict[str, Any] | None = None
    overlay: bool = False


@dataclass(frozen=True)
class Capture:
    """A photo that exists on disk, and everything needed to talk about it."""

    photo: Photo
    kind: str
    mission_id: int | None
    #: The sidecar contents when one was written, else None.
    sidecar: dict[str, Any] | None = None
    #: 1-based, and only for timelapse frames.
    sequence: int | None = None

    @property
    def size_bytes(self) -> int:
        return self.photo.path.stat().st_size


@dataclass
class TimelapseState:
    """What the retained ``payload_status`` says about the timelapse."""

    active: bool = False
    interval_sec: float | None = None
    #: Frames actually on disk, not frames attempted.
    frames: int = 0
    #: Why the last run ended, or None if none has ever been started. What makes
    #: "stopped because the card is nearly full" distinguishable from "nobody
    #: ever asked for one", which is the difference a science fair needs.
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "interval_sec": self.interval_sec,
            "frames": self.frames,
            "reason": self.reason,
        }


class _Timelapse:
    """One timelapse run: its thread, its stop flag and its frame counter.

    Not a dataclass, because the thread has to be handed a reference to the run
    it belongs to and that is circular at construction time.
    """

    def __init__(self, interval_sec: float, target: Callable[[_Timelapse], None]) -> None:
        self.interval_sec = interval_sec
        self.stop = threading.Event()
        #: Frames written. Bumped only after a capture succeeds, so a retried
        #: sequence number reuses its slot and the count never claims a frame
        #: the card refused.
        self.frames = 0
        self.thread = threading.Thread(
            target=target,
            args=(self,),
            name="timelapse",
            # Daemon so a hung capture cannot keep the process alive past
            # SIGTERM; close() still joins it for an orderly stop first.
            daemon=True,
        )


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CameraController:
    """PAYLOAD's side of the camera: policy, filing and the timelapse thread."""

    def __init__(
        self,
        camera: Camera,
        *,
        photos_dir: Path | None = None,
        log: logging.Logger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._camera = camera
        self._root = photos_dir if photos_dir is not None else config.PHOTOS_DIR
        self.log = log or logging.getLogger("payload.camera")
        self._clock = clock
        self._capture_lock = threading.Lock()
        self._timelapse: _Timelapse | None = None
        self._unfiled_warned = False
        #: Why the last timelapse ended. None means none was ever started.
        self._timelapse_reason: str | None = None
        #: The pending idle close, and the count of camera uses that arms it.
        #: The generation is what makes a stale timer harmless: a timer that was
        #: already firing when a capture re-armed cannot be cancelled, but it
        #: can notice the world moved on and decline to close a camera that was
        #: just used.
        self._idle_timer: threading.Timer | None = None
        self._idle_generation = 0

    def probe(self) -> bool:
        """Whether the camera answers, and PAYLOAD's only way of asking.

        Routed through the controller rather than exposing the device, because
        a probe *opens* the camera — that is what makes it evidence — and an
        opened camera is heat until something schedules giving it back. The
        idle close below is that something, so every path that touches the
        sensor has to end here or in ``capture``.
        """
        try:
            return bool(self._camera.probe())
        finally:
            self._arm_idle_close()

    # ── the card ────────────────────────────────────────────────────────────

    def storage(self) -> Storage:
        """How much room is left where the photos go.

        Cheap — a ``statvfs`` — next to a capture, so it is asked at the moment
        of writing rather than the moment of asking. A timelapse crossing the
        floor mid-run is exactly the case that matters, and it cannot be caught
        by a check made when the command arrived.
        """
        return Storage(free_mb=self._free_mb(), min_free_mb=float(config.PHOTOS_MIN_FREE_MB))

    def _free_mb(self) -> float:
        """Free megabytes on the filesystem the photos root lives on.

        Walks up to an existing ancestor because the root may not have been
        created yet — asking where a mission files creates nothing — and the
        free space of a directory that does not exist yet is the free space of
        the filesystem it will be created on.

        An unreadable filesystem fails **open**: a check that cannot answer must
        not be the reason a photo is lost. The card filling up after that is a
        smaller problem than never taking the picture.
        """
        directory = self._root
        while not directory.exists() and directory != directory.parent:
            directory = directory.parent
        try:
            return shutil.disk_usage(directory).free / BYTES_PER_MB
        except OSError as exc:
            self.log.warning("free space at %s is unreadable (%s); allowing the capture",
                             directory, exc)
            return float("inf")

    def _refuse_if_full(self) -> None:
        storage = self.storage()
        if storage.blocked:
            raise StorageFull(
                f"Photo capture not allowed: {storage.free_mb:.0f} MB free at {self._root}, "
                f"below the {storage.min_free_mb:.0f} MB floor"
            )

    # ── filing ──────────────────────────────────────────────────────────────

    def path_for(self, mission_id: int | None) -> Path:
        """Where this mission's photos go. Creates nothing — see below.

        This is the one place the id becomes a string: the directory name is
        the filesystem's representation of it, while everywhere else — the
        wire, the sidecar, the logs — carries the integer DHS reported. HOSTD's
        photo-directory rule ("a run of digits") is matched by exactly this
        rendering.
        """
        return self._root / (UNFILED if mission_id is None else str(mission_id))

    def directory_for(self, mission_id: int | None) -> Path:
        """The same directory, brought into existence and complained about.

        Separate from ``path_for`` because publishing a status must not create
        directories or emit warnings: only an actual capture should do either,
        or a satellite that never takes a photo still grows an ``unfiled/``
        folder and a log line about a mission it was never asked to file into.

        The state directory itself is systemd's job (``StateDirectory=cubesat``)
        and nothing here creates it. This subdirectory is different: a mission id
        does not exist until DHS opens a mission, so no unit file can name it in
        advance.
        """
        if mission_id is None and not self._unfiled_warned:
            self._unfiled_warned = True
            self.log.warning(
                "no mission is open, so photos are being filed under %s/ — DHS has not "
                "reported a mission id",
                UNFILED,
            )
        if mission_id is not None:
            # Armed again, so a later gap in DHS' reporting is said out loud
            # rather than swallowed by a flag set hours earlier.
            self._unfiled_warned = False
        directory = self.path_for(mission_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _filename(self, taken_at: float, kind: str, sequence: int | None) -> str:
        stamp = datetime.fromtimestamp(taken_at, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        if kind == KIND_TIMELAPSE:
            # The sequence number is in the name because a frame every second
            # would otherwise collide with the one before it, and because a
            # directory listing should show the order the frames were taken in.
            return f"timelapse_{stamp}_{sequence:04d}.jpg"
        return f"photo_{stamp}.jpg"

    # ── capture ─────────────────────────────────────────────────────────────

    def capture(
        self,
        context: CaptureContext,
        *,
        kind: str = KIND_PHOTO,
        sequence: int | None = None,
    ) -> Capture:
        """Take one photo. Blocks if another capture is in flight.

        Raises ``StorageFull`` when the card is below the floor — before
        anything is created, so a full card does not even leave an empty mission
        directory behind.
        """
        with self._capture_lock:
            self._refuse_if_full()
            taken_at = self._clock()
            directory = self.directory_for(context.mission_id)
            path = directory / self._filename(taken_at, kind, sequence)
            sidecar = self._sidecar(context, taken_at, path) if context.overlay else None
            try:
                photo = self._camera.capture(path, overlay=_overlay_text(sidecar))
            finally:
                # Armed on failure too: a capture that raised still touched —
                # and may have opened — the sensor, and a camera that broke
                # mid-shot is not one worth keeping powered.
                self._arm_idle_close()
            if sidecar is not None:
                # Written after the capture so a sidecar never outlives a photo
                # that failed to be taken.
                sidecar["width"] = photo.width
                sidecar["height"] = photo.height
                path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2))
            self.log.info("captured %s (%s)", path.name, kind)
            return Capture(
                photo=photo,
                kind=kind,
                mission_id=context.mission_id,
                sidecar=sidecar,
                sequence=sequence,
            )

    def _sidecar(
        self, context: CaptureContext, taken_at: float, path: Path
    ) -> dict[str, Any]:
        """The overlay, as a JSON file beside the photo instead of ink on it.

        Everything the burnt-in caption would have said, in a form a dashboard
        can render over the image, a database can index and an image processor
        can ignore. The position carries its own ``at`` timestamp because a last
        known fix can be minutes old, and a coordinate with no age attached is
        the kind of plausible wrong number this project keeps trying to avoid.
        """
        return {
            "captured_at": _iso(taken_at),
            "timestamp": taken_at,
            "file": path.name,
            "mission_id": context.mission_id,
            "mission_state": context.state.value if context.state is not None else None,
            "position": context.position,
        }

    # ── idle close ──────────────────────────────────────────────────────────

    def _arm_idle_close(self) -> None:
        """Schedule giving the sensor back, restarting the countdown.

        Why close at all: an open Picamera2 runs the ISP pipeline — metering,
        white balance, the lores stream — continuously, which is SoC heat for
        as long as nobody takes a photo. Reopening costs about a second, so a
        timelapse faster than the window never pays it, and a slower one trades
        a second per frame for a camera that is cold between frames.

        Read from config at each call, like the timelapse floor: a bench
        session should be able to change the window without a redeploy.
        """
        self._idle_generation += 1
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        window = config.CAMERA_IDLE_CLOSE_SEC
        if window <= 0:
            return
        timer = threading.Timer(window, self._idle_close, args=(self._idle_generation,))
        # Daemon for the same reason the timelapse thread is: a pending close
        # must not keep the process alive past SIGTERM.
        timer.daemon = True
        timer.name = "camera-idle-close"
        timer.start()
        self._idle_timer = timer

    def _idle_close(self, generation: int) -> None:
        """The timer's side: close the camera unless it was used again.

        The generation check closes the race that ``cancel()`` cannot: a timer
        that has already started firing when a capture re-arms would otherwise
        close the sensor immediately after the capture that wanted it warm.
        """
        with self._capture_lock:
            if generation != self._idle_generation:
                return
            self._camera.close()
            self.log.info(
                "camera closed after %.0fs idle; the next capture re-opens it",
                config.CAMERA_IDLE_CLOSE_SEC,
            )

    # ── timelapse ───────────────────────────────────────────────────────────

    @property
    def timelapse(self) -> TimelapseState:
        active = self._timelapse
        if active is None:
            return TimelapseState(reason=self._timelapse_reason)
        return TimelapseState(
            active=not active.stop.is_set(),
            interval_sec=active.interval_sec,
            frames=active.frames,
            # None while it is still running: a reason is why it ended.
            reason=None if not active.stop.is_set() else self._timelapse_reason,
        )

    def start_timelapse(
        self,
        interval_sec: float,
        *,
        context: Callable[[], CaptureContext],
        on_frame: Callable[[Capture], None],
        on_finish: Callable[[str], None],
    ) -> float:
        """Start the frame thread and return the interval it actually uses.

        ``context`` is a callable, not a value: the mission, the state and the
        position all change over a run measured in hours, and each frame asks
        again rather than recording the situation at the moment somebody typed
        the command.
        """
        # The floor is a setting (``camera.min_timelapse_interval_sec``) and is
        # read here rather than captured at import: a capture takes the better
        # part of a second, so anything below it is not a faster timelapse, it
        # is a camera running flat out with a queue behind it.
        floor = config.MIN_TIMELAPSE_INTERVAL_SEC
        interval = max(floor, float(interval_sec))
        if interval != interval_sec:
            self.log.warning(
                "timelapse interval %.3fs is below the %.1fs floor; using %.1fs",
                interval_sec,
                floor,
                interval,
            )
        timelapse = _Timelapse(
            interval,
            functools.partial(
                self._run, context=context, on_frame=on_frame, on_finish=on_finish
            ),
        )
        self._timelapse = timelapse
        self._timelapse_reason = None
        timelapse.thread.start()
        self.log.info("timelapse started at %.1fs intervals", interval)
        return interval

    def stop_timelapse(self) -> bool:
        """Stop the timelapse if one is running. True if one still was.

        Permitted from every mission state — see the module docstring. False
        covers both "there was never one" and "it already ended itself", because
        to a caller deciding whether anything changed those are the same answer.
        """
        timelapse, self._timelapse = self._timelapse, None
        if timelapse is None:
            return False
        running = not timelapse.stop.is_set()
        timelapse.stop.set()
        if timelapse.thread is not threading.current_thread():
            # Not when the frame thread is stopping itself, which would be a
            # thread joining itself and is an error rather than a wait.
            timelapse.thread.join(timeout=TIMELAPSE_JOIN_TIMEOUT_SEC)
        if running:
            self.log.info("timelapse stopped after %d frame(s)", timelapse.frames)
        return running

    def _run(
        self,
        timelapse: _Timelapse,
        *,
        context: Callable[[], CaptureContext],
        on_frame: Callable[[Capture], None],
        on_finish: Callable[[str], None],
    ) -> None:
        """The frame loop. Never raises — it is the whole of its thread."""
        failures = 0
        reason = "stopped by command"
        while not timelapse.stop.is_set():
            ctx = context()
            refused = refusal(ctx.state)
            if refused is not None:
                # The state descended under a running timelapse. The gate is
                # re-asked every frame rather than only at the start, because a
                # timelapse outlives the state it was started in.
                reason = refused
                break
            sequence = timelapse.frames + 1
            try:
                capture = self.capture(ctx, kind=KIND_TIMELAPSE, sequence=sequence)
            except StorageFull as exc:
                # Stop, rather than refuse every frame from here on: a loop that
                # fails forever is a log-flooding machine, and the card is not
                # going to get emptier by itself — PAYLOAD deletes nothing.
                reason = str(exc)
                break
            except Exception:
                failures += 1
                self.log.exception("timelapse frame %d failed", sequence)
                if failures >= MAX_CONSECUTIVE_FRAME_FAILURES:
                    reason = f"the camera failed {failures} frames in a row"
                    break
            else:
                timelapse.frames = sequence
                failures = 0
                try:
                    on_frame(capture)
                except Exception:
                    # Publishing failed, the frame is still on disk, and the run
                    # is worth continuing: the photos are the deliverable.
                    self.log.exception("publishing timelapse frame %d failed", sequence)
            timelapse.stop.wait(timelapse.interval_sec)
        # Set even when the loop ended on its own, so the reported state stops
        # claiming to be active the moment the last frame is behind us.
        timelapse.stop.set()
        # Recorded before on_finish, so the status that handler publishes
        # already carries the reason rather than the run before it.
        self._timelapse_reason = reason
        self.log.info("timelapse finished: %s", reason)
        try:
            on_finish(reason)
        except Exception:
            self.log.exception("timelapse finish handler failed")

    # ── shutdown ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Stop the timelapse and give the camera back, in that order."""
        self.stop_timelapse()
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        # Bumped so an idle timer already past cancel() finds a stale
        # generation instead of closing a second time on its own schedule.
        self._idle_generation += 1
        self._camera.close()


def _overlay_text(sidecar: dict[str, Any] | None) -> str | None:
    """A one-line rendering of the sidecar, handed to the driver.

    The driver files it rather than drawing it — see ``hal/rpi/camera.py`` — but
    it is passed down so a driver that *can* draw needs no new plumbing here.
    """
    if sidecar is None:
        return None
    parts = [sidecar["captured_at"], str(sidecar["mission_state"])]
    if sidecar["mission_id"] is not None:
        parts.append(f"mission {sidecar['mission_id']}")
    return " ".join(parts)
