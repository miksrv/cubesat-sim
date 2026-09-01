"""Raspberry Pi Camera Module V2 (Sony IMX219) over Picamera2.

The camera is the one piece of hardware in this project with a *lifetime*: the
sensor is a single exclusive resource, opening it takes the better part of a
second, and a second ``Picamera2()`` in the same process is an error rather than
a second camera. So this driver opens once, on the first use, keeps the handle,
and gives it back in ``close()``. PAYLOAD is its only owner, and PAYLOAD is the
only service that may hold it — the ``video`` group membership in the unit file
is what that ownership looks like from outside.

``picamera2`` and ``libcamera`` are imported lazily, exactly as ``hal/i2c.py``
imports ``smbus2``: they install on a Raspberry Pi and nowhere else, and merely
importing this module — which the registry does on any machine — must not need
them.

**Nothing is drawn onto the pixels.** ``overlay`` is part of the ``Camera``
protocol and is accepted here, but this driver deliberately does not burn text
into the image. There is no imaging library in this project and adding one for
captioning would be a dependency bought for decoration; more to the point, a
science image with text stamped across it has been defaced — the caption cannot
be removed, and the pixels it covers are gone. PAYLOAD writes the same
information to a sidecar JSON file beside the photo instead, where it can be
read by a dashboard, indexed by a database and ignored by an image processor.
The text is still passed down here so that a driver which *does* draw — a future
model with a hardware overlay, say — needs no new plumbing.

Verified versus inferred
------------------------

The sensor's 3280 × 2464 maximum, the low-resolution preview stream, its YUV420
format and the 180° flip are all from ``docs/hardware-camera-module-v2.md``: the
flip is not a preference but a correction for how the module sits in the printed
frame (``hardware/models/frame/Cubesat_RaspbiCam_Frame.stl``), so an un-flipped
image is upside down.

The still size itself is a **setting** (``camera.resolution``), not a constant,
and it defaults well below the sensor maximum. A long mission is the case that
decides it — full resolution is 24 MB a frame off a bus and onto an SD card —
and that is a number somebody should be able to lower without a deploy.

Not in those notes: that the *achieved* still size equals the requested one.
Picamera2 may adjust a request to something the sensor supports, and the
``Photo`` dimensions below are the size that was asked for. Reading the size
back out of the live configuration would be more truthful, but the attribute
that holds it is not in our documentation, and guessing at a library API in
order to look precise is how a plausible wrong number gets published.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from cubesat.common import config
from cubesat.hal.interfaces import Photo

logger = logging.getLogger(__name__)

#: The sensor's maximum, for reference: 8 MP, 3280 × 2464. What is actually
#: captured comes from ``config.PHOTO_RESOLUTION``.
SENSOR_MAX_SIZE = (3280, 2464)
#: A low-resolution stream alongside the still. Picamera2 wants somewhere to run
#: the auto-exposure and auto-white-balance loops; without it the first frame
#: after a start is metered from nothing.
PREVIEW_SIZE = (640, 480)
PREVIEW_FORMAT = "YUV420"

#: 180°, because of how the module is mounted in the frame — see above.
FLIP_HORIZONTAL = 1
FLIP_VERTICAL = 1


class CameraError(OSError):
    """The camera could not be opened or a capture failed."""


class PiCamera:
    """One camera, opened once and closed once."""

    def __init__(
        self,
        still_size: tuple[int, int] | None = None,
        preview_size: tuple[int, int] = PREVIEW_SIZE,
    ) -> None:
        # Resolved here rather than in the signature so the configured value is
        # read when the driver is built, not when this module is imported.
        self._still_size = still_size if still_size is not None else config.PHOTO_RESOLUTION
        self._preview_size = preview_size
        #: The Picamera2 handle. Untyped because picamera2 ships no stubs and
        #: only installs on a Pi; the protocol this driver satisfies is what is
        #: actually checked.
        self._camera: Any = None

    # ── lifetime ────────────────────────────────────────────────────────────

    def _open(self) -> Any:
        """The live Picamera2 handle, opening and configuring it on first use.

        Deferred rather than done in ``__init__`` so that constructing the
        driver — which the registry does before anything has decided to take a
        photo — costs nothing and cannot fail. A profile that starts PAYLOAD for
        its environmental sensor alone never touches the camera at all.
        """
        if self._camera is not None:
            return self._camera
        try:
            from libcamera import Transform
            from picamera2 import Picamera2
        except ImportError as exc:
            raise CameraError(
                "picamera2 is not installed, so there is no camera here. "
                "Set CUBESAT_MOCK_HARDWARE=1 to run without hardware."
            ) from exc

        camera = Picamera2()
        camera.configure(
            camera.create_still_configuration(
                main={"size": self._still_size},
                lores={"size": self._preview_size, "format": PREVIEW_FORMAT},
                transform=Transform(hflip=FLIP_HORIZONTAL, vflip=FLIP_VERTICAL),
            )
        )
        camera.start()
        self._camera = camera
        logger.info("camera opened at %dx%d", *self._still_size)
        return camera

    def close(self) -> None:
        """Give the sensor back. Idempotent, because shutdown paths overlap."""
        if self._camera is None:
            return
        camera, self._camera = self._camera, None
        camera.stop()
        camera.close()
        logger.info("camera closed")

    # ── the Camera protocol ─────────────────────────────────────────────────

    def probe(self) -> bool:
        """Whether the camera opens at all. The DEPLOY self-test asks this.

        Broad by intent: Picamera2 raises its own exception types for a missing
        ribbon cable, a camera already claimed by another process and a
        misconfigured stream, and none of them is in our documentation. What
        DEPLOY needs to know is "yes" or "no", and every one of those is a no.
        """
        try:
            self._open()
        except Exception as exc:
            logger.error("camera did not open: %s", exc)
            return False
        return True

    def capture(self, path: Path, *, overlay: str | None = None) -> Photo:
        """Save one JPEG to ``path``.

        A request object rather than ``capture_file`` so the buffer is released
        explicitly: the still stream at full resolution is 24 MB per frame, and
        a mission that leaks one of those per capture exhausts CMA memory long
        before it exhausts the SD card.
        """
        if overlay is not None:
            # Said at debug because it is the answer to "why is there no text on
            # my photo?", and that question is asked at a bench, not in flight.
            logger.debug("overlay text is filed beside the photo, not drawn on it: %s", overlay)
        camera = self._open()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Stamped before the shutter rather than after: the capture itself takes
        # a moment, and the time the photo is *of* is the time it started.
        taken_at = time.time()
        request = camera.capture_request()
        try:
            request.save("main", str(path))
        finally:
            request.release()
        width, height = self._still_size
        return Photo(path=path, width=width, height=height, taken_at=taken_at)
