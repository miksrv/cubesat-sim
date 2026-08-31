"""The DEPLOY self-test: is the hardware this profile asked for actually here?

Two checks, and a third thing that is deliberately not a check.

**The bus sweep** is a read-only presence probe, under the shared advisory lock,
for the addresses the active profile's services need — only those, so a profile
that runs no payload is not failed for a sensor it never asked for. EPS is
always in the sweep because EPS runs in every profile.

**The status wait** is the real evidence. A subsystem's own first status message
is proof that its hardware answered *to the process that owns it*, which is
better evidence than OBC probing devices it does not own: ADCS owns the IMU and
the GNSS receiver, PAYLOAD the environmental sensor and the camera, COMMS the
radio, and two readers on one 10 kHz bus is exactly the contention the bus lock
exists to prevent. So OBC reads nothing here beyond the presence check.

A **heartbeat will not do**, and this is the trap worth naming: every service in
this project is written to log a silent device and stay up — EPS does exactly
that when the fuel gauge stops answering — because OBC has to be able to see the
silence rather than a vanished process. So a heartbeat proves the process
started and says nothing at all about the sensor, and heartbeat-only evidence
would let DEPLOY pass in precisely the situation it exists for: a cable knocked
loose during re-assembly. Heartbeats are the health monitor's input, not this
module's.

**A GNSS fix is not a check.** It is waited for best-effort and its absence is
logged and nothing more. ``DEMO`` and ``EXPO`` run indoors, where a fix never
arrives; failing on it would send every indoor demonstration to ``SAFE``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import Enum

from cubesat.common import config
from cubesat.common.profiles import ProfileSpec
from cubesat.hal import i2c

#: How long a subsystem has to produce its first status message before DEPLOY
#: fails. Long enough for a service systemd has only just started to open its bus
#: and connect to the broker; short enough that a demonstration is not left
#: standing in front of an audience waiting for a verdict.
#:
#: Deliberately **below** the heartbeat-loss window
#: (``HEARTBEAT_INTERVAL_SEC * HEARTBEAT_MISS_THRESHOLD``, 30 s). Both fire on a
#: subsystem that never came up, and "the bring-up self-test failed" is the more
#: informative of the two reasons, so it has to land first by construction rather
#: than by the order in which OBC happens to evaluate them.
DEPLOY_TIMEOUT_SEC = 20.0

#: How often a missing address is re-probed while the window is open. Often
#: enough to pass a healthy bring-up promptly; seldom enough not to contend for
#: a 10 kHz bus the subsystems are reading their own devices over.
RESWEEP_INTERVAL_SEC = 1.0

#: The I2C addresses each service needs to find. Keyed by service so that the
#: sweep follows the profile rather than the address map: adding a profile must
#: not mean editing this table.
SERVICE_ADDRESSES: dict[str, tuple[int, ...]] = {
    "eps": (0x36,),
    "adcs": (0x28, 0x20),
    "payload": (0x22,),
}

#: For log messages. 0x68 (the DS1307 RTC) is deliberately absent: it is
#: kernel-owned, shows as UU in a bus scan, and must never be touched from user
#: space — probing it is how a working clock gets broken.
DEVICE_NAMES: dict[int, str] = {
    0x20: "TEL0157 GNSS",
    0x22: "SEN0501 environment",
    0x28: "BNO055 orientation",
    0x36: "MAX17048 fuel gauge",
}

#: The status topic whose first message counts as a service reporting in. DHS and
#: COMMS have no sensors of their own to probe, but a recorder or a radio that
#: never came up is still a failed bring-up in a profile that asked for one.
REPORT_TOPICS: dict[str, str] = {
    "adcs": "adcs_status",
    "payload": "payload_status",
    "dhs": "dhs_status",
    "comms": "comms_status",
}

#: Swept in every profile, because EPS runs in every profile and is therefore in
#: no profile's service list.
#:
#: Swept but **not** awaited: EPS has been up since boot, its cadence during
#: DEPLOY is longer than the window above, and the 0x36 probe already proves the
#: gauge answers. Its liveness is the health monitor's business, where it is
#: watched unconditionally.
ALWAYS_SWEPT: tuple[str, ...] = ("eps",)


class Outcome(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


def default_bus() -> i2c.I2CBus | None:
    """The bus to sweep, or None when there is no bus on this machine.

    Under ``CUBESAT_MOCK_HARDWARE`` the whole stack runs on a laptop against the
    mock HAL. There is no I2C bus there, and every address would read as absent
    — which would fail DEPLOY and put a perfectly healthy simulation in SAFE.
    """
    if config.MOCK_HARDWARE:
        return None
    return i2c.shared_bus()


class DeploySelfTest:
    """One bring-up attempt, for one profile."""

    def __init__(
        self,
        spec: ProfileSpec,
        *,
        bus: i2c.I2CBus | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEPLOY_TIMEOUT_SEC,
        log: logging.Logger | None = None,
    ) -> None:
        self.log = log or logging.getLogger("obc.deploy")
        self._bus = bus if bus is not None else default_bus()
        self._clock = clock
        self._timeout = timeout

        self.expected_addresses: tuple[int, ...] = tuple(
            sorted(
                {
                    addr
                    for svc in (*ALWAYS_SWEPT, *spec.services)
                    for addr in SERVICE_ADDRESSES.get(svc, ())
                }
            )
        )
        self.awaited_services: set[str] = {
            svc for svc in spec.services if svc in REPORT_TOPICS
        }

        self.missing_addresses: list[int] = []
        self.reported: set[str] = set()
        #: None until an ADCS position is seen at all. False means "seen, no fix".
        self.gnss_fix: bool | None = None
        self._deadline: float | None = None
        self._swept = False
        self._last_sweep: float | None = None

    # ── the two checks ──────────────────────────────────────────────────────

    def begin(self) -> None:
        """Sweep the bus and start the clock on the report wait.

        A miss here is **pending, not fatal**: the profile change has only just
        started the service that owns the device, and bring-up may have it
        mid-reset — the BNO055 stops ACKing its own address for ~650 ms during
        the soft reset ADCS opens with, which is exactly when this sweep hit it
        on the bench (2026-08-28). Missing addresses are re-probed once a
        second by ``evaluate`` and fail the self-test only when the window
        closes with them still silent.
        """
        self._deadline = self._clock() + self._timeout
        self._last_sweep = self._clock()
        self.missing_addresses = [
            addr for addr in self.expected_addresses if not self._present(addr)
        ]
        self._swept = True
        for addr in self.missing_addresses:
            self.log.warning(
                "DEPLOY: nothing answers at %#04x (%s) yet; will re-probe",
                addr,
                DEVICE_NAMES.get(addr, "unknown"),
            )
        self.log.info(
            "DEPLOY: swept %d address(es), waiting for %s",
            len(self.expected_addresses),
            ", ".join(sorted(self.awaited_services)) or "nothing",
        )

    def _present(self, address: int) -> bool:
        if self._bus is None:
            # Nothing to sweep; the report wait still has to be satisfied, and
            # that is the check that carries the real evidence anyway.
            return True
        return self._bus.present(address)

    def note_report(self, service: str) -> None:
        """A service has published its own status, so its hardware answered."""
        if service in self.awaited_services and service not in self.reported:
            self.reported.add(service)
            self.log.info("DEPLOY: %s reported in", service)

    def note_gnss(self, fix: bool) -> None:
        """Record whether the GNSS receiver has a fix. Never a pass/fail input."""
        self.gnss_fix = fix

    # ── the verdict ─────────────────────────────────────────────────────────

    @property
    def silent(self) -> tuple[str, ...]:
        return tuple(sorted(self.awaited_services - self.reported))

    @property
    def expired(self) -> bool:
        return self._deadline is not None and self._clock() >= self._deadline

    def evaluate(self) -> Outcome:
        """Where the bring-up stands. Safe to call as often as you like."""
        if not self._swept:
            return Outcome.PENDING
        if self.missing_addresses and not self.expired:
            self._resweep()
        if not self.missing_addresses and not self.silent:
            return Outcome.PASSED
        return Outcome.FAILED if self.expired else Outcome.PENDING

    def _resweep(self) -> None:
        """Re-probe the addresses still missing, at most once a second."""
        now = self._clock()
        if self._last_sweep is not None and now - self._last_sweep < RESWEEP_INTERVAL_SEC:
            return
        self._last_sweep = now
        still = [addr for addr in self.missing_addresses if not self._present(addr)]
        for addr in set(self.missing_addresses) - set(still):
            self.log.info(
                "DEPLOY: %#04x (%s) answered on re-probe",
                addr,
                DEVICE_NAMES.get(addr, "unknown"),
            )
        self.missing_addresses = still

    @property
    def failures(self) -> tuple[str, ...]:
        """Why DEPLOY failed, in words fit for a log line."""
        reasons = [
            f"{DEVICE_NAMES.get(addr, 'device')} at {addr:#04x} did not answer"
            for addr in self.missing_addresses
        ]
        reasons += [f"{svc} never reported" for svc in self.silent]
        return tuple(reasons)

    def log_gnss(self) -> None:
        """Say what became of the fix. Absence is information, not a fault."""
        if self.gnss_fix:
            self.log.info("DEPLOY: GNSS has a fix")
        else:
            # Indoors this is the normal outcome, which is exactly why it does
            # not fail the bring-up.
            self.log.info("DEPLOY: no GNSS fix yet; continuing without one")
