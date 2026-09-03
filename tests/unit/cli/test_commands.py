"""The four commands, against a fake broker and a real database file.

What is asserted is the text an operator reads and the exit code a script sees,
because those are the whole of what this program is: it has no state of its own
and every decision it reports belongs to the satellite.
"""

from __future__ import annotations

import logging

import pytest

from cubesat.cli.commands import mission as mission_cmd
from cubesat.cli.commands import profile as profile_cmd
from cubesat.cli.commands import status as status_cmd
from cubesat.cli.session import Session
from cubesat.common import config, profiles
from cubesat.common.states import EndReason, Profile
from cubesat.common.topics import TOPICS
from cubesat.dhs import schema
from cubesat.dhs.missions import MissionStore

LOG = logging.getLogger("test-cli")


@pytest.fixture
def session(fake_client):
    with Session(client=fake_client, collect_window=0.01, apply_timeout=0.05) as live:
        yield live


def text(lines: list[str]) -> str:
    return "\n".join(lines)


# ── profile ─────────────────────────────────────────────────────────────────


def test_profile_reports_what_the_platform_achieved(session, fake_client):
    fake_client.deliver(TOPICS["host_status"], {"profile": "DEMO", "profile_requested": "DEMO"})
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "cadence_scale": 1.0})
    fake_client.deliver(
        TOPICS["dhs_status"],
        {"recording": True, "mission": {"id": 11, "label": "walk to work", "rows": 42}},
    )

    code, lines = profile_cmd.show(session)

    assert code == 0
    assert "Profile:       DEMO" in text(lines)
    assert "Mission state: NOMINAL" in text(lines)
    assert "mission 11 — walk to work (42 rows)" in text(lines)


def test_profile_says_when_a_switch_only_half_applied(session, fake_client):
    # The difference between achieved and requested is the whole debugging story
    # of a failed switch, so it is the thing printed rather than smoothed over.
    fake_client.deliver(
        TOPICS["host_status"],
        {"profile": "HOSTED", "profile_requested": "EXPO", "errors": ["hostapd failed"]},
    )
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})

    _code, lines = profile_cmd.show(session)

    assert "a switch to EXPO did not fully apply" in text(lines)
    assert "hostapd failed" in text(lines)


def test_profile_with_nothing_published_says_so_rather_than_idle(session):
    # Both statuses are retained and published in every profile, so silence is
    # not "the satellite is idle" — it is nobody home.
    code, lines = profile_cmd.show(session)
    assert code == 1
    assert "Nothing has published a status" in text(lines)


def test_a_typo_is_refused_from_this_disk(session):
    wanted, complaints = profile_cmd.resolve(profiles.load(), "MARS")
    assert wanted is None
    assert "Unknown profile 'MARS'" in text(complaints)


def test_a_profile_this_deployment_does_not_define_is_refused_too():
    trimmed = profiles.load()
    del trimmed.profiles[Profile.DIAG]
    wanted, complaints = profile_cmd.resolve(trimmed, "diag")
    assert wanted is None
    assert "not defined in this deployment" in text(complaints)


def test_switching_publishes_the_command_and_confirms_from_host_status(session, fake_client):
    fake_client.deliver(
        TOPICS["host_status"], {"profile": "FLIGHT", "profile_requested": "FLIGHT", "errors": []}
    )

    code, lines = profile_cmd.switch(session, Profile.FLIGHT, ttl_minutes=480, mission_label="walk")

    assert code == 0
    assert "FLIGHT applied." in text(lines)
    published = fake_client.payloads(TOPICS["command"])[-1]
    assert published["command"] == "set_profile"
    assert published["params"] == {
        "profile": "FLIGHT",
        "ttl_minutes": 480,
        "mission_label": "walk",
    }


def test_switching_without_a_ttl_sends_none_so_the_profile_s_own_applies(session, fake_client):
    # The satellite's own default is the right answer: FLIGHT carries 600
    # minutes, which is the strap on a trip somebody forgot to end.
    fake_client.deliver(
        TOPICS["host_status"], {"profile": "DEMO", "profile_requested": "DEMO", "errors": []}
    )
    profile_cmd.switch(session, Profile.DEMO)
    assert fake_client.payloads(TOPICS["command"])[-1]["params"] == {"profile": "DEMO"}


def test_a_switch_that_only_half_applied_is_not_a_success(session, fake_client):
    fake_client.deliver(
        TOPICS["host_status"],
        {"profile": "HOSTED", "profile_requested": "EXPO", "errors": ["hostapd failed"]},
    )

    code, lines = profile_cmd.switch(session, Profile.EXPO)

    assert code == 1
    assert "applied only in part" in text(lines)
    assert "hostapd failed" in text(lines)


def test_a_switch_nobody_answered_names_the_logs_to_read(session, fake_client):
    code, lines = profile_cmd.switch(session, Profile.EXPO)
    assert code == 1
    assert "No answer from the satellite" in text(lines)
    assert "journalctl" in text(lines)
    # It was published even so: the command may well have been acted on.
    assert fake_client.payloads(TOPICS["command"])[-1]["command"] == "set_profile"


def test_a_ttl_that_was_armed_is_reported_back(session, fake_client):
    fake_client.deliver(
        TOPICS["host_status"],
        {
            "profile": "FLIGHT",
            "profile_requested": "FLIGHT",
            "errors": [],
            "ttl_expires_at": 1_700_000_000.0,
        },
    )
    _code, lines = profile_cmd.switch(session, Profile.FLIGHT)
    assert "Returns to the default profile at" in text(lines)


def test_profile_reports_a_ttl_that_is_running_out(session, fake_client):
    fake_client.deliver(
        TOPICS["host_status"],
        {"profile": "FLIGHT", "profile_requested": "FLIGHT", "ttl_expires_at": 1_700_000_000.0},
    )
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    _code, lines = profile_cmd.show(session)
    assert "TTL:" in text(lines)


def test_profile_says_the_recorder_is_idle_rather_than_missing(session, fake_client):
    # DHS runs in DEMO and EXPO and deliberately records nothing there, so
    # "running but not recording" is a normal state with its own words.
    fake_client.deliver(TOPICS["host_status"], {"profile": "DEMO", "profile_requested": "DEMO"})
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    fake_client.deliver(TOPICS["dhs_status"], {"recording": False, "mission": None})
    _code, lines = profile_cmd.show(session)
    assert "Recording:     no" in text(lines)


def test_profile_with_only_obc_published_still_answers(session, fake_client):
    # HOSTD publishes nothing until its first profile is applied — deliberately,
    # because an empty host_status reads to OBC as a fault.
    fake_client.deliver(TOPICS["obc_status"], {"status": "BOOT"})
    code, lines = profile_cmd.show(session)
    assert code == 0
    assert "HOSTD has published nothing" in text(lines)


# ── status ──────────────────────────────────────────────────────────────────


def test_status_reads_the_host_metrics_off_the_published_row(session, fake_client, tmp_path):
    # The point of cubesat/dhs/telemetry: CPU, RAM and disk exist in no status
    # message, and before that topic this command could not answer at all in the
    # profiles that record nothing.
    fake_client.deliver(
        TOPICS["obc_status"],
        {"status": "NOMINAL", "profile": "DEMO", "subsystems": {"watched": ["eps"], "lost": []}},
    )
    fake_client.deliver(TOPICS["eps_status"], {"battery_percent": 72.4, "voltage": 3.905})
    fake_client.deliver(TOPICS["comms_status"], {"lora_listening": True, "beacon_enabled": False})
    fake_client.deliver(
        TOPICS["dhs_telemetry"],
        {
            "row": {
                "cpu_percent": 34.0,
                "ram_percent": 52.0,
                "disk_percent": 41.0,
                "uptime_seconds": 7200.0,
                "cpu_temperature": 48.2,
                "temperature": 27.9,
                "fix": 1,
                "satellites": 9,
            }
        },
    )

    code, lines = status_cmd.show(session)
    body = text(lines)

    assert code == 0
    assert "cpu 34%, ram 52%, disk 41%, up 2.0h, 48.2°C" in body
    assert "72.4%, 3.905 V, on battery" in body
    assert "fix, 9 satellites" in body
    assert "all reporting" in body


def test_status_distinguishes_a_quiet_radio_from_a_deaf_one(session, fake_client):
    # DEMO and EXPO start the beacon off deliberately, so "quiet" has to read as
    # a setting rather than as a fault.
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "DEMO"})
    fake_client.deliver(TOPICS["comms_status"], {"lora_listening": True, "beacon_enabled": False})
    _code, lines = status_cmd.show(session)
    assert "listening only (beacon off)" in text(lines)

    fake_client.deliver(TOPICS["comms_status"], {"lora_listening": False, "beacon_enabled": False})
    _code, lines = status_cmd.show(session)
    assert "this profile does not use the radio" in text(lines)


def test_status_names_the_subsystems_obc_declared_lost(session, fake_client):
    fake_client.deliver(
        TOPICS["obc_status"],
        {
            "status": "SAFE",
            "profile": "DEMO",
            "subsystems": {"watched": ["adcs", "eps"], "lost": ["adcs"]},
        },
    )
    _code, lines = status_cmd.show(session)
    assert "LOST: adcs" in text(lines)


def test_status_reports_the_profile_before_the_last_boot(
    session, fake_client, tmp_path, monkeypatch
):
    # HOSTD writes it, nothing reads it to decide anything, and it answers the
    # question no live topic can: what was it doing when it died?
    path = tmp_path / "last-profile"
    path.write_text("FLIGHT\n")
    monkeypatch.setattr(config, "LAST_PROFILE_FILE", path)
    fake_client.deliver(TOPICS["obc_status"], {"status": "STANDBY", "profile": "HOSTED"})

    _code, lines = status_cmd.show(session)

    assert "Before this boot: FLIGHT" in text(lines)


def test_status_says_nothing_about_a_last_profile_file_that_is_absent(
    session, fake_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "LAST_PROFILE_FILE", tmp_path / "absent")
    fake_client.deliver(TOPICS["obc_status"], {"status": "STANDBY", "profile": "HOSTED"})
    _code, lines = status_cmd.show(session)
    assert "Before this boot" not in text(lines)


def test_status_with_no_obc_says_which_unit_to_look_at(session):
    code, lines = status_cmd.show(session)
    assert code == 1
    assert "cubesat@obc" in text(lines)


def test_status_reports_the_charge_rate_when_the_gauge_gives_one(session, fake_client):
    # A SOC that disagrees with its own charge rate is the fault this pairing
    # exists to make visible, and an operator checks the two side by side. The
    # value here is the one the old gauge driver reported forever (V12, closed
    # 2026-09-01: it was 0xFFFF decoded, not a measurement).
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "DEMO"})
    fake_client.deliver(
        TOPICS["eps_status"],
        {"battery_percent": 66.1, "voltage": 3.92, "external_power": True, "charge_rate": -0.208},
    )
    _code, lines = status_cmd.show(session)
    assert "on mains" in text(lines)
    assert "-0.21 %/h" in text(lines)


def test_status_reports_the_recorder_writing_and_the_card_it_writes_to(session, fake_client):
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "FLIGHT"})
    fake_client.deliver(
        TOPICS["dhs_status"],
        {
            "recording": True,
            "mission": {"id": 12, "label": "2026-09-01 07:12", "rows": 8},
            "db_size_bytes": 5_242_880,
        },
    )
    _code, lines = status_cmd.show(session)
    assert "mission 12 — 2026-09-01 07:12, 8 rows, 5.0 MB on the card" in text(lines)


def test_status_calls_a_running_recorder_that_records_nothing_idle(session, fake_client):
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "DEMO"})
    fake_client.deliver(TOPICS["dhs_status"], {"recording": False, "mission": None})
    _code, lines = status_cmd.show(session)
    assert "Recorder:  idle" in text(lines)


def test_status_says_no_fix_rather_than_zero_coordinates(session, fake_client):
    # The receiver reports no fix as tidy zeros, and 0,0 is a real place in the
    # Gulf of Guinea.
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "FLIGHT"})
    fake_client.deliver(TOPICS["dhs_telemetry"], {"row": {"fix": 0, "temperature": 21.0}})
    _code, lines = status_cmd.show(session)
    assert "no fix" in text(lines)


def test_status_says_when_a_profile_expects_no_subsystems(session, fake_client):
    # HOSTED watches EPS alone; MAINTENANCE watches nothing, and an empty list
    # there is correct rather than a fault.
    fake_client.deliver(
        TOPICS["obc_status"],
        {"status": "STANDBY", "profile": "MAINTENANCE", "subsystems": {"watched": [], "lost": []}},
    )
    _code, lines = status_cmd.show(session)
    assert "none expected in this profile" in text(lines)


def test_status_without_a_published_row_says_so_instead_of_inventing_metrics(session, fake_client):
    fake_client.deliver(TOPICS["obc_status"], {"status": "BOOT", "profile": "HOSTED"})
    _code, lines = status_cmd.show(session)
    assert "DHS has published no row yet" in text(lines)
    assert "Sensors:   -" in text(lines)


# ── mission list ────────────────────────────────────────────────────────────


def test_mission_list_reads_the_card_directly(tmp_path):
    # Not over HTTP: the dashboard service does not run in FLIGHT at all, which
    # is exactly the profile whose missions somebody wants to list afterwards.
    path = tmp_path / "comms.db"
    conn = schema.connect(path, LOG)
    store = MissionStore(conn, LOG)
    walk = store.open("FLIGHT", "walk to work")
    conn.execute(
        "INSERT INTO telemetry (timestamp, mission_id, lat, lon, fix) VALUES (?, ?, ?, ?, ?)",
        ("2026-08-24T07:00:00Z", walk.id, 55.75, 37.61, 1),
    )
    conn.commit()
    store.close(walk.id, EndReason.PROFILE_CHANGE)
    conn.close()

    code, lines = mission_cmd.listing(database=path)

    assert code == 0
    assert "1 mission(s)" in text(lines)
    assert "#1 · walk to work · FLIGHT" in text(lines)
    assert "profile_change" in text(lines)


def test_an_empty_archive_is_not_an_error(tmp_path):
    # A satellite that has never left the desk has no missions, and a
    # demonstration deliberately records none.
    code, lines = mission_cmd.listing(database=tmp_path / "absent.db")
    assert code == 0
    assert "No missions recorded" in text(lines)


def test_a_long_archive_is_trimmed_with_a_note(tmp_path):
    path = tmp_path / "comms.db"
    conn = schema.connect(path, LOG)
    store = MissionStore(conn, LOG)
    for _ in range(4):
        store.open("DEMO")
    conn.close()

    code, lines = mission_cmd.listing(database=path, limit=2)

    assert code == 0
    assert "and 2 older" in text(lines)
    assert len([line for line in lines if line.startswith("  #")]) == 2


def test_all_shows_every_mission(tmp_path):
    path = tmp_path / "comms.db"
    conn = schema.connect(path, LOG)
    store = MissionStore(conn, LOG)
    for _ in range(3):
        store.open("DEMO")
    conn.close()

    _code, lines = mission_cmd.listing(database=path, limit=0)

    assert len([line for line in lines if line.startswith("  #")]) == 3


def test_a_mission_still_open_says_so(tmp_path):
    path = tmp_path / "comms.db"
    conn = schema.connect(path, LOG)
    MissionStore(conn, LOG).open("FLIGHT")
    conn.close()

    _code, lines = mission_cmd.listing(database=path)

    assert "still open" in text(lines)


def test_a_purged_mission_is_listed_as_purged_not_as_empty(tmp_path):
    # Retention drops the detail and keeps the row: a trip that happened stays
    # listed, and an empty chart for it would be a lie.
    path = tmp_path / "comms.db"
    conn = schema.connect(path, LOG)
    store = MissionStore(conn, LOG)
    mission = store.open("FLIGHT", "old walk")
    store.close(mission.id, EndReason.SHUTDOWN)
    conn.execute(
        "UPDATE missions SET purged_at = ? WHERE id = ?", ("2026-09-29T00:00:00Z", mission.id)
    )
    conn.commit()
    conn.close()

    _code, lines = mission_cmd.listing(database=path)

    assert "detail purged" in text(lines)
