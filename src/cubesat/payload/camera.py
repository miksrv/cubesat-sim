"""Capture policy: when a photo is allowed, where it goes, and how a mission's
own photography runs without either flooding the broker or outliving the service.

**Capture is gated on the mission state.** ``NOMINAL`` only; ``LOW_POWER`` and
below refuse with a reason. The camera is the most expensive
thing PAYLOAD can do — the sensor draws power, the encode costs CPU and the
result costs disk — and ``LOW_POWER`` exists precisely to stop discretionary
work. Stopping is permitted from *any* state, because stopping something is
never the dangerous direction: a gate that refused to stop a running series in
``SAFE`` would keep the camera running in the one state that most wanted it off.

**A mission photographs itself, on a cadence.** There is no ``start_timelapse``
command and no interval on the wire: while a mission is open, frames are taken
every ``photos.mission_interval_sec`` (300 by default), and they stop when the
mission does. Decided 2026-09-01, and the reasoning is the use case rather than
tidiness — a recorded mission is a trip, a trip wants pictures along the way,
and nobody was ever going to remember to send a command before walking out of
the house. What it replaces was a ground-commanded timelapse with an interval
somebody had to choose, whose only real use was the one now automatic.

**Photos are filed per mission**, under ``<data>/photos/<mission_id>/``, so a
gallery groups the way the charts do. PAYLOAD does not own missions — DHS does —
so the id arrives on ``dhs_status`` and is passed in here. The *root* arrives
with it, from the same message: the two databases number their missions
independently, so an id alone does not name a directory (see
``config.photos_root_for``). Id and root travel together as one pair through
``CaptureContext`` for the same reason every other field there does — a series
spans minutes, and a frame filed under this mission's id in the previous
mission's root would be filed under a trip it was not part of.

**With no mission open, a photo is never written to the card.** It goes to
``PHOTO_SCRATCH_DIR`` — a directory on ``/run``, which is a tmpfs, so the file
exists in RAM only — is published as pixels, and is deleted. That is the whole
of what ``DEMO`` and ``EXPO`` need: a photograph is taken because somebody asked
for it, they see it, and there is no history worth keeping of a satellite that
was standing on a desk. It replaces ``photos/unfiled/``, which retention was
never allowed to touch and which therefore only ever grew — the one directory on
the card guaranteed to accumulate was the one holding the photographs nobody
would come back for.

**A mission frame publishes metadata, not pixels.** A ``take_photo`` puts the
base64 JPEG on ``cubesat/payload/photo``, because that is how the dashboard
receives it. A mission frame publishes only its path, size, sequence number and
mission: a hundred frames at a few hundred kilobytes each is tens of megabytes
pushed through a broker that is simultaneously carrying the telemetry the
satellite exists to collect — on a Pi, over Wi-Fi, sharing a mesh radio's
uplink. Those frames are on the card, filed under their mission, and that is
where a gallery reads them from. The ``kind`` field on every ``payload_photo``
message says which of the two a consumer is looking at, so nothing has to infer
it from the presence of a base64 blob.

**The camera is the only unbounded writer on this satellite**, so it is the one
that has to watch the card. Below ``photos.min_free_mb`` a capture is refused
and a running series stops itself, because the next write to fail on a full card
is the telemetry row the mission exists to record — and the recorder tidying up
later is no comfort when the card is full now. PAYLOAD deletes nothing on the
card: retention is DHS's job, and a service that writes files and also decides
which ones to remove is one bug away from removing the wrong ones. (The scratch
frame above is not on the card and is deleted by the service that published it,
which is a different thing from a retention policy.) The science tick is
deliberately not gated on free space either: that reading is small, bounded, and
the thing most likely to explain what went wrong.

**One capture at a time.** The camera is a single exclusive resource and there
are two callers — a ground command arriving on the MQTT thread and the frame
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
from cubesat.common.topics import KIND_MISSION, KIND_PHOTO
from cubesat.hal.interfaces import Camera, Photo

#: Free space is reported and compared in mebibytes, which is what
#: ``photos.min_free_mb`` means and what ``df -m`` prints.
BYTES_PER_MB = 1024 * 1024

#: How many frames in a row may fail before the series gives up. One failure is
#: a hiccup worth riding out over a long run; three in a row is a camera that has
#: gone away, and continuing would fill the log for hours.
MAX_CONSECUTIVE_FRAME_FAILURES = 3

#: How long ``stop_mission_photos`` waits for the frame thread. Longer than one
#: capture, so a stop issued mid-frame still returns having actually stopped.
FRAME_THREAD_JOIN_TIMEOUT_SEC = 10.0

#: The floor under ``photos.mission_interval_sec``. A capture takes the better
#: part of a second, so anything below this is not a faster series — it is a
#: camera running flat out with a queue behind it. A module constant rather than
#: a configuration key: the interval is meant to be tuned, this is meant to stop
#: a typo in it, and a floor somebody can lower is not a floor.
MIN_MISSION_INTERVAL_SEC = 1.0


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

    Passed in rather than reached for, because a series spans minutes: the
    mission can change, the state can change and the satellite can move between
    one frame and the next, and every frame should record the truth of its own
    moment.
    """

    mission_id: int | None = None
    #: Which of the photo roots this mission's frames are filed under, from the
    #: same ``dhs_status`` the id came from. None means the caller names none
    #: and the controller's own root applies — which is every case where there
    #: is no mission, and the photograph is going to the scratch tmpfs anyway.
    photos_root: Path | None = None
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
    #: 1-based, and only for mission frames.
    sequence: int | None = None

    @property
    def size_bytes(self) -> int:
        return self.photo.path.stat().st_size


@dataclass
class MissionPhotoState:
    """What the retained ``payload_status`` says about the mission's photography."""

    active: bool = False
    interval_sec: float | None = None
    #: Frames actually on disk, not frames attempted.
    frames: int = 0
    #: Why the last run ended, or None if none has ever been started. What makes
    #: "stopped because the card is nearly full" distinguishable from "no mission
    #: has been open yet", which is the difference a science fair needs.
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "interval_sec": self.interval_sec,
            "frames": self.frames,
            "reason": self.reason,
        }


class _PhotoRun:
    """One mission's photography: its thread, its stop flag and its counter.

    Not a dataclass, because the thread has to be handed a reference to the run
    it belongs to and that is circular at construction time.
    """

    def __init__(self, interval_sec: float, target: Callable[[_PhotoRun], None]) -> None:
        self.interval_sec = interval_sec
        self.stop = threading.Event()
        #: Frames written. Bumped only after a capture succeeds, so a retried
        #: sequence number reuses its slot and the count never claims a frame
        #: the card refused.
        self.frames = 0
        self.thread = threading.Thread(
            target=target,
            args=(self,),
            name="mission-photos",
            # Daemon so a hung capture cannot keep the process alive past
            # SIGTERM; close() still joins it for an orderly stop first.
            daemon=True,
        )


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CameraController:
    """PAYLOAD's side of the camera: policy, filing and the frame thread."""

    def __init__(
        self,
        camera: Camera,
        *,
        photos_dir: Path | None = None,
        scratch_dir: Path | None = None,
        log: logging.Logger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._camera = camera
        #: The mission database's photo root: where a frame goes when its caller
        #: names no other, and the directory whose filesystem the free-space
        #: floor is measured on. Both roots are under the data directory, so
        #: which one is measured makes no difference to the answer.
        self._root = photos_dir if photos_dir is not None else config.PHOTOS_DIR
        #: Where a photo goes when no mission is open. On the satellite this is
        #: under /run, which is a tmpfs: the frame never touches the card.
        self._scratch = scratch_dir if scratch_dir is not None else config.PHOTO_SCRATCH_DIR
        self.log = log or logging.getLogger("payload.camera")
        self._clock = clock
        self._capture_lock = threading.Lock()
        self._run_state: _PhotoRun | None = None
        #: Why the last run ended. None means none was ever started.
        self._photo_reason: str | None = None
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
        of writing rather than the moment of asking. A series crossing the
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

    def path_for(self, mission_id: int | None, photos_root: Path | None = None) -> Path:
        """Where this photo goes. Creates nothing — see below.

        With a mission, this is the one place the id becomes a string: the
        directory name is the filesystem's representation of it, while everywhere
        else — the wire, the sidecar, the logs — carries the integer DHS
        reported. Retention's photo-directory rule ("a run of digits") is matched
        by exactly this rendering, and it stays matched because ``photos_root``
        selects a *sibling* root rather than adding a level above the id.

        ``photos_root`` is which database's root — a mission id alone does not
        name a directory, because both databases issue ids from 1. It is
        ``config.photos_root_for`` applied to what DHS reported, and a caller
        that names none gets the mission database's root, which is where every
        photograph filed before there was a second database already is.

        With no mission, it is the scratch directory on the tmpfs, and the
        photograph is never written to the card at all. See the module docstring:
        that is the ``DEMO``/``EXPO`` case, where the pixels go to whoever asked
        and no history is kept.
        """
        if mission_id is None:
            return self._scratch
        return (photos_root if photos_root is not None else self._root) / str(mission_id)

    def directory_for(self, mission_id: int | None, photos_root: Path | None = None) -> Path:
        """The same directory, brought into existence.

        Separate from ``path_for`` because publishing a status must not create
        directories: only an actual capture should, or a satellite that never
        takes a photo still grows the folders.

        The state and runtime directories themselves belong to
        ``config/tmpfiles.d/cubesat.conf`` and nothing here creates them — the
        scratch directory included. A mission's subdirectory is the exception
        that has to be made here: its name does not exist until DHS opens the
        mission, so no unit file and no tmpfiles entry can name it in advance.
        """
        directory = self.path_for(mission_id, photos_root)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def discard(self, path: Path) -> None:
        """Delete a scratch frame once its pixels have been published.

        Called by PAYLOAD rather than done inside ``capture`` because the frame
        has to survive long enough to be read and base64-encoded, and the code
        that publishes it is the code that knows when that is done.

        Deliberately narrow: it refuses anything outside the scratch directory,
        so the one method in this class that deletes a file cannot be handed a
        mission's frame by a future caller who did not read the docstring.
        PAYLOAD deletes nothing on the card — that is retention's job.
        """
        if path.parent != self._scratch:
            self.log.error("refusing to delete %s: not a scratch frame", path)
            return
        for target in (path, path.with_suffix(".json")):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                # A frame left behind in a tmpfs costs a few hundred kilobytes
                # of RAM until the next reboot; failing the photograph over it
                # would cost the photograph.
                self.log.exception("could not remove %s", target)

    def _filename(self, taken_at: float, kind: str, sequence: int | None) -> str:
        stamp = datetime.fromtimestamp(taken_at, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        if kind == KIND_MISSION:
            # The sequence number is in the name because two frames inside the
            # same second would otherwise collide, and because a directory
            # listing should show the order the frames were taken in.
            return f"frame_{stamp}_{sequence:04d}.jpg"
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
            directory = self.directory_for(context.mission_id, context.photos_root)
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
        series faster than the window never pays it, and a slower one trades
        a second per frame for a camera that is cold between frames.

        Read from config at each call, like the mission interval: a bench
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
        # Daemon for the same reason the frame thread is: a pending close
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

    # ── the mission's own photography ───────────────────────────────────────

    @property
    def mission_photos(self) -> MissionPhotoState:
        active = self._run_state
        if active is None:
            return MissionPhotoState(reason=self._photo_reason)
        return MissionPhotoState(
            active=not active.stop.is_set(),
            interval_sec=active.interval_sec,
            frames=active.frames,
            # None while it is still running: a reason is why it ended.
            reason=None if not active.stop.is_set() else self._photo_reason,
        )

    def start_mission_photos(
        self,
        *,
        context: Callable[[], CaptureContext],
        on_frame: Callable[[Capture], None],
        on_finish: Callable[[str], None],
    ) -> float:
        """Start the frame thread and return the interval it actually uses.

        The interval comes from configuration (``photos.mission_interval_sec``)
        and is read here rather than captured at import, so an operator who edits
        it and restarts PAYLOAD gets the new value without a code change. There
        is deliberately no parameter: the interval is a property of how this
        satellite photographs a trip, not of an individual request, and the whole
        point of the 2026-09-01 change was that nobody has to choose one.

        ``context`` is a callable, not a value: the mission, the state and the
        position all change over a run measured in hours, and each frame asks
        again rather than recording the situation at the moment it started.
        """
        requested = config.PHOTO_MISSION_INTERVAL_SEC
        interval = max(MIN_MISSION_INTERVAL_SEC, float(requested))
        if interval != requested:
            self.log.warning(
                "photos.mission_interval_sec is %.3fs, below the %.1fs floor; using %.1fs",
                requested,
                MIN_MISSION_INTERVAL_SEC,
                interval,
            )
        run = _PhotoRun(
            interval,
            functools.partial(
                self._run, context=context, on_frame=on_frame, on_finish=on_finish
            ),
        )
        self._run_state = run
        self._photo_reason = None
        run.thread.start()
        self.log.info("mission photography started at %.1fs intervals", interval)
        return interval

    def stop_mission_photos(self) -> bool:
        """Stop the frame thread if one is running. True if one still was.

        Permitted from every mission state — see the module docstring. False
        covers both "there was never one" and "it already ended itself", because
        to a caller deciding whether anything changed those are the same answer.
        """
        run, self._run_state = self._run_state, None
        if run is None:
            return False
        running = not run.stop.is_set()
        run.stop.set()
        if run.thread is not threading.current_thread():
            # Not when the frame thread is stopping itself, which would be a
            # thread joining itself and is an error rather than a wait.
            run.thread.join(timeout=FRAME_THREAD_JOIN_TIMEOUT_SEC)
        if running:
            self.log.info("mission photography stopped after %d frame(s)", run.frames)
        return running

    def _run(
        self,
        run: _PhotoRun,
        *,
        context: Callable[[], CaptureContext],
        on_frame: Callable[[Capture], None],
        on_finish: Callable[[str], None],
    ) -> None:
        """The frame loop. Never raises — it is the whole of its thread."""
        failures = 0
        reason = "the mission closed"
        while not run.stop.is_set():
            ctx = context()
            refused = refusal(ctx.state)
            if refused is not None:
                # The state descended under a running series. The gate is
                # re-asked every frame rather than only at the start, because a
                # mission outlives the state it opened in.
                reason = refused
                break
            sequence = run.frames + 1
            try:
                capture = self.capture(ctx, kind=KIND_MISSION, sequence=sequence)
            except StorageFull as exc:
                # Stop, rather than refuse every frame from here on: a loop that
                # fails forever is a log-flooding machine, and the card is not
                # going to get emptier by itself — PAYLOAD deletes nothing.
                reason = str(exc)
                break
            except Exception:
                failures += 1
                self.log.exception("mission frame %d failed", sequence)
                if failures >= MAX_CONSECUTIVE_FRAME_FAILURES:
                    reason = f"the camera failed {failures} frames in a row"
                    break
            else:
                run.frames = sequence
                failures = 0
                try:
                    on_frame(capture)
                except Exception:
                    # Publishing failed, the frame is still on disk, and the run
                    # is worth continuing: the photos are the deliverable.
                    self.log.exception("publishing mission frame %d failed", sequence)
            run.stop.wait(run.interval_sec)
        # Set even when the loop ended on its own, so the reported state stops
        # claiming to be active the moment the last frame is behind us.
        run.stop.set()
        # Recorded before on_finish, so the status that handler publishes
        # already carries the reason rather than the run before it.
        self._photo_reason = reason
        self.log.info("mission photography finished: %s", reason)
        try:
            on_finish(reason)
        except Exception:
            self.log.exception("mission photography finish handler failed")

    # ── shutdown ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Stop the frame thread and give the camera back, in that order."""
        self.stop_mission_photos()
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
