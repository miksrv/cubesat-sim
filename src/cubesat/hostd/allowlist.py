"""What HOSTD may ever touch, decided once at startup.

HOSTD is the only privileged process in the project, and the allowlist is the
whole reason that is acceptable: a typo in ``profiles.yaml`` can then fail to
start a unit, but it cannot take down ``sshd``. Two rules do the work.

**The permitted set is closed.** It is built once, from the four mission
services' template instances, the dashboard, the units HOSTD owns outright, and
exactly the units ``external_units`` names — nothing computed later, nothing
inferred from a command. Anything else is refused before a process is spawned.

That makes this file the whole inventory of what root can ``systemctl`` in this
project, which is the second job the allowlist does: a safety property that
takes two files to verify is one people stop verifying. So ``avahi-daemon`` is
here too, even though ``network.py`` names it in a constant no profile can
redirect — it is a unit root touches, so it is listed where someone auditing
this file will see it.

**Four units are denied outright.** ``cubesat@obc``, ``cubesat-hostd`` and
``mosquitto`` are the branch HOSTD is sitting on: OBC is the only thing that
decides anything, HOSTD is the only thing that acts, and every control path in
this design runs through the broker. Stopping any of them strands the satellite
in whatever profile it was half-way through applying.

``NetworkManager`` is the same reason one step further out. Every network mode
this project has is applied through ``nmcli``, so a profile that stopped it
would take away the way back to a reachable one — and the profile where that
bites is ``FLIGHT``, which has neither Wi-Fi nor SSH to fix it from. Nothing in
``profiles.yaml`` names it today, but "nobody wrote it down" is a convention,
and this line is what makes it a property.

The deny is checked independently of how the permitted set was built, so a
future profile that names one of them changes nothing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from cubesat.common.profiles import KNOWN_SERVICES, ProfileConfig

logger = logging.getLogger("hostd.allowlist")

#: The separate units, outside the ``cubesat@`` template.
DASHBOARD_UNIT = "cubesat-dashboard.service"

#: Avahi, so ``cubesat.local`` resolves. Driven by a profile's
#: ``network.advertise_mdns``, never named by one: it is HOSTD's own lever, and
#: it is here so that every unit root can touch is listed in one place.
MDNS_UNIT = "avahi-daemon.service"

#: Units HOSTD manages on its own account rather than on a profile's. Permitted,
#: but outside the set a profile starts and stops — nothing may name them.
HOSTD_OWNED = frozenset({MDNS_UNIT})

#: Never, under any profile, for any reason. See the module docstring.
DENIED_UNITS = frozenset(
    {
        "cubesat@obc.service",
        "cubesat-hostd.service",
        "mosquitto.service",
        # Not ours, and denied for what it carries rather than for what it is:
        # every network mode goes through nmcli, so this is the way back.
        "NetworkManager.service",
    }
)

#: Units belonging to this repository. ``external_units`` is the registry of
#: things the project does *not* own, so a name matching this pattern there is a
#: mistake — and the one mistake that could reach ``cubesat@eps``, which is
#: always-on and outside profile control.
_OURS = re.compile(r"^cubesat[@-].*\.service$")


def _ours(unit: str) -> bool:
    """Whether ``unit`` is this project's business rather than a foreign one."""
    return bool(_OURS.match(unit)) or unit in HOSTD_OWNED


class Refused(Exception):
    """A unit outside the allowlist was asked for. No process was started."""


def unit_for(service: str) -> str:
    """The template instance for a mission service name (``adcs`` -> unit)."""
    return f"cubesat@{service}.service"


#: The four services a profile may ask for, as unit names. Derived from the
#: profile model so that adding a service is one line in ``common``, not two.
MISSION_UNITS = frozenset(unit_for(service) for service in KNOWN_SERVICES)


class Allowlist:
    """The closed set of units HOSTD may start, stop or interrogate."""

    def __init__(self, external_units: Iterable[str] = ()) -> None:
        external = set()
        for unit in external_units:
            if _ours(unit):
                # Refused at construction rather than at use: external_units is
                # for units this repo does not own, and letting one name
                # cubesat@eps.service would hand a config file the power to stop
                # the only source of battery telemetry. Avahi is excluded for the
                # same reason in the other direction — it is HOSTD's lever, not
                # a profile's unit.
                logger.error(
                    "ignoring external unit %s: external_units may only name foreign units", unit
                )
                continue
            if unit in DENIED_UNITS:
                # Subtracted from the permitted set below in any case. Logged
                # here as well because the alternative is a registry entry that
                # does nothing and says nothing about why.
                logger.error(
                    "ignoring external unit %s: HOSTD may never touch it — see DENIED_UNITS", unit
                )
                continue
            external.add(unit)
        #: Everything root may ``systemctl`` in this project.
        self._permitted = (
            MISSION_UNITS | {DASHBOARD_UNIT} | HOSTD_OWNED | external
        ) - DENIED_UNITS
        #: The subset a profile starts and stops. HOSTD's own units are managed
        #: by the module that owns them, and must not be swept up by "stop
        #: everything this profile did not ask for".
        self._profile_units = self._permitted - HOSTD_OWNED

    @classmethod
    def from_profiles(cls, profiles: ProfileConfig) -> Allowlist:
        return cls(profiles.unit_allowlist)

    @property
    def permitted(self) -> frozenset[str]:
        """Every unit HOSTD may touch at all — the audit inventory."""
        return frozenset(self._permitted)

    @property
    def profile_units(self) -> frozenset[str]:
        """The units a profile may ask for, and whose absence means "stop it"."""
        return frozenset(self._profile_units)

    def permits(self, unit: str) -> bool:
        # The deny is re-checked here, and not only subtracted when the set was
        # built, so that neither a future edit to the construction above nor a
        # profile that names one of the three can widen this.
        if unit in DENIED_UNITS:
            return False
        return unit in self._permitted

    def check(self, unit: str) -> None:
        """Raise ``Refused`` unless ``unit`` is permitted.

        Called *before* any argv is assembled, which is the only ordering that
        makes the allowlist a safety property rather than an audit log.
        """
        if self.permits(unit):
            return
        if unit in DENIED_UNITS:
            raise Refused(
                f"{unit} is never permitted: HOSTD would be sawing off the branch it sits on"
            )
        raise Refused(f"{unit} is not on the allowlist")
