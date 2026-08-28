"""Everything root does, and the seam that lets it be tested by an ordinary user.

No test here spawns a process or needs a privilege: ``subprocess.run`` is
monkeypatched where the real executor is under test, and everything else runs
against ``RecordingExecutor``.
"""

from __future__ import annotations

import subprocess

import pytest

from cubesat.common import config
from cubesat.hostd import executor as executor_module
from cubesat.hostd.allowlist import Allowlist, Refused
from cubesat.hostd.executor import (
    DEFAULT_TIMEOUT_SEC,
    MOCK_HOST_ENV,
    TIMED_OUT,
    UNAVAILABLE,
    UNKNOWN,
    ExecutorError,
    HostActions,
    PrivilegeError,
    RecordingExecutor,
    Result,
    SubprocessExecutor,
    select_executor,
)


class ScriptedExecutor(RecordingExecutor):
    """A recording executor that can be told which commands fail.

    Shared with the network and service tests: the failure paths are the
    interesting ones, and every one of them has to be reachable without a host.
    """

    def __init__(self, *, fails=(), outputs=None, states=None) -> None:
        super().__init__(states)
        self.fails = tuple(tuple(prefix) for prefix in fails)
        #: argv prefix -> stdout, for the commands that are read rather than run.
        self.outputs = {tuple(prefix): text for prefix, text in (outputs or {}).items()}
        self.governor_error: str | None = None

    def run(self, argv, *, timeout=DEFAULT_TIMEOUT_SEC):
        command = tuple(str(part) for part in argv)
        for prefix in self.fails:
            if command[: len(prefix)] == prefix:
                self.calls.append(command)
                return Result(command, 1, "", f"{command[0]} refused")
        for prefix, text in self.outputs.items():
            if command[: len(prefix)] == prefix:
                self.calls.append(command)
                return Result(command, 0, text)
        return super().run(argv, timeout=timeout)

    def write_governor(self, governor):
        if self.governor_error is not None:
            raise ExecutorError(self.governor_error)
        return super().write_governor(governor)


@pytest.fixture
def host():
    """A HostActions over the standard allowlist and a no-op executor."""
    executor = ScriptedExecutor()
    return HostActions(executor, Allowlist(["telegram-bot.service"])), executor


# ── Result ──────────────────────────────────────────────────────────────────


def test_a_failure_message_prefers_stderr_then_stdout_then_the_exit_code():
    assert Result(("x",), 1, "out", "err").message == "err"
    assert Result(("x",), 1, "out", "  ").message == "out"
    assert Result(("x",), 3).message == "exit 3"
    assert Result(("x",), 0).ok


def test_a_verbose_failure_is_truncated_because_it_travels_in_a_retained_message():
    # A retained payload is what every late subscriber reads, forever.
    long = Result(("x",), 1, "", "y" * 5000)
    assert len(long.message) == executor_module.MAX_MESSAGE_CHARS


# ── the real executor ───────────────────────────────────────────────────────


def test_the_real_executor_never_uses_a_shell(monkeypatch):
    # An SSID comes from a config file today and could come from a ground
    # command tomorrow; there must be nothing to quote.
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "active\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SubprocessExecutor().run(["systemctl", "is-active", "cubesat@adcs.service"])

    assert seen["argv"] == ("systemctl", "is-active", "cubesat@adcs.service")
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["timeout"] == DEFAULT_TIMEOUT_SEC
    assert result.stdout == "active\n"


def test_a_command_that_hangs_is_reported_rather_than_taking_hostd_with_it(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SubprocessExecutor().run(["systemctl", "start", "x.service"], timeout=1.0)
    assert result.returncode == TIMED_OUT
    assert "timed out" in result.stderr


def test_a_missing_binary_is_an_ordinary_answer_not_an_exception(monkeypatch):
    # nmcli on a Pi without NetworkManager, iw on a headless image.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    result = SubprocessExecutor().run(["nmcli", "radio", "wifi", "on"])
    assert result.returncode == UNAVAILABLE
    assert not result.ok


def test_the_governor_is_written_to_every_core(tmp_path):
    for cpu in ("cpu0", "cpu1"):
        path = tmp_path / cpu / "cpufreq"
        path.mkdir(parents=True)
        (path / "scaling_governor").write_text("ondemand")
    # A directory that is not a cpuN must not be swept up with them.
    (tmp_path / "cpuidle").mkdir()

    written = SubprocessExecutor(cpu_root=tmp_path).write_governor("powersave")

    assert len(written) == 2
    assert all(path.read_text() == "powersave" for path in written)


def test_a_kernel_without_cpufreq_is_an_error_rather_than_a_silent_success(tmp_path):
    with pytest.raises(ExecutorError, match="no cpufreq"):
        SubprocessExecutor(cpu_root=tmp_path).write_governor("ondemand")


def test_a_governor_file_that_refuses_the_write_is_reported(tmp_path):
    # Writing a directory raises OSError, which stands in for the read-only
    # sysfs of a kernel built without the governor compiled in.
    (tmp_path / "cpu0" / "cpufreq" / "scaling_governor").mkdir(parents=True)
    with pytest.raises(ExecutorError, match="writing"):
        SubprocessExecutor(cpu_root=tmp_path).write_governor("ondemand")


# ── the fake executor ───────────────────────────────────────────────────────


def test_the_mock_executor_records_instead_of_acting(caplog):
    executor = RecordingExecutor()
    with caplog.at_level("INFO"):
        executor.run(["systemctl", "start", "cubesat@dhs.service"])
        executor.write_governor("performance")
    assert executor.calls == [("systemctl", "start", "cubesat@dhs.service")]
    assert executor.governors == ["performance"]
    assert "would run" in caplog.text


def test_the_mock_executor_remembers_what_it_started_so_idempotence_is_testable():
    executor = RecordingExecutor()
    assert executor.run(["systemctl", "is-active", "cubesat@dhs.service"]).stdout == "inactive\n"
    executor.run(["systemctl", "start", "cubesat@dhs.service"])
    answer = executor.run(["systemctl", "is-active", "cubesat@dhs.service"])
    assert answer.stdout == "active\n"
    assert answer.ok
    executor.run(["systemctl", "stop", "cubesat@dhs.service"])
    assert executor.run(["systemctl", "is-active", "cubesat@dhs.service"]).stdout == "inactive\n"


def test_the_mock_executor_answers_anything_else_with_success():
    assert RecordingExecutor().run(["nmcli", "radio", "wifi", "off"]).ok
    assert RecordingExecutor().run(["systemctl"]).ok


# ── choosing between them ───────────────────────────────────────────────────


def test_the_mock_executor_is_selected_loudly(monkeypatch, caplog):
    # Mistaking one executor for the other is the worst confusion available in
    # this service, so both branches announce themselves at WARNING.
    monkeypatch.setattr(config, "MOCK_HOST", True)
    with caplog.at_level("WARNING"):
        executor = select_executor()
    assert isinstance(executor, RecordingExecutor)
    assert "MOCK executor" in caplog.text


def test_the_real_executor_is_selected_loudly(monkeypatch, caplog):
    monkeypatch.setattr(config, "MOCK_HOST", False)
    with caplog.at_level("WARNING"):
        executor = select_executor(geteuid=lambda: 0)
    assert isinstance(executor, SubprocessExecutor)
    assert "REAL executor" in caplog.text


def test_running_the_real_executor_unprivileged_is_refused_not_survived(monkeypatch):
    # Every systemctl call would fail while HOSTD reported profiles as applied.
    monkeypatch.setattr(config, "MOCK_HOST", False)
    with pytest.raises(PrivilegeError, match="needs root") as refusal:
        select_executor(geteuid=lambda: 1000)
    # The message has to name the way out, or it is only half a message.
    assert MOCK_HOST_ENV in str(refusal.value)


# ── HostActions ─────────────────────────────────────────────────────────────


def test_a_refused_unit_starts_no_process_at_all(host):
    # The allowlist is a safety property only if it is checked before the argv
    # exists. Inspecting a result afterwards would mean the process already ran.
    actions, executor = host
    for method in (actions.start, actions.stop, actions.unit_state):
        with pytest.raises(Refused):
            method("mosquitto.service")
    assert executor.calls == []


def test_starting_and_stopping_a_permitted_unit(host):
    actions, executor = host
    actions.start("cubesat@adcs.service")
    actions.stop("telegram-bot.service")
    assert executor.calls == [
        ("systemctl", "start", "cubesat@adcs.service"),
        ("systemctl", "stop", "telegram-bot.service"),
    ]


def test_a_unit_that_refuses_to_start_raises_so_the_profile_can_report_it(host):
    actions, executor = host
    executor.fails = (("systemctl", "start"),)
    with pytest.raises(ExecutorError, match="systemctl start cubesat@dhs.service"):
        actions.start("cubesat@dhs.service")


def test_a_unit_state_is_whatever_systemd_says(host):
    actions, executor = host
    executor.states["cubesat@comms.service"] = "failed"
    assert actions.unit_state("cubesat@comms.service") == "failed"


def test_a_systemctl_that_says_nothing_is_reported_as_unknown(host):
    actions, executor = host
    executor.fails = (("systemctl", "is-active"),)
    assert actions.unit_state("cubesat@comms.service") == UNKNOWN


def test_an_unknown_governor_never_reaches_the_filesystem(host):
    # The validation is what makes it safe for this value to come from a config
    # file — or one day from a ground command.
    actions, executor = host
    with pytest.raises(ExecutorError, match="unknown CPU governor"):
        actions.set_governor("turbo; rm -rf /")
    assert executor.governors == []


def test_a_known_governor_is_written(host, caplog):
    actions, executor = host
    with caplog.at_level("INFO"):
        actions.set_governor("powersave")
    assert executor.governors == ["powersave"]
    assert "governor set to powersave" in caplog.text


def test_a_governor_that_cannot_be_written_is_raised_to_the_caller(host):
    actions, executor = host
    executor.governor_error = "sysfs is read-only"
    with pytest.raises(ExecutorError, match="read-only"):
        actions.set_governor("ondemand")


def test_poweroff_takes_no_arguments_so_the_reason_cannot_become_one(host, caplog):
    actions, executor = host
    with caplog.at_level("WARNING"):
        actions.poweroff("battery_critical")
    assert executor.calls == [("systemctl", "poweroff")]
    # The reason is worth having in the log and nowhere else.
    assert "battery_critical" in caplog.text


def test_a_poweroff_that_fails_is_raised_so_obc_learns_the_host_is_still_up(host):
    actions, executor = host
    executor.fails = (("systemctl", "poweroff"),)
    with pytest.raises(ExecutorError, match="poweroff"):
        actions.poweroff("battery_critical")
