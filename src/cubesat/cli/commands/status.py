"""``cubesat status`` — is it well, and what is it doing?

Everything printed here is a **retained** MQTT message, which is why this is a
one-second command and not a query: the broker replays the current situation on
subscribe. Two consequences worth knowing.

The host's own CPU, RAM, disk and uptime come from ``cubesat/dhs/telemetry`` —
the wide row DHS assembles every tick and publishes whether or not it is being
recorded. Before that topic existed this command would have had to read the
database, which meant it could not answer at all in the profiles that record
nothing. Now it answers the same way everywhere.

The last profile before the current boot comes off the disk instead, from
``/var/lib/cubesat/last-profile``. It answers the question no live topic can:
what was it doing when it died? The satellite reads the same file for one narrow
purpose — resuming an interrupted ``FLIGHT``, and only after measuring that
there is no mains (``obc/resume.py``) — so what is printed here is the evidence
that rule was given, including how many resumes in a row it has taken.
"""

from __future__ import annotations

from typing import Any

from cubesat.cli.session import Session
from cubesat.common import config, last_profile

SNAPSHOT = (
    "host_status",
    "obc_status",
    "eps_status",
    "payload_status",
    "comms_status",
    "dhs_status",
    "dhs_telemetry",
)


def show(session: Session) -> tuple[int, list[str]]:
    seen = session.collect(*SNAPSHOT)
    obc = seen.get("obc_status")
    if obc is None:
        return 1, ["OBC has published no status. Is `cubesat@obc` running?"]

    row = (seen.get("dhs_telemetry") or {}).get("row") or {}
    lines = [
        f"State:     {obc.get('status') or '-'} in {obc.get('profile') or '-'}",
        f"Power:     {_power(seen.get('eps_status'))}",
        f"Radio:     {_radio(seen.get('comms_status'))}",
        f"Recorder:  {_recorder(seen.get('dhs_status'))}",
        f"Host:      {_host_metrics(row)}",
        f"Sensors:   {_sensors(row)}",
        f"Subsystems: {_subsystems(obc)}",
    ]

    last = _last_profile()
    if last is not None:
        lines.append(f"Before this boot: {last}")
    boot = obc.get("boot")
    if isinstance(boot, dict) and boot.get("previous"):
        lines.append(f"This boot: {_boot(boot)}")
    errors = (seen.get("host_status") or {}).get("errors") or []
    lines.extend(f"⚠ HOSTD: {error}" for error in errors)
    return 0, lines


def _power(eps: dict[str, Any] | None) -> str:
    if eps is None:
        return "-"
    percent = eps.get("battery_percent")
    volts = eps.get("voltage")
    mains = eps.get("external_power")
    parts = [
        f"{percent:.1f}%" if isinstance(percent, (int, float)) else "?%",
        f"{volts:.3f} V" if isinstance(volts, (int, float)) else "? V",
        "on mains" if mains else "on battery",
    ]
    rate = eps.get("charge_rate")
    if isinstance(rate, (int, float)):
        parts.append(f"{rate:+.2f} %/h")
    # Whichever way the pack is going, at most one of these is a number. Both
    # are estimates off an inferred curve, so they are shown to the nearest
    # tenth of an hour and never to the minute.
    remaining = eps.get("time_to_empty_sec")
    if isinstance(remaining, (int, float)):
        parts.append(f"{remaining / 3600.0:.1f} h to empty")
    until_full = eps.get("time_to_full_sec")
    if isinstance(until_full, (int, float)):
        parts.append(f"{until_full / 3600.0:.1f} h to full")
    return ", ".join(parts)


def _radio(comms: dict[str, Any] | None) -> str:
    """Quiet and deaf are different states, and this is where an operator checks
    which one the beacon is in — `DEMO` and `EXPO` start it off deliberately."""
    if comms is None:
        return "not running"
    if not comms.get("lora_listening"):
        return "off (this profile does not use the radio)"
    # Since 2026-09-03 "beacon off" no longer means silence: a command still
    # gets an answer. The wording says listening rather than quiet for exactly
    # that reason.
    return "beaconing" if comms.get("beacon_enabled") else "listening only (beacon off)"


def _recorder(dhs: dict[str, Any] | None) -> str:
    if dhs is None:
        return "not running"
    mission = dhs.get("mission")
    size = dhs.get("db_size_bytes")
    where = f", {size / 1_048_576:.1f} MB on the card" if isinstance(size, (int, float)) else ""
    if not dhs.get("recording") or not isinstance(mission, dict):
        return f"idle{where}"
    label = mission.get("label") or "unlabelled"
    return f"mission {mission.get('id')} — {label}, {mission.get('rows') or 0} rows{where}"


def _host_metrics(row: dict[str, Any]) -> str:
    if not row:
        return "- (DHS has published no row yet)"
    def pct(key: str) -> str:
        value = row.get(key)
        return f"{value:.0f}%" if isinstance(value, (int, float)) else "?"

    uptime = row.get("uptime_seconds")
    up = f"{uptime / 3600:.1f}h" if isinstance(uptime, (int, float)) else "?"
    temp = row.get("cpu_temperature")
    degrees = f"{temp:.1f}°C" if isinstance(temp, (int, float)) else "?"
    return (
        f"cpu {pct('cpu_percent')}, ram {pct('ram_percent')}, "
        f"disk {pct('disk_percent')}, up {up}, {degrees}"
    )


def _sensors(row: dict[str, Any]) -> str:
    if not row:
        return "-"
    parts = []
    temperature = row.get("temperature")
    if isinstance(temperature, (int, float)):
        parts.append(f"{temperature:.1f}°C")
    fix = row.get("fix")
    satellites = row.get("satellites")
    if fix:
        parts.append(f"fix, {satellites or 0} satellites")
    else:
        parts.append("no fix")
    return ", ".join(parts) if parts else "-"


def _subsystems(obc: dict[str, Any]) -> str:
    """OBC's own verdict, not this tool's opinion. `lost` is what a bring-up
    would have failed on; an empty `lost` with a populated `watched` is health."""
    block = obc.get("subsystems")
    if not isinstance(block, dict):
        return "-"
    watched = block.get("watched") or []
    lost = block.get("lost") or []
    if not watched:
        return "none expected in this profile"
    if lost:
        return f"{len(watched)} watched, LOST: {', '.join(str(name) for name in lost)}"
    return f"{len(watched)} watched, all reporting"


def _last_profile() -> str | None:
    """What the run before this boot was doing — see the module docstring."""
    previous = last_profile.read(config.LAST_PROFILE_FILE)
    if previous is None or not previous.profile:
        return None
    line = previous.profile
    if previous.mission_label:
        line += f" ({previous.mission_label})"
    if previous.resume_count:
        line += f", {previous.resume_count} resume(s) in a row"
    return line


def _boot(boot: dict[str, Any]) -> str:
    """OBC's verdict on this boot: resumed, or refused with a reason."""
    previous = boot.get("previous")
    if boot.get("resumed"):
        return f"resumed {previous} after a reset"
    reason = boot.get("reason")
    return f"did not resume {previous}" + (f" ({reason})" if reason else "")
