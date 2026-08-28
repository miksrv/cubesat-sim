"""The shared vocabulary.

Every profile, state or reason that crosses a process boundary is one of these
enums, never a bare string. Eight services exchanging free-form strings over
MQTT is how ``"LOW_POWER"`` becomes ``"low_power"`` in one publisher and stops
matching everywhere else.

All of them subclass ``str`` so they serialise straight into JSON and compare
equal to the wire value, while still failing loudly on a typo at construction.
"""

from enum import Enum


class Profile(str, Enum):
    """Platform profile — what the Raspberry Pi is allowed to be. Chosen by a human."""

    HOSTED = "HOSTED"
    DEMO = "DEMO"
    EXPO = "EXPO"
    FLIGHT = "FLIGHT"
    DIAG = "DIAG"
    MAINTENANCE = "MAINTENANCE"


class MissionState(str, Enum):
    """Mission state — the satellite's own activity level. Chosen by the satellite."""

    BOOT = "BOOT"
    STANDBY = "STANDBY"
    DEPLOY = "DEPLOY"
    NOMINAL = "NOMINAL"
    SCIENCE = "SCIENCE"
    LOW_POWER = "LOW_POWER"
    SAFE = "SAFE"
    CRITICAL = "CRITICAL"


class MissionMode(str, Enum):
    """What a profile asks of the mission state machine."""

    ACTIVE = "active"  # bring the subsystems up: STANDBY -> DEPLOY -> NOMINAL
    STANDBY = "standby"  # platform only, no mission
    NONE = "none"  # not even the bus (MAINTENANCE)


class NetworkMode(str, Enum):
    CLIENT = "client"  # join a known network
    AP = "ap"  # be the network
    OFF = "off"  # radio down


class Persistence(str, Enum):
    NONE = "none"
    MISSION_DB = "mission_db"
    DIAG_DB = "diag_db"


class EndReason(str, Enum):
    """Why a mission stopped recording. ``INTERRUPTED`` is never written by the
    mission that ended — it is applied by DHS at the next startup to a mission
    that never got to close itself."""

    PROFILE_CHANGE = "profile_change"
    SHUTDOWN = "shutdown"
    BATTERY_CRITICAL = "battery_critical"
    INTERRUPTED = "interrupted"


#: Profiles that run the mission services. Everything else is platform-only.
ACTIVE_PROFILES = frozenset(
    {Profile.DEMO, Profile.EXPO, Profile.FLIGHT, Profile.DIAG}
)

#: States in which the payload camera may be used.
CAMERA_ALLOWED_STATES = frozenset({MissionState.NOMINAL, MissionState.SCIENCE})

#: States in which a mission is actively recording.
RECORDING_STATES = frozenset(
    {MissionState.NOMINAL, MissionState.SCIENCE, MissionState.LOW_POWER, MissionState.SAFE}
)
