"""Wi-Fi: join a network, be a network, or be silent.

Its own module for two reasons. Bringing up an access point is the fiddliest
thing in this project, so it has to be testable apart from unit management; and
a network failure must be *reported*, never raised — a profile whose services
started but whose AP never came up is a real state, and the one OBC most needs
to see. Nothing here throws.

NetworkManager does the work, through ``nmcli`` with explicit argv. That is a
deliberate choice over hand-writing ``hostapd.conf`` and ``dnsmasq.conf``: one
connection profile carries the SSID, the key, the address plan and its own DHCP
server, and ``nmcli connection up`` starts all of it and ``down`` stops it. The
files it would have replaced are the kind that get edited on the satellite at a
science fair and never make it back into git.

**The AP is a named connection, not a hotspot command** (2026-09-04). It used to
be ``nmcli device wifi hotspot ... ssid cubesat``, which works — and which picks
the address (``10.42.0.1``) and generates the pre-shared key *itself*. Both then
lived only inside NetworkManager on the satellite, so joining the access point in
the field required first getting a shell on the satellite to read the password
off it, and the field is exactly where there is no other way in. Now
``scripts/install.sh`` creates one connection with a known SSID, key and address,
NetworkManager stores it root-only at 0600 where a password belongs, and a
profile names it. HOSTD raises a name and reports what came up.

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

#: What ``scripts/install.sh`` calls the access point's connection, and the
#: fallback for taking one down. A profile names its own in ``profiles.yaml``;
#: this is what "leave AP mode" reaches for when this process never raised one —
#: HOSTD restarted while the AP was up, which is a restart away at any time.
DEFAULT_AP_CONNECTION = "cubesat-ap"

#: The connection ``nmcli device wifi hotspot`` used to create, taken down beside
#: the one above when leaving AP mode. Only a satellite upgraded *while* an old
#: hotspot was up can still have it running; deleting this line costs nothing
#: once every deployment has been through one client-mode profile since
#: 2026-09-04, and until then it is two seconds of a command that usually fails
#: harmlessly.
LEGACY_HOTSPOT_CONNECTION = "Hotspot"

#: Reported when a mode was asked for and the decisive command failed. The
#: platform is then somewhere between two modes, and saying "ap" would be a lie
#: on the one topic that exists to be believed.
UNKNOWN_MODE = "unknown"

#: Radio changes are quick; associating with an AP or starting one is not.
RADIO_TIMEOUT_SEC = 15.0
HOTSPOT_TIMEOUT_SEC = 45.0
STATION_TIMEOUT_SEC = 10.0
#: Reading one field back out of a stored connection: local, no radio involved.
QUERY_TIMEOUT_SEC = 10.0


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
        #: The AP connection this process last raised, so that leaving AP mode
        #: takes down the one that is actually up rather than a name from a
        #: constant. None until an AP profile has been applied in this run.
        self._ap_connection: str | None = None

    def apply(self, spec: NetworkSpec) -> NetworkState:
        errors: list[str] = []
        ssid: str | None = None
        if spec.mode is NetworkMode.OFF:
            mode_ok = self._radio(False, errors)
        elif spec.mode is NetworkMode.AP:
            mode_ok = self._radio(True, errors)
            if mode_ok:
                mode_ok, ssid = self._raise_ap(spec.connection, errors)
        else:
            mode_ok = self._radio(True, errors)
            if mode_ok:
                self._leave_ap()

        # mDNS is reported as a failure of its own but does not make the *mode*
        # unknown: the radio can be exactly where it was asked to be while
        # avahi is missing from the image.
        self._mdns(spec.advertise_mdns, errors)

        clients = self.client_count() if spec.mode is NetworkMode.AP and mode_ok else None
        return NetworkState(
            mode=spec.mode.value if mode_ok else UNKNOWN_MODE,
            ssid=ssid,
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

    def _raise_ap(self, connection: str | None, errors: list[str]) -> tuple[bool, str | None]:
        """Raise the named connection; return whether it came up, and its SSID.

        Both together because they are one question asked of one connection, and
        because the SSID is *read back* rather than echoed from the profile:
        profiles.yaml names a connection and carries no SSID at all, so this is
        the only place the published name can come from — and it is the better
        source anyway. It says what is being broadcast, which is what an operator
        is typing into a phone, rather than what a file believes.
        """
        if not connection:
            # profiles.py already refuses an AP without a connection name; this
            # is the second half of that guarantee, at the point where the value
            # would otherwise reach a command line.
            errors.append("network mode 'ap' without a connection name")
            self.log.error("refusing to start an access point with no connection name")
            return False, None
        result = self._executor.run(
            ["nmcli", "connection", "up", connection, "ifname", self._interface],
            timeout=HOTSPOT_TIMEOUT_SEC,
        )
        if not result.ok:
            errors.append(f"nmcli connection up {connection}: {result.message}")
            # Naming the likeliest cause in the log rather than only the failure:
            # the connection is created by scripts/install.sh from the operator's
            # own .env, so a satellite that was upgraded by `git pull` alone has
            # a profile naming a connection NetworkManager has never heard of.
            self.log.error(
                "access point %r did not come up: %s "
                "(does the connection exist? nmcli connection show)",
                connection,
                result.message,
            )
            return False, None
        self._ap_connection = connection
        self.log.info("access point %r is up on %s", connection, self._interface)
        return True, self._ssid_of(connection)

    def _ssid_of(self, connection: str) -> str | None:
        """What that connection actually broadcasts, or None if it cannot be read.

        Withheld rather than guessed from the connection's own name: the two are
        different strings and need not resemble each other, and a name a visitor
        cannot find in a Wi-Fi list is worse than no name at all.
        """
        result = self._executor.run(
            ["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", connection],
            timeout=QUERY_TIMEOUT_SEC,
        )
        if not result.ok:
            self.log.info("could not read the SSID of %r: %s", connection, result.message)
            return None
        return result.stdout.strip() or None

    def _leave_ap(self) -> None:
        """Return to client mode by taking the access point down.

        Deliberately *not* ``nmcli device wifi connect``: the home network's
        credentials are not in ``profiles.yaml`` and must not be. NetworkManager
        already has them saved and reconnects on its own once the AP connection
        is out of the way — which is also what makes a power cycle a recovery
        path from ``FLIGHT``.

        Two names, because there are two ways one can be up: the one this process
        raised, or — after a HOSTD restart with the AP already running — the
        installed default. The legacy hotspot goes with them until every
        satellite has been through a client profile since 2026-09-04.

        A failure here is not reported: the ordinary case is that there was
        nothing to take down, because the previous profile was a client one too.
        Errors on ``host_status`` have to mean something.
        """
        names = [self._ap_connection or DEFAULT_AP_CONNECTION, LEGACY_HOTSPOT_CONNECTION]
        for name in names:
            result = self._executor.run(
                ["nmcli", "connection", "down", name], timeout=RADIO_TIMEOUT_SEC
            )
            if not result.ok:
                self.log.debug("no %s connection to take down (%s)", name, result.message)
        self._ap_connection = None

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
