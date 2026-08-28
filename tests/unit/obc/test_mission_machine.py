import pytest

from cubesat.common.states import MissionState
from cubesat.obc import mission_machine
from cubesat.obc.mission_machine import MissionMachine

BOOT = MissionState.BOOT
STANDBY = MissionState.STANDBY
DEPLOY = MissionState.DEPLOY
NOMINAL = MissionState.NOMINAL
SCIENCE = MissionState.SCIENCE
LOW_POWER = MissionState.LOW_POWER
SAFE = MissionState.SAFE
CRITICAL = MissionState.CRITICAL


@pytest.fixture
def machine():
    changes = []
    m = MissionMachine(on_change=lambda previous, current: changes.append((previous, current)))
    m.changes = changes
    return m


def drive(machine, *triggers):
    return [machine.fire(trigger) for trigger in triggers]


def test_it_starts_in_boot(machine):
    assert machine.state is BOOT


def test_boot_leads_to_standby_and_no_further_on_its_own(machine):
    # STANDBY is where a satellite under HOSTED lives. Nothing brings the
    # subsystems up until a profile asks for them.
    machine.fire(mission_machine.BOOT_COMPLETE)
    assert machine.state is STANDBY


def test_an_active_profile_walks_standby_through_deploy_to_nominal(machine):
    drive(machine, mission_machine.BOOT_COMPLETE, mission_machine.BEGIN_DEPLOY)
    assert machine.state is DEPLOY
    machine.fire(mission_machine.DEPLOY_COMPLETE)
    assert machine.state is NOMINAL


def test_nominal_and_science_swap_both_ways(machine):
    drive(
        machine,
        mission_machine.BOOT_COMPLETE,
        mission_machine.BEGIN_DEPLOY,
        mission_machine.DEPLOY_COMPLETE,
        mission_machine.SCIENCE_START,
    )
    assert machine.state is SCIENCE
    machine.fire(mission_machine.SCIENCE_STOP)
    assert machine.state is NOMINAL


def test_deploy_cannot_be_skipped(machine):
    # Reaching NOMINAL without a self-test would be pretending the hardware
    # answered. The absent transition is the specification.
    machine.fire(mission_machine.BOOT_COMPLETE)
    assert machine.fire(mission_machine.DEPLOY_COMPLETE) is False
    assert machine.state is STANDBY


def test_science_is_refused_outside_nominal(machine):
    machine.fire(mission_machine.BOOT_COMPLETE)
    assert machine.fire(mission_machine.SCIENCE_START) is False
    assert machine.state is STANDBY


@pytest.mark.parametrize(
    "path",
    [
        (),
        (mission_machine.BOOT_COMPLETE,),
        (mission_machine.BOOT_COMPLETE, mission_machine.BEGIN_DEPLOY),
        (
            mission_machine.BOOT_COMPLETE,
            mission_machine.BEGIN_DEPLOY,
            mission_machine.DEPLOY_COMPLETE,
        ),
    ],
)
def test_safe_is_reachable_from_anywhere(machine, path):
    drive(machine, *path)
    assert machine.fire(mission_machine.ENTER_SAFE) is True
    assert machine.state is SAFE


def test_critical_is_reachable_from_anywhere_including_safe(machine):
    machine.fire(mission_machine.ENTER_SAFE)
    assert machine.fire(mission_machine.ENTER_CRITICAL) is True
    assert machine.state is CRITICAL


def test_nothing_outranks_critical(machine):
    # It is the only state permitted to change host power, and the host is
    # already going down. A profile change or a ground command arriving now must
    # not leave it.
    machine.fire(mission_machine.ENTER_CRITICAL)
    for trigger in (
        mission_machine.STAND_DOWN,
        mission_machine.ENTER_SAFE,
        mission_machine.RECOVER,
        mission_machine.BEGIN_DEPLOY,
    ):
        assert machine.fire(trigger) is False
    assert machine.state is CRITICAL


def test_low_power_is_only_entered_from_the_states_that_spend_power(machine):
    machine.fire(mission_machine.BOOT_COMPLETE)
    assert machine.fire(mission_machine.ENTER_LOW_POWER) is False
    drive(machine, mission_machine.BEGIN_DEPLOY)
    assert machine.fire(mission_machine.ENTER_LOW_POWER) is True


def test_recovery_lands_in_nominal_not_back_in_deploy(machine):
    # The subsystems never went away; they were throttled. Re-running the
    # self-test would drop a working satellite back through bring-up.
    for descent in (mission_machine.ENTER_SAFE, mission_machine.ENTER_LOW_POWER):
        m = MissionMachine()
        drive(m, mission_machine.BOOT_COMPLETE, mission_machine.BEGIN_DEPLOY)
        m.fire(mission_machine.DEPLOY_COMPLETE)
        m.fire(descent)
        assert m.fire(mission_machine.RECOVER) is True
        assert m.state is NOMINAL


def test_recovery_is_refused_where_there_is_nothing_to_recover_from(machine):
    machine.fire(mission_machine.BOOT_COMPLETE)
    assert machine.fire(mission_machine.RECOVER) is False
    assert machine.state is STANDBY


def test_leaving_an_active_profile_returns_to_standby_from_any_live_state(machine):
    drive(
        machine,
        mission_machine.BOOT_COMPLETE,
        mission_machine.BEGIN_DEPLOY,
        mission_machine.DEPLOY_COMPLETE,
        mission_machine.ENTER_SAFE,
    )
    assert machine.fire(mission_machine.STAND_DOWN) is True
    assert machine.state is STANDBY


def test_every_change_is_announced_once(machine):
    drive(machine, mission_machine.BOOT_COMPLETE, mission_machine.BEGIN_DEPLOY)
    assert machine.changes == [(BOOT, STANDBY), (STANDBY, DEPLOY)]


def test_a_repeated_trigger_announces_nothing(machine):
    # obc_status is retained and DHS acts on the transition into CRITICAL, so a
    # reflexive transition would be a spurious event three services away.
    machine.fire(mission_machine.ENTER_SAFE)
    machine.fire(mission_machine.ENTER_SAFE)
    assert machine.changes == [(BOOT, SAFE)]


def test_a_refused_trigger_is_logged_rather_than_raised(machine, caplog):
    # A ground command that does not apply in the current state is a refusal,
    # never a reason to take OBC down.
    with caplog.at_level("INFO"):
        assert machine.fire(mission_machine.DEPLOY_COMPLETE) is False
    assert "refused in BOOT" in caplog.text


def test_no_auto_transitions_are_generated():
    # transitions would otherwise add to_CRITICAL() and friends, which bypass
    # every rule in the table above.
    machine = MissionMachine()
    assert not hasattr(machine, "to_CRITICAL")
    assert not hasattr(machine, "to_NOMINAL")


def test_a_machine_without_a_listener_still_transitions():
    machine = MissionMachine()
    assert machine.fire(mission_machine.BOOT_COMPLETE) is True
