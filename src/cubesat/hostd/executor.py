"""The hands themselves: units, the CPU governor, poweroff — and the seam.

Every privileged thing HOSTD does happens here, behind a ``typing.Protocol``
with two implementations: one that really spawns ``systemctl`` and writes
``sysfs``, and one that records what it would have done. The tests run against
the second, so the suite needs no root and starts no processes.

Four rules this module exists to hold, all of them things a reviewer will ask
"why is root doing that?" about:

* **The allowlist is checked before an argv exists.** ``HostActions`` is the
  only place a unit name becomes a command, and the check is the first statement
  of each method. Filtering a result afterwards would mean the process already ran.
* **Never ``shell=True``, ever.** Explicit argv lists only, and no value from a
  config file or a command is interpolated into a command string. Today the AP's
  SSID comes from ``profiles.yaml``; tomorrow it could arrive from the ground.
* **Every call has a timeout.** A ``systemctl`` that never returns must not take
  the executor down with it, because the profile switch is how a satellite gets
  back to a state with SSH in it.
* **The real executor refuses to exist without root.** Running it unprivileged
  would mean every call failing while HOSTD cheerfully reported a profile as
  applied — the one outcome worse than not starting at all.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cubesat.common import config
from cubesat.common.profiles import VALID_GOVERNORS
from cubesat.hostd.allowlist import Allowlist

logger = logging.getLogger("hostd.executor")

#: Named only so the refusal below can tell an operator what to set. The flag
#: itself is read from ``config.MOCK_HOST``.
MOCK_HOST_ENV = "CUBESAT_MOCK_HOST"

#: Bound on any command. Generous, because a unit's ExecStartPre may be slow;
#: finite, because "hung forever" is not a state this service may be in.
DEFAULT_TIMEOUT_SEC = 30.0
#: A state query answers immediately or something is badly wrong.
STATE_TIMEOUT_SEC = 10.0

#: Returncodes for the two ways a command fails without running to completion.
#: 124 is ``timeout(1)``'s convention; 127 is "could not be executed".
TIMED_OUT = 124
UNAVAILABLE = 127

#: Unit states as ``systemctl is-active`` reports them, plus our own fallback
#: for "systemctl said nothing at all".
ACTIVE = "active"
INACTIVE = "inactive"
UNKNOWN = "unknown"

#: Where the kernel exposes the governor, one file per CPU. Written directly
#: rather than through ``cpupower``, which is not installed on Raspberry Pi OS.
CPU_ROOT = Path("/sys/devices/system/cpu")
GOVERNOR_GLOB = "cpu[0-9]*/cpufreq/scaling_governor"

#: Errors travel in a retained MQTT payload, so one verbose stderr must not
#: become a permanent multi-kilobyte message on the bus.
MAX_MESSAGE_CHARS = 200


class ExecutorError(RuntimeError):
    """A host action was attempted and did not succeed."""


class PrivilegeError(RuntimeError):
    """The real executor was asked for without root. A configuration error."""


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def message(self) -> str:
        """The shortest honest explanation of a failure."""
        text = self.stderr.strip() or self.stdout.strip() or f"exit {self.returncode}"
        return text[:MAX_MESSAGE_CHARS]


class Executor(Protocol):
    """The two privileged primitives. Everything else is built from them."""

    def run(self, argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Result:
        """Run a command. Never raises: a failure is a ``Result``, not an exception."""
        ...

    def write_governor(self, governor: str) -> tuple[Path, ...]:
        """Write ``governor`` to every CPU's ``scaling_governor``, return the paths."""
        ...


class SubprocessExecutor:
    """The real one. Spawns processes as root and writes to ``sysfs``."""

    def __init__(self, *, cpu_root: Path = CPU_ROOT) -> None:
        self._cpu_root = cpu_root

    def run(self, argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Result:
        command = tuple(str(part) for part in argv)
        try:
            # No shell, no interpolation, always a timeout. The three properties
            # that make a privileged subprocess reviewable.
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            return Result(command, TIMED_OUT, "", f"timed out after {timeout:.0f}s")
        except OSError as exc:
            # A missing binary is the ordinary case here: nmcli on a Pi without
            # NetworkManager, iw on a headless image. Reported, never raised.
            return Result(command, UNAVAILABLE, "", str(exc))
        return Result(command, completed.returncode, completed.stdout or "", completed.stderr or "")

    def write_governor(self, governor: str) -> tuple[Path, ...]:
        paths = sorted(self._cpu_root.glob(GOVERNOR_GLOB))
        if not paths:
            raise ExecutorError(f"no cpufreq governor files under {self._cpu_root}")
        for path in paths:
            try:
                path.write_text(governor)
            except OSError as exc:
                raise ExecutorError(f"writing {path}: {exc}") from exc
        return tuple(paths)


class RecordingExecutor:
    """The no-op one, selected by ``CUBESAT_MOCK_HOST=1``.

    It records and logs instead of acting, and answers state queries from a
    dictionary it keeps up to date as units are started and stopped — so the
    idempotence that matters on the satellite ("do not restart a healthy
    service") is observable here too, on a laptop, in the tests.
    """

    def __init__(self, states: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.governors: list[str] = []
        self.states: dict[str, str] = dict(states or {})

    def run(self, argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Result:
        command = tuple(str(part) for part in argv)
        self.calls.append(command)
        logger.info("mock host: would run %s", " ".join(command))
        return self._answer(command)

    def write_governor(self, governor: str) -> tuple[Path, ...]:
        self.governors.append(governor)
        logger.info("mock host: would set the CPU governor to %s", governor)
        return (CPU_ROOT / "cpu0/cpufreq/scaling_governor",)

    def _answer(self, command: tuple[str, ...]) -> Result:
        verb = command[1] if len(command) > 2 and command[0] == "systemctl" else None
        if verb == "is-active":
            state = self.states.get(command[2], INACTIVE)
            # is-active exits non-zero for anything but active; callers read the
            # word on stdout, exactly as they do against the real systemctl.
            return Result(command, 0 if state == ACTIVE else 3, f"{state}\n")
        if verb in ("start", "stop"):
            self.states[command[2]] = ACTIVE if verb == "start" else INACTIVE
        return Result(command, 0)


def select_executor(*, geteuid: Callable[[], int] = os.geteuid) -> Executor:
    """Pick the executor and say so loudly.

    Mistaking one for the other is the worst confusion available in this
    service — a mock that looks like it applied a profile, or a real one nobody
    expected to touch the host — so both branches log at WARNING, where a glance
    at ``journalctl -u cubesat-hostd`` cannot miss them.
    """
    if config.MOCK_HOST:
        logger.warning(
            "%s=1: MOCK executor active. Nothing will be started, stopped or reconfigured.",
            MOCK_HOST_ENV,
        )
        return RecordingExecutor()
    if geteuid() != 0:
        raise PrivilegeError(
            "hostd needs root to run systemctl, nmcli and poweroff, and refuses to "
            f"pretend otherwise. Run it as root (see systemd/cubesat-hostd.service), "
            f"or set {MOCK_HOST_ENV}=1 to run a no-op executor for development."
        )
    logger.warning("REAL executor active as root: systemctl, nmcli and poweroff are live.")
    return SubprocessExecutor()


class HostActions:
    """The fixed vocabulary of privileged actions, guarded by the allowlist."""

    def __init__(
        self, executor: Executor, allowlist: Allowlist, *, log: logging.Logger | None = None
    ) -> None:
        self._executor = executor
        self._allowlist = allowlist
        self.log = log or logger

    # ── units ───────────────────────────────────────────────────────────────

    def unit_state(self, unit: str) -> str:
        """``active`` / ``inactive`` / ``failed`` / ``unknown``, as systemd sees it."""
        self._allowlist.check(unit)
        result = self._executor.run(["systemctl", "is-active", unit], timeout=STATE_TIMEOUT_SEC)
        first = result.stdout.strip().splitlines()
        return first[0].strip() if first else UNKNOWN

    def start(self, unit: str) -> None:
        # Refused before an argv exists, not after a result comes back.
        self._allowlist.check(unit)
        self._unit_command("start", unit)

    def stop(self, unit: str) -> None:
        self._allowlist.check(unit)
        self._unit_command("stop", unit)

    def restart(self, unit: str) -> None:
        """One `systemctl restart`, through the same allowlist as everything else.

        ``restart`` rather than stop-then-start, so a unit that is already down
        comes back up: the case this exists for is a subsystem OBC has declared
        lost, and "lost" often means the process is gone rather than wedged.
        """
        self._allowlist.check(unit)
        self._unit_command("restart", unit)

    def _unit_command(self, verb: str, unit: str) -> None:
        result = self._executor.run(["systemctl", verb, unit])
        if not result.ok:
            raise ExecutorError(f"systemctl {verb} {unit}: {result.message}")
        self.log.info("systemctl %s %s", verb, unit)

    # ── power ───────────────────────────────────────────────────────────────

    def set_governor(self, governor: str) -> None:
        """Set the CPU frequency governor on every core.

        The name is validated against the same table ``profiles.py`` validates
        against, before it reaches the filesystem. That check is what makes it
        safe for this value to have come from a config file — or, one day, from
        a ground command.
        """
        if governor not in VALID_GOVERNORS:
            raise ExecutorError(f"refusing unknown CPU governor {governor!r}")
        paths = self._executor.write_governor(governor)
        self.log.info("CPU governor set to %s on %d core(s)", governor, len(paths))

    def poweroff(self, reason: str) -> None:
        """Shut the host down. Requested by OBC on CRITICAL, and nowhere else.

        ``reason`` is logged and never reaches the command line: there is exactly
        one way to power this host off, and it takes no arguments.
        """
        self.log.warning("powering off: %s", reason)
        result = self._executor.run(["systemctl", "poweroff"])
        if not result.ok:
            raise ExecutorError(f"systemctl poweroff: {result.message}")
