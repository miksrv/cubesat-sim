"""Wi-Fi: join a network, be a network, or be silent.

Its own module for two reasons. Bringing up an access point is the fiddliest
thing in this project, so it has to be testable apart from unit management; and
a network failure must be *reported*, never raised — a profile whose services
started but whose AP never came up is a real state, and the one OBC most needs
to see. Nothing here throws.

NetworkManager does the work, through ``nmcli`` with explicit argv. That is a
deliberate choice over hand-writing ``hostapd.conf`` and ``dnsmasq.conf``:
``nmcli device wifi hotspot`` brings up the AP, its own DHCP server and the
address plan in one command, and tears all three down again just as cleanly. The
files it would have replaced are the kind that get edited on the satellite at a
science fair and never make it back into git.

``advertise_mdns`` is the Avahi daemon, which is what makes ``cubesat.local``
resolve — worth having in ``EXPO``, where the audience types a URL into a phone
that has never seen this network before. It is named by a constant in
``allowlist.py`` and checked against the allowlist like any other unit: no
profile can redirect it, but it is still a unit root touches, and the allowlist
is where the reader looks for those.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from cubesat.common.profiles import NetworkSpec
from cubesat.common.states import NetworkMode
from cubesat.hostd.allowlist import MDNS_UNIT, Allowlist, Refused
from cubesat.hostd.executor import Executor

logger = logging.getLogger("hostd.network")

#: The wireless interface. One radio on this hardware, so a constant with an
#: override rather than device enumeration nobody would ever exercise.
WIFI_INTERFACE = "wlan0"

#: The connection profile ``nmcli device wifi hotspot`` creates and reuses.
HOTSPOT_CONNECTION = "Hotspot"

#: Reported when a mode was asked for and the decisive command failed. The
#: platform is then somewhere between two modes, and saying "ap" would be a lie
#: on the one topic that exists to be believed.
UNKNOWN_MODE = "unknown"

#: Radio changes are quick; associating with an AP or starting one is not.
RADIO_TIMEOUT_SEC = 15.0
HOTSPOT_TIMEOUT_SEC = 45.0
STATION_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class NetworkState:
    """What the radio is actually doing, as far as we can tell."""

    mode: str
    ssid: str | None = None
    #: Associated stations in AP mode. ``None`` means "could not tell" — which
    #: is published as null rather than as a guessed zero.
    clients: int | None = None
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "ssid": self.ssid, "clients": self.clients}


class Network:
    """Applies a profile's ``network`` block. Reports; never raises."""

    def __init__(
        self,
        executor: Executor,
        allowlist: Allowlist,
        *,
        interface: str = WIFI_INTERFACE,
        log: logging.Logger | None = None,
    ) -> None:
        self._executor = executor
        self._allowlist = allowlist
        self._interface = interface
        self.log = log or logger

    def apply(self, spec: NetworkSpec) -> NetworkState:
        errors: list[str] = []
        if spec.mode is NetworkMode.OFF:
            mode_ok = self._radio(False, errors)
        elif spec.mode is NetworkMode.AP:
            mode_ok = self._radio(True, errors) and self._hotspot(spec.ssid, errors)
        else:
            mode_ok = self._radio(True, errors)
            if mode_ok:
                self._leave_hotspot()

        # mDNS is reported as a failure of its own but does not make the *mode*
        # unknown: the radio can be exactly where it was asked to be while
        # avahi is missing from the image.
        self._mdns(spec.advertise_mdns, errors)

        clients = self.client_count() if spec.mode is NetworkMode.AP and mode_ok else None
        return NetworkState(
            mode=spec.mode.value if mode_ok else UNKNOWN_MODE,
            ssid=spec.ssid,
            clients=clients,
            errors=tuple(errors),
        )

    def client_count(self) -> int | None:
        """Associated stations, best effort.

        ``iw`` is not on every image and says nothing useful when the interface
        is not an AP, so this returns ``None`` rather than a plausible zero.
        A dashboard showing "0 visitors" that means "we cannot tell" is worse
        than one showing nothing.
        """
        result = self._executor.run(
            ["iw", "dev", self._interface, "station", "dump"], timeout=STATION_TIMEOUT_SEC
        )
        if not result.ok:
            self.log.info("client count unavailable: %s", result.message)
            return None
        return sum(1 for line in result.stdout.splitlines() if line.startswith("Station "))

    # ── the individual levers ───────────────────────────────────────────────

    def _radio(self, on: bool, errors: list[str]) -> bool:
        state = "on" if on else "off"
        result = self._executor.run(["nmcli", "radio", "wifi", state], timeout=RADIO_TIMEOUT_SEC)
        if not result.ok:
            errors.append(f"nmcli radio wifi {state}: {result.message}")
            self.log.error("could not turn the radio %s: %s", state, result.message)
            return False
        self.log.info("radio %s", state)
        return True

    def _hotspot(self, ssid: str | None, errors: list[str]) -> bool:
        if not ssid:
            # profiles.py already refuses an AP without an SSID; this is the
            # second half of that guarantee, at the point where the value would
            # otherwise reach a command line.
            errors.append("network mode 'ap' without an ssid")
            self.log.error("refusing to start an access point with no ssid")
            return False
        result = self._executor.run(
            ["nmcli", "device", "wifi", "hotspot", "ifname", self._interface, "ssid", ssid],
            timeout=HOTSPOT_TIMEOUT_SEC,
        )
        if not result.ok:
            errors.append(f"nmcli device wifi hotspot: {result.message}")
            self.log.error("access point %r did not come up: %s", ssid, result.message)
            return False
        self.log.info("access point %r is up on %s", ssid, self._interface)
        return True

    def _leave_hotspot(self) -> None:
        """Return to client mode by taking the hotspot down.

        Deliberately *not* ``nmcli device wifi connect``: the home network's
        credentials are not in ``profiles.yaml`` and must not be. NetworkManager
        already has them saved and reconnects on its own once the hotspot
        connection is out of the way — which is also what makes a power cycle a
        recovery path from ``FLIGHT``.

        A failure here is not reported: the ordinary case is that there was no
        hotspot to take down, because the previous profile was a client one too.
        Errors on ``host_status`` have to mean something.
        """
        result = self._executor.run(
            ["nmcli", "connection", "down", HOTSPOT_CONNECTION], timeout=RADIO_TIMEOUT_SEC
        )
        if not result.ok:
            self.log.debug("no %s connection to take down (%s)", HOTSPOT_CONNECTION, result.message)

    def _mdns(self, enabled: bool, errors: list[str]) -> None:
        """Start or stop Avahi, so ``cubesat.local`` resolves — or does not."""
        verb = "start" if enabled else "stop"
        try:
            # Checked before the argv exists, exactly as unit management does it.
            self._allowlist.check(MDNS_UNIT)
        except Refused as exc:
            trouble: str | None = str(exc)
        else:
            result = self._executor.run(["systemctl", verb, MDNS_UNIT], timeout=RADIO_TIMEOUT_SEC)
            trouble = None if result.ok else result.message
        if trouble is None:
            self.log.info("mDNS %s (%s)", "advertised" if enabled else "off", MDNS_UNIT)
            return
        if not enabled:
            # Failing to stop a daemon that is not installed is the desired
            # state, and reporting it would mark every profile on an image
            # without avahi as partially applied — including the default one
            # applied at every boot.
            self.log.info("nothing to stop: %s (%s)", MDNS_UNIT, trouble)
            return
        errors.append(f"systemctl {verb} {MDNS_UNIT}: {trouble}")
        self.log.error("could not %s %s: %s", verb, MDNS_UNIT, trouble)
