"""#118154: the post-update dashboard reap must not run against pre-pull sibling modules.

An updater from before the post-swap hand-off pulls, reloads ``dashboard_procs`` from the new
tree and calls it, while ``hermes_cli.main_dashboard`` (imported at startup by ``main.py``) and
``hermes_cli.profiles`` are still the pre-pull copies. The new reap then called the old finder
with ``scope_home=`` (TypeError) and old-missing launchd helpers (AttributeError).
"""

import json
import sys
import types

import psutil
import pytest

import hermes_cli
from hermes_cli import dashboard_procs

CHILD_ENV = "HERMES_DASHBOARD_REAP_CHILD"


def _pre_scope_home_main_dashboard() -> types.ModuleType:
    """A ``main_dashboard`` shaped like v0.21.2/v0.21.3: old finder, no launchd helpers."""
    module = types.ModuleType("hermes_cli.main_dashboard")

    def _find_stale_dashboard_pids(*, exclude_pids=None):
        return [424242]

    setattr(module, "_find_stale_dashboard_pids", _find_stale_dashboard_pids)
    setattr(module, "_restart_managed_dashboard_service", lambda reason, unit="hermes-dashboard.service": False)
    return module


class _StartedBeforeThePull:
    """``psutil.Process()`` for an interpreter older than every file on disk."""

    def create_time(self) -> float:
        return 0.0


def test_stale_updater_reaps_in_a_fresh_interpreter_not_the_old_modules(monkeypatch):
    stale = _pre_scope_home_main_dashboard()
    # ``from hermes_cli import main_dashboard`` reads the package attribute first.
    monkeypatch.setitem(sys.modules, "hermes_cli.main_dashboard", stale)
    monkeypatch.setattr(hermes_cli, "main_dashboard", stale)
    monkeypatch.setattr(psutil, "Process", lambda *a, **k: _StartedBeforeThePull())
    monkeypatch.delenv(CHILD_ENV, raising=False)
    spawned = []

    def fake_run(cmd, **kwargs):
        # Anything but the child spawn means the reap ran in-process against the stale module.
        assert cmd[0] == sys.executable, f"unexpected in-process subprocess: {cmd}"
        spawned.append((cmd, kwargs))
        result = {"matched": [7], "killed": [7], "failed": [], "unrecovered": []}
        with open(cmd[-1], "w", encoding="utf-8") as fh:
            json.dump(result, fh)

    monkeypatch.setattr(dashboard_procs.subprocess, "run", fake_run)

    result = dashboard_procs._kill_stale_dashboard_processes(
        restart_managed=True, already_restarted_units={"hermes-serve"}, scope_home="/tmp/own-home")

    assert result == {"matched": [7], "killed": [7], "failed": [], "unrecovered": []}
    [(cmd, kwargs)] = spawned
    assert cmd[0] == sys.executable
    assert json.loads(cmd[-2]) == {
        "reason": "the running backend no longer matches the updated frontend",
        "restart_managed": True, "already_restarted_units": ["hermes-serve"],
        "scope_home": "/tmp/own-home"}
    assert kwargs["env"][CHILD_ENV] == "1"


def test_child_scopes_an_unscoped_legacy_request_to_its_own_home(monkeypatch, tmp_path):
    """Pre-#113978 updaters pass no ``scope_home``; the child must not widen to every install."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    seen = {}

    def fake_reap(**kwargs):
        seen.update(kwargs)
        return {"matched": [], "killed": [], "failed": [(9, "gone")]}

    monkeypatch.setattr(dashboard_procs, "_kill_stale_dashboard_processes", fake_reap)
    result_path = tmp_path / "result.json"
    request = {"reason": "r", "restart_managed": True, "already_restarted_units": [], "scope_home": None}

    dashboard_procs._fresh_interpreter_reap_main(json.dumps(request), str(result_path))

    assert seen == {"reason": "r", "restart_managed": True, "already_restarted_units": None,
                    "scope_home": str(home)}
    assert json.loads(result_path.read_text(encoding="utf-8"))["failed"] == [[9, "gone"]]


def test_fresh_child_command_runs_on_this_tree(monkeypatch, tmp_path):
    """Real child: imports this tree, runs a scoped reap for an empty home, returns its result."""
    home = tmp_path / "nobody-runs-here"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = dashboard_procs._reap_in_fresh_interpreter(
        "test", restart_managed=False, already_restarted_units=None, scope_home=str(home))

    assert result == {"matched": [], "killed": [], "failed": []}


def test_current_interpreter_and_the_child_reap_in_process(monkeypatch):
    # This interpreter started after the checkout was written: no delegation.
    assert dashboard_procs._interpreter_predates_reap_sources() is False
    monkeypatch.setattr(psutil, "Process", lambda *a, **k: _StartedBeforeThePull())
    monkeypatch.setenv(CHILD_ENV, "1")
    assert dashboard_procs._interpreter_predates_reap_sources() is False


def test_unreadable_process_start_time_keeps_the_in_process_reap(monkeypatch):
    def boom(*a, **k):
        raise psutil.AccessDenied(pid=0)

    monkeypatch.setattr(psutil, "Process", boom)
    monkeypatch.delenv(CHILD_ENV, raising=False)
    assert dashboard_procs._interpreter_predates_reap_sources() is False


@pytest.mark.parametrize("scope_home", ["/tmp/own-home", None])
def test_child_failure_reports_a_recovery_command(monkeypatch, capsys, scope_home):
    monkeypatch.setattr(dashboard_procs.subprocess, "run", lambda cmd, **k: None)  # writes nothing

    result = dashboard_procs._reap_in_fresh_interpreter(
        "r", restart_managed=True, already_restarted_units=None, scope_home=scope_home)

    assert result == {"matched": [], "killed": [], "failed": []}
    assert "hermes dashboard --stop" in capsys.readouterr().out
