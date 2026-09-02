"""Loading and validating ``config/profiles.yaml``.

Profiles are data, not code: adding one must not require editing OBC. That only
holds if the loader validates hard, so a malformed profile fails at load with a
clear message instead of half-applying at 2 a.m. in a backpack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cubesat.common import config
from cubesat.common.states import MissionMode, NetworkMode, Persistence, Profile

#: Mission services a profile may ask for. Not a free-form list: OBC and EPS are
#: always-on and outside profile control, and HOSTD refuses anything else.
KNOWN_SERVICES = frozenset({"adcs", "payload", "dhs", "comms"})

VALID_GOVERNORS = frozenset({"ondemand", "powersave", "performance", "conservative"})


class ProfileError(ValueError):
    """A profile definition is malformed or names something unknown."""


@dataclass(frozen=True)
class NetworkSpec:
    mode: NetworkMode
    ssid: str | None = None
    advertise_mdns: bool = False


@dataclass(frozen=True)
class PowerSpec:
    governor: str = "ondemand"
    #: Multiplies every poll interval. Below 1 polls faster (DIAG uses 0.2).
    cadence_scale: float = 1.0


@dataclass(frozen=True)
class DownlinkSpec:
    """Which outbound channels a profile permits, and how it starts out.

    It stays a block rather than a bare ``lora`` flag on the profile: there was a
    cloud API here until the ground segment was rebuilt as an interface over the
    satellite's own dashboard, and a second channel is a plausible thing to want
    again.

    ``lora`` and ``beacon`` are two different statements, and conflating them is
    the mistake this pair exists to prevent:

    * ``lora`` is the **envelope** — whether the radio is part of the mission at
      all. False means the inbox is not even polled, and no ground command can
      widen it.
    * ``beacon`` is the **starting state of transmission inside that envelope**,
      applied on entering the profile. False means the satellite listens but says
      nothing until somebody asks it to — over the radio, over SSH, or from the
      dashboard, all of which end at the same ``set_comms_config``.

    Added 2026-09-01 for the desk case: in ``DEMO`` and ``EXPO`` the satellite is
    a metre from its operator and beaconing at them over a shared mesh channel is
    noise, while still hearing an uplinked ``set_profile`` is exactly what makes
    the radio worth having there.
    """

    lora: bool = False
    beacon: bool = True


@dataclass(frozen=True)
class ExternalUnit:
    unit: str
    requires_internet: bool = False


@dataclass(frozen=True)
class ProfileSpec:
    name: Profile
    mission: MissionMode
    network: NetworkSpec
    #: The foreign units this profile wants running, already resolved from
    #: the ``start``/``stop`` shorthands. Everything else HOSTD may touch is
    #: stopped, so this list is read as the whole intent, never as a delta.
    external_units: tuple[str, ...]
    services: tuple[str, ...]
    dashboard: bool
    persistence: Persistence
    downlink: DownlinkSpec
    power: PowerSpec
    ttl_minutes: int | None = None

    @property
    def records(self) -> bool:
        """Whether this profile permits writing telemetry at all.

        Persistence is a property of the profile; how *often* rows are written
        is a property of the mission state. The pre-rewrite code gated writes on
        a SCIENCE state entered by ground command, which meant FLIGHT recorded
        nothing unless someone remembered to send that command before leaving
        the house. The gate went first, and the state itself followed once it
        was clear nothing else had ever depended on it.
        """
        return self.persistence is not Persistence.NONE


@dataclass(frozen=True)
class ProfileConfig:
    default: Profile
    profiles: dict[Profile, ProfileSpec]
    external_units: tuple[ExternalUnit, ...] = field(default=())

    def get(self, name: Profile | str) -> ProfileSpec:
        try:
            key = Profile(name)
        except ValueError as exc:
            raise ProfileError(f"unknown profile {name!r}") from exc
        if key not in self.profiles:
            raise ProfileError(f"profile {key.value} is not defined in profiles.yaml")
        return self.profiles[key]

    @property
    def unit_allowlist(self) -> frozenset[str]:
        """Every external unit HOSTD is permitted to touch."""
        return frozenset(u.unit for u in self.external_units)


def _external_units(name: str, raw: Any, known: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve a profile's ``external_units`` to the units it wants running.

    Three spellings, one meaning: ``start`` is every unit in the registry,
    ``stop`` is none of them, and a list is exactly the ones named. The
    shorthands are resolved *here* rather than in HOSTD, so what reaches the
    privileged process is already a plain set of unit names — there is no verb
    left for it to interpret, and no second place where "all of them" could
    drift into meaning something else.

    A name absent from the registry is refused rather than ignored. The registry
    is the allowlist: HOSTD would decline to start such a unit anyway, and a
    typo that fails at load beats one that silently never starts a service.
    """
    if isinstance(raw, str):
        if raw == "start":
            return known
        if raw == "stop":
            return ()
        raise ProfileError(
            f"profile {name}: external_units must be 'start', 'stop', or a list of units"
        )
    if not isinstance(raw, list):
        raise ProfileError(
            f"profile {name}: external_units must be 'start', 'stop', or a list of units"
        )
    wanted = tuple(dict.fromkeys(str(unit) for unit in raw))
    unknown = sorted(set(wanted) - set(known))
    if unknown:
        raise ProfileError(
            f"profile {name}: external_units names {unknown}, "
            "which is not declared in the external_units registry"
        )
    return wanted


def _spec(name: str, raw: dict[str, Any], known_units: tuple[str, ...]) -> ProfileSpec:
    try:
        profile = Profile(name)
    except ValueError as exc:
        raise ProfileError(f"unknown profile name {name!r} in profiles.yaml") from exc

    def need(key: str) -> Any:
        if key not in raw:
            raise ProfileError(f"profile {name}: missing required key {key!r}")
        return raw[key]

    net_raw = need("network")
    try:
        network = NetworkSpec(
            mode=NetworkMode(net_raw.get("mode", "off")),
            ssid=net_raw.get("ssid"),
            advertise_mdns=bool(net_raw.get("advertise_mdns", False)),
        )
    except ValueError as exc:
        raise ProfileError(f"profile {name}: bad network mode {net_raw.get('mode')!r}") from exc

    if network.mode is NetworkMode.AP and not network.ssid:
        raise ProfileError(f"profile {name}: network mode 'ap' requires an ssid")

    external = _external_units(name, need("external_units"), known_units)

    services = tuple(need("services"))
    unknown = set(services) - KNOWN_SERVICES
    if unknown:
        raise ProfileError(f"profile {name}: unknown services {sorted(unknown)}")

    try:
        mission = MissionMode(need("mission"))
        persistence = Persistence(need("persistence"))
    except ValueError as exc:
        raise ProfileError(f"profile {name}: {exc}") from exc

    power_raw = raw.get("power", {})
    governor = power_raw.get("governor", "ondemand")
    if governor not in VALID_GOVERNORS:
        raise ProfileError(f"profile {name}: unknown CPU governor {governor!r}")
    scale = float(power_raw.get("cadence_scale", 1.0))
    if scale <= 0:
        raise ProfileError(f"profile {name}: cadence_scale must be positive")

    down_raw = raw.get("downlink", {})
    ttl = raw.get("ttl_minutes")
    if ttl is not None:
        ttl = int(ttl)
        if ttl <= 0:
            raise ProfileError(f"profile {name}: ttl_minutes must be positive or null")

    # A profile that runs no mission cannot meaningfully record or serve a
    # dashboard; catching it here beats debugging an empty database later.
    if mission is not MissionMode.ACTIVE and persistence is not Persistence.NONE:
        raise ProfileError(f"profile {name}: persistence requires mission 'active'")

    return ProfileSpec(
        name=profile,
        mission=mission,
        network=network,
        external_units=external,
        services=services,
        dashboard=bool(raw.get("dashboard", False)),
        persistence=persistence,
        downlink=DownlinkSpec(
            lora=bool(down_raw.get("lora", False)),
            # Defaults true: a profile that permits the radio and says nothing
            # about the beacon means "transmit as the beacon table allows",
            # which is what every profile meant before this field existed.
            beacon=bool(down_raw.get("beacon", True)),
        ),
        power=PowerSpec(governor=governor, cadence_scale=scale),
        ttl_minutes=ttl,
    )


def load(path: Path | None = None) -> ProfileConfig:
    """Load and validate the profile configuration."""
    target = path or config.PROFILES_FILE
    if not target.exists():
        raise ProfileError(f"profiles file not found: {target}")
    with target.open() as fh:
        raw = yaml.safe_load(fh) or {}

    profiles_raw = raw.get("profiles") or {}
    if not profiles_raw:
        raise ProfileError(f"{target}: no profiles defined")

    # The registry first: it is what a profile's own external_units list is
    # validated against, so it has to exist before any profile is read.
    units = tuple(
        ExternalUnit(
            unit=str(u["unit"]),
            requires_internet=bool(u.get("requires_internet", False)),
        )
        for u in raw.get("external_units") or []
    )
    known_units = tuple(dict.fromkeys(u.unit for u in units))

    profiles = {}
    for name, body in profiles_raw.items():
        spec = _spec(name, body or {}, known_units)
        profiles[spec.name] = spec

    try:
        default = Profile(raw.get("default_profile", Profile.HOSTED.value))
    except ValueError as exc:
        raise ProfileError(f"{target}: bad default_profile") from exc
    if default not in profiles:
        raise ProfileError(f"{target}: default_profile {default.value} is not defined")

    return ProfileConfig(default=default, profiles=profiles, external_units=units)
