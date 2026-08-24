"""Contract tests for the generic plugin-owned worker runtime."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest


def test_clean_runner_shims_are_pytest_opt_in_only() -> None:
    if os.environ.get("HERMES_TEST_SHIMS") != "1":
        pytest.skip("Hermes test shims were not enabled for this pytest run")

    import agent.deadline
    import hermes_cli.profiles
    import hermes_constants
    from plugin_worker_runtime import PluginWorkerRuntime

    support_root = Path(__file__).resolve().parent / "support"
    for module in (agent.deadline, hermes_cli.profiles, hermes_constants):
        assert support_root in Path(module.__file__).resolve().parents
    assert PluginWorkerRuntime.__module__ == "plugin_worker_runtime"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from plugin_worker_runtime import PluginWorkerRuntime

    instance = PluginWorkerRuntime(data_root=tmp_path / "runtime-data")
    try:
        yield instance
    finally:
        instance.shutdown(reason="test_fixture_cleanup")


def worker_definition(tmp_path: Path, *, command: tuple[str, ...] | None = None):
    from plugin_worker_runtime import WorkerDefinition

    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace = workspace_root / "fixture"
    workspace.mkdir()
    return WorkerDefinition(
        definition_id="fixture-worker",
        argv=command
        or (
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1]); print(sys.argv[2])",
            "{attempt_id}",
            "{work_item_ref}",
        ),
        allowed_profiles=("default",),
        allowed_models=("default", "test-model"),
        allowed_reasoning=("low", "high", "none"),
        workspace_root=workspace_root,
        max_runtime_seconds=10,
        output_limit_bytes=512,
        cancellation_grace_seconds=1,
    )


def test_launch_rejects_renderer_controlled_command_path_and_settings(runtime, tmp_path):
    definition = worker_definition(tmp_path)
    runtime.register_definition("example", definition)

    with pytest.raises(ValueError, match="profile"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-profile",
            work_item_ref="work-1",
            workspace_relative="fixture",
            profile="other-profile",
            model="default",
            reasoning="low",
        )
    with pytest.raises(ValueError, match="workspace"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-path",
            work_item_ref="work-1",
            workspace_relative="../outside",
            profile="default",
            model="default",
            reasoning="low",
        )
    escaped_workspace = tmp_path / "outside-workspace"
    escaped_workspace.mkdir()
    with pytest.raises(ValueError, match="escapes the registered workspace root"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-absolute-path",
            work_item_ref="work-1",
            workspace_relative=str(escaped_workspace.resolve()),
            profile="default",
            model="default",
            reasoning="low",
        )
    with pytest.raises(ValueError, match="work item"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-item",
            work_item_ref="--inject=command",
            workspace_relative="fixture",
            profile="default",
            model="default",
            reasoning="low",
        )


def test_launch_rejects_an_in_root_symlink_that_resolves_outside(runtime, tmp_path):
    definition = worker_definition(tmp_path)
    outside_workspace = tmp_path / "outside-workspace"
    outside_workspace.mkdir()
    escaped_link = definition.workspace_root / "escaped"
    try:
        escaped_link.symlink_to(outside_workspace, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"directory symlinks are unavailable: {error}")
        junction = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(escaped_link), str(outside_workspace)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode:
            pytest.skip(f"directory links are unavailable: {junction.stderr or error}")
    runtime.register_definition("example", definition)

    with pytest.raises(ValueError, match="escapes the registered workspace root"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-symlink-escape",
            work_item_ref="work-1",
            workspace_relative="escaped",
            profile="default",
            model="default",
            reasoning="low",
        )

    assert runtime._processes == {}


def test_launch_allows_a_bounded_nested_workspace(runtime, tmp_path):
    definition = worker_definition(tmp_path)
    nested = definition.workspace_root / "repository" / "pr-42"
    nested.mkdir(parents=True)
    runtime.register_definition("example", definition)

    snapshot = runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-nested",
        work_item_ref="work-1",
        workspace_relative="repository/pr-42",
        profile="default",
        model="default",
        reasoning="none",
    )

    assert snapshot.status in {"running", "succeeded"}


def test_launch_persists_immutable_snapshot_and_is_idempotent(runtime, tmp_path):
    definition = worker_definition(tmp_path)
    runtime.register_definition("example", definition)

    first = runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-immutable",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="test-model",
        reasoning="high",
    )
    second = runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-immutable",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="test-model",
        reasoning="high",
    )

    assert first.attempt_id == second.attempt_id
    assert second.status in {"running", "succeeded"}
    assert second.settings == {
        "model": "test-model",
        "profile": "default",
        "reasoning": "high",
    }
    with pytest.raises(ValueError, match="different immutable launch"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-immutable",
            work_item_ref="work-2",
            workspace_relative="fixture",
            profile="default",
            model="test-model",
            reasoning="high",
        )


def test_launch_releases_the_target_only_after_durable_pid_registration(runtime, tmp_path, monkeypatch):
    marker = tmp_path / "target-ran.txt"
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
        ),
    )
    runtime.register_definition("example", definition)
    original_close_gate = runtime._close_startup_gate
    observed_starting: list[tuple[str, int | None, float | None]] = []

    def release_after_durable_record(process, *, release):
        if release:
            connection = runtime._connect()
            try:
                row = connection.execute(
                    "SELECT status, pid, pid_created_at FROM attempts "
                    "WHERE owner = ? AND attempt_id = ?",
                    ("example", "attempt-gated-release"),
                ).fetchone()
            finally:
                connection.close()
            assert row is not None
            observed_starting.append((row["status"], row["pid"], row["pid_created_at"]))
            assert not marker.exists()
        return original_close_gate(process, release=release)

    monkeypatch.setattr(runtime, "_close_startup_gate", release_after_durable_record)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-gated-release",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert observed_starting and observed_starting[0][0] == "starting"
    assert observed_starting[0][1] and observed_starting[0][2]
    assert marker.read_text(encoding="utf-8") == "ran"


def test_closed_startup_gate_never_executes_the_target(runtime, tmp_path, monkeypatch):
    marker = tmp_path / "must-not-run.txt"
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
        ),
    )
    runtime.register_definition("example", definition)
    monkeypatch.setattr(runtime, "_process_started_at", lambda _pid: None)

    with pytest.raises(RuntimeError, match="identity"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-gate-closed",
            work_item_ref="work-1",
            workspace_relative="fixture",
            profile="default",
            model="default",
            reasoning="none",
        )

    time.sleep(0.1)
    assert not marker.exists()
    assert runtime.observe("example", "attempt-gate-closed").status == "failed"


def test_cancel_does_not_signal_a_worker_that_already_exited(runtime, tmp_path, monkeypatch):
    import plugin_worker_runtime as worker_runtime

    definition = worker_definition(
        tmp_path,
        command=(sys.executable, "-c", "raise SystemExit(0)"),
    )
    runtime.register_definition("example", definition)
    monkeypatch.setattr(runtime, "_monitor", lambda *_args: None)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-ended-before-cancel",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )
    process = runtime._processes[("example", "attempt-ended-before-cancel")]
    assert process.wait(timeout=5) == 0

    def fail_if_signalled(_pid):
        raise AssertionError("an exited worker must not be signalled")

    monkeypatch.setattr(worker_runtime, "kill_process_tree", fail_if_signalled)
    snapshot = runtime.cancel(
        "example", "attempt-ended-before-cancel", reason="test cancellation"
    )

    assert snapshot.status == "cancelled"


def test_restart_reconciles_a_durable_unreleased_startup_gate(runtime, tmp_path, monkeypatch):
    marker = tmp_path / "must-not-run-before-restart.txt"
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
        ),
    )
    runtime.register_definition("example", definition)
    entered_release_window = threading.Event()
    continue_parent = threading.Event()
    original_close_gate = runtime._close_startup_gate
    failures: list[BaseException] = []

    def pause_before_release(process, *, release):
        if release:
            entered_release_window.set()
            assert continue_parent.wait(timeout=5)
        return original_close_gate(process, release=release)

    monkeypatch.setattr(runtime, "_close_startup_gate", pause_before_release)

    def launch() -> None:
        try:
            runtime.launch(
                "example",
                "fixture-worker",
                attempt_id="attempt-crash-window",
                work_item_ref="work-1",
                workspace_relative="fixture",
                profile="default",
                model="default",
                reasoning="none",
            )
        except BaseException as error:
            failures.append(error)

    launch_thread = threading.Thread(target=launch)
    launch_thread.start()
    assert entered_release_window.wait(timeout=5)
    process = runtime._processes[("example", "attempt-crash-window")]
    assert not marker.exists()

    from plugin_worker_runtime import PluginWorkerRuntime

    restarted = PluginWorkerRuntime(data_root=runtime.data_root)
    assert restarted.reconcile_startup() == 1
    assert restarted.observe("example", "attempt-crash-window").status == "indeterminate"
    assert process.wait(timeout=5) is not None
    assert not marker.exists()

    continue_parent.set()
    launch_thread.join(timeout=5)
    assert not launch_thread.is_alive()
    assert failures and isinstance(failures[0], RuntimeError)


def test_unregistration_releases_its_lock_and_cancels_active_process(runtime, tmp_path, monkeypatch):
    import plugin_worker_runtime as worker_runtime

    definition = worker_definition(
        tmp_path,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-cancel",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    entered_cancellation = threading.Event()
    release_cancellation = threading.Event()
    original_kill = worker_runtime.kill_process_tree

    def delayed_kill(pid: int) -> None:
        entered_cancellation.set()
        assert release_cancellation.wait(timeout=5)
        original_kill(pid)

    monkeypatch.setattr(worker_runtime, "kill_process_tree", delayed_kill)
    result: list[object] = []

    def unload() -> None:
        result.append(runtime.unregister_owner("example", reason="plugin_unload"))

    unload_thread = threading.Thread(target=unload)
    unload_thread.start()
    assert entered_cancellation.wait(timeout=5)

    sibling = replace(definition, definition_id="sibling-worker")
    sibling_registration = threading.Thread(
        target=runtime.register_definition,
        args=("sibling", sibling),
    )
    sibling_registration.start()
    sibling_registration.join(timeout=1)
    assert not sibling_registration.is_alive()

    release_cancellation.set()
    unload_thread.join(timeout=5)
    assert not unload_thread.is_alive()
    assert len(result) == 1
    result = result[0]
    assert result.cancelled == 1
    assert result.indeterminate == 0
    snapshot = runtime.observe("example", "attempt-cancel")
    assert snapshot.status == "cancelled"
    assert any(event.kind == "cancel_requested" for event in runtime.list_events("example", "attempt-cancel"))


@pytest.mark.parametrize("disposal", ("owner", "definition"))
def test_indeterminate_disposal_keeps_owner_admission_closed(runtime, tmp_path, monkeypatch, disposal):
    import plugin_worker_runtime as worker_runtime

    definition = worker_definition(
        tmp_path,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
    )
    runtime.register_definition("example", definition)
    monkeypatch.setattr(runtime, "_monitor", lambda *_args: None)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id=f"attempt-indeterminate-{disposal}",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )
    attempt_id = f"attempt-indeterminate-{disposal}"
    process = runtime._processes[("example", attempt_id)]

    def force_indeterminate(owner, cancelled_attempt_id, *, reason):
        del reason
        with runtime._connect() as connection:
            connection.execute(
                "UPDATE attempts SET status = 'indeterminate', terminal_at = ? "
                "WHERE owner = ? AND attempt_id = ?",
                ("test", owner, cancelled_attempt_id),
            )
        return runtime.observe(owner, cancelled_attempt_id)

    monkeypatch.setattr(runtime, "cancel", force_indeterminate)
    try:
        if disposal == "owner":
            result = runtime.unregister_owner("example", reason="plugin_unload")
        else:
            result = runtime.unregister_definition(
                "example", "fixture-worker", reason="registration_disposed"
            )

        assert result.cancelled == 1
        assert result.indeterminate == 1
        with pytest.raises(RuntimeError, match="worker admission is closed"):
            runtime.register_definition("example", definition)
    finally:
        worker_runtime.kill_process_tree(process.pid)
        process.wait(timeout=5)


def test_startup_reconciliation_reopens_admission_only_after_identity_is_gone(
    runtime, tmp_path, monkeypatch
):
    import plugin_worker_runtime as worker_runtime
    from plugin_worker_runtime import PluginWorkerRuntime

    definition = worker_definition(
        tmp_path,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
    )
    runtime.register_definition("example", definition)
    monkeypatch.setattr(runtime, "_monitor", lambda *_args: None)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-reconcile-indeterminate",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )
    process = runtime._processes[("example", "attempt-reconcile-indeterminate")]
    with runtime._connect() as connection:
        connection.execute(
            "UPDATE attempts SET status = 'indeterminate', terminal_at = ? "
            "WHERE owner = ? AND attempt_id = ?",
            ("test", "example", "attempt-reconcile-indeterminate"),
        )

    unresolved = PluginWorkerRuntime(data_root=runtime.data_root)
    monkeypatch.setattr(unresolved, "_process_identity_state", lambda *_args: None)
    assert unresolved.reconcile_startup() == 1
    with pytest.raises(RuntimeError, match="worker admission is closed"):
        unresolved.register_definition("example", definition)

    worker_runtime.kill_process_tree(process.pid)
    process.wait(timeout=5)

    recovered = PluginWorkerRuntime(data_root=runtime.data_root)
    monkeypatch.setattr(recovered, "_process_identity_state", lambda *_args: False)
    assert recovered.reconcile_startup() == 1
    recovered.register_definition("example", definition)
    assert recovered.definition("example", "fixture-worker") == definition


def test_restart_reconciliation_marks_unfinished_attempt_indeterminate(runtime, tmp_path):
    definition = worker_definition(
        tmp_path,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-restart",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )
    process = runtime._processes[("example", "attempt-restart")]
    assert any(event.kind == "released" for event in runtime.list_events("example", "attempt-restart"))

    from plugin_worker_runtime import PluginWorkerRuntime

    restarted = PluginWorkerRuntime(data_root=runtime.data_root)
    reconciled = restarted.reconcile_startup()

    assert reconciled == 1
    snapshot = restarted.observe("example", "attempt-restart")
    assert snapshot.status == "indeterminate"
    assert snapshot.status != "succeeded"
    assert process.wait(timeout=5) is not None


def test_output_is_bounded_redacted_and_terminal_event_is_immutable(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_RUNTIME_SECRET", "do-not-store-this")
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            "print('do-not-store-this'); print('x' * 128)",
        ),
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-output",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    snapshot = runtime.observe("example", "attempt-output")
    while snapshot.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.observe("example", "attempt-output")

    assert snapshot.status == "succeeded"
    events = runtime.list_events("example", "attempt-output")
    serialized = "\n".join(event.detail for event in events)
    assert "do-not-store-this" not in serialized
    assert "[REDACTED]" in serialized
    assert len(serialized.encode("utf-8")) < 1_200
    with pytest.raises(ValueError, match="terminal"):
        runtime.cancel("example", "attempt-output", reason="late-cancel")


def test_stderr_only_output_is_redacted(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_RUNTIME_SECRET", "stderr-secret-must-not-persist")
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('stderr-secret-must-not-persist\\n')",
        ),
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-stderr-secret",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    snapshot = runtime.observe("example", "attempt-stderr-secret")
    while snapshot.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.observe("example", "attempt-stderr-secret")

    assert snapshot.status == "succeeded"
    serialized = "\n".join(
        event.detail for event in runtime.list_events("example", "attempt-stderr-secret")
    )
    assert "stderr-secret-must-not-persist" not in serialized
    assert "[REDACTED]" in serialized
    assert len(serialized.encode("utf-8")) < 1_200


def _assert_fragmented_same_stream_secret_is_redacted(runtime, tmp_path, monkeypatch, stream):
    """One stream must not expose a secret split across two reader chunks."""
    secret = "fragmented-secret-must-not-persist"
    monkeypatch.setenv("WORKER_RUNTIME_SECRET", secret)
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            (
                "import sys; "
                f"secret = {secret!r}.encode('utf-8'); "
                f"stream = sys.{stream}.buffer; "
                "stream.write(b'x' * (8192 - 9) + secret[:9]); "
                "stream.flush(); "
                "stream.write(secret[9:]); stream.flush()"
            ),
        ),
    )
    definition = replace(definition, output_limit_bytes=10_000)
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id=f"attempt-fragmented-{stream}-secret",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    snapshot = runtime.observe("example", f"attempt-fragmented-{stream}-secret")
    while snapshot.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.observe("example", f"attempt-fragmented-{stream}-secret")

    assert snapshot.status == "succeeded"
    serialized = "\n".join(
        event.detail
        for event in runtime.list_events("example", f"attempt-fragmented-{stream}-secret")
    )
    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert len(serialized.encode("utf-8")) < 10_000


def test_fragmented_stdout_secret_is_never_persisted(runtime, tmp_path, monkeypatch):
    """Stdout redaction must span deterministic flushed reader boundaries."""
    _assert_fragmented_same_stream_secret_is_redacted(
        runtime, tmp_path, monkeypatch, "stdout"
    )


def test_fragmented_stderr_secret_is_never_persisted(runtime, tmp_path, monkeypatch):
    """Stderr redaction must span deterministic flushed reader boundaries."""
    _assert_fragmented_same_stream_secret_is_redacted(
        runtime, tmp_path, monkeypatch, "stderr"
    )


def test_finite_oversized_output_cannot_be_recorded_as_success(runtime, tmp_path):
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 1024); sys.stdout.flush()",
        ),
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-finite-oversize",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    snapshot = runtime.observe("example", "attempt-finite-oversize")
    while snapshot.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.observe("example", "attempt-finite-oversize")

    assert snapshot.status == "failed"
    assert snapshot.status != "succeeded"
    events = runtime.list_events("example", "attempt-finite-oversize")
    assert any(event.kind == "output_limit_exceeded" for event in events)
    assert len("\n".join(event.detail for event in events).encode("utf-8")) < 1_200


def test_finite_oversized_stderr_cannot_be_recorded_as_success(runtime, tmp_path):
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x' * 1024); sys.stderr.flush()",
        ),
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-finite-stderr-oversize",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    snapshot = runtime.observe("example", "attempt-finite-stderr-oversize")
    while snapshot.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.observe("example", "attempt-finite-stderr-oversize")

    assert snapshot.status == "failed"
    assert snapshot.status != "succeeded"
    events = runtime.list_events("example", "attempt-finite-stderr-oversize")
    assert any(event.kind == "output_limit_exceeded" for event in events)
    assert len("\n".join(event.detail for event in events).encode("utf-8")) < 1_200


def test_combined_stdout_and_stderr_enforce_one_output_limit(runtime, tmp_path):
    """The durable output cap must cover both streams together."""
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.write('o' * 300); sys.stdout.flush(); "
                "sys.stderr.write('e' * 300); sys.stderr.flush()"
            ),
        ),
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-combined-output-limit",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    snapshot = runtime.observe("example", "attempt-combined-output-limit")
    while snapshot.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.observe("example", "attempt-combined-output-limit")

    assert snapshot.status == "failed"
    events = runtime.list_events("example", "attempt-combined-output-limit")
    assert [event.kind for event in events].count("output_limit_exceeded") == 1
    output_events = [event for event in events if event.kind == "output"]
    assert len(output_events) == 1
    assert len(output_events[0].detail.encode("utf-8")) <= definition.output_limit_bytes


def test_output_cap_terminates_a_noisy_worker_before_success(runtime, tmp_path):
    definition = worker_definition(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            "import sys, time; sys.stdout.write('x' * 2_000_000); sys.stdout.flush(); time.sleep(60)",
        ),
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-noisy",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    snapshot = runtime.observe("example", "attempt-noisy")
    while snapshot.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.observe("example", "attempt-noisy")

    assert snapshot.status == "failed"
    events = runtime.list_events("example", "attempt-noisy")
    assert any(event.kind == "output_limit_exceeded" for event in events)
    assert len("\n".join(event.detail for event in events).encode("utf-8")) < 1_200


def test_configured_runtime_limit_terminates_a_silent_worker(runtime, tmp_path):
    definition = replace(
        worker_definition(
            tmp_path,
            command=(sys.executable, "-c", "import time; time.sleep(60)"),
        ),
        max_runtime_seconds=1,
    )
    runtime.register_definition("example", definition)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-runtime-limit",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    deadline = time.monotonic() + 5
    snapshot = runtime.observe("example", "attempt-runtime-limit")
    while snapshot.status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.observe("example", "attempt-runtime-limit")

    assert snapshot.status == "indeterminate"
    assert snapshot.status != "succeeded"
    events = runtime.list_events("example", "attempt-runtime-limit")
    assert any(event.kind == "timed_out" for event in events)


def test_unconfirmed_monitor_termination_keeps_owner_admission_closed(
    runtime, tmp_path, monkeypatch
):
    import plugin_worker_runtime as worker_runtime

    definition = replace(
        worker_definition(
            tmp_path,
            command=(sys.executable, "-c", "import time; time.sleep(60)"),
        ),
        max_runtime_seconds=1,
    )
    runtime.register_definition("example", definition)
    original_kill = worker_runtime.kill_process_tree
    monkeypatch.setattr(worker_runtime, "kill_process_tree", lambda _pid: None)
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-unconfirmed-monitor-termination",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )
    process = runtime._processes[("example", "attempt-unconfirmed-monitor-termination")]
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (
                runtime.observe("example", "attempt-unconfirmed-monitor-termination").status
                == "indeterminate"
            ):
                break
            time.sleep(0.05)

        assert (
            runtime.observe("example", "attempt-unconfirmed-monitor-termination").status
            == "indeterminate"
        )
        assert (
            "example",
            "attempt-unconfirmed-monitor-termination",
        ) in runtime._processes
        assert "example" in runtime._owners_closing
        sibling = replace(definition, definition_id="sibling-worker")
        with pytest.raises(RuntimeError, match="worker admission is closed"):
            runtime.register_definition("example", sibling)
    finally:
        original_kill(process.pid)
        process.wait(timeout=5)


def test_startup_reconciles_a_pidless_pre_spawn_launch_without_closing_owner(runtime, tmp_path):
    from plugin_worker_runtime import PluginWorkerRuntime

    definition = worker_definition(tmp_path)
    with runtime._connect() as connection:
        connection.execute(
            """
            INSERT INTO attempts (
                owner, attempt_id, definition_id, work_item_ref, workspace_relative,
                profile, model, reasoning, argv_json, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "example",
                "attempt-pre-spawn-crash",
                definition.definition_id,
                "work-1",
                "fixture",
                "default",
                "default",
                "none",
                "[]",
                "launching",
                "test",
            ),
        )

    restarted = PluginWorkerRuntime(data_root=runtime.data_root)

    assert restarted.reconcile_startup() == 1
    assert restarted.observe("example", "attempt-pre-spawn-crash").status == "cancelled"
    assert any(
        event.kind == "restart_pre_spawn"
        for event in restarted.list_events("example", "attempt-pre-spawn-crash")
    )
    restarted.register_definition("example", definition)
    assert restarted.definition("example", "fixture-worker") == definition


def test_closed_owner_rejects_new_worker_admission(runtime, tmp_path):
    definition = worker_definition(tmp_path)
    runtime.register_definition("example", definition)
    runtime._owners_closing.add("example")

    with pytest.raises(RuntimeError, match="admission is closed"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-closing",
            work_item_ref="work-1",
            workspace_relative="fixture",
            profile="default",
            model="default",
            reasoning="none",
        )


def test_unload_rejects_a_launch_that_started_before_the_owner_closed(runtime, tmp_path, monkeypatch):
    definition = worker_definition(tmp_path)
    runtime.register_definition("example", definition)
    entered_resolution = threading.Event()
    release_resolution = threading.Event()
    original_resolve = runtime._resolve_workspace
    failures = []

    def delayed_resolve(*args, **kwargs):
        entered_resolution.set()
        assert release_resolution.wait(timeout=5)
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(runtime, "_resolve_workspace", delayed_resolve)

    def launch() -> None:
        try:
            runtime.launch(
                "example",
                "fixture-worker",
                attempt_id="attempt-unload-race",
                work_item_ref="work-1",
                workspace_relative="fixture",
                profile="default",
                model="default",
                reasoning="none",
            )
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=launch)
    thread.start()
    assert entered_resolution.wait(timeout=5)
    runtime.unregister_owner("example", reason="plugin_unload")
    release_resolution.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "admission is closed" in str(failures[0])
    assert runtime.definition("example", "fixture-worker") is None


def test_global_shutdown_cancels_every_profile_scoped_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    import plugin_worker_runtime as worker_runtime
    from plugin_worker_runtime import (
        WorkerDefinition,
        get_plugin_worker_runtime,
        shutdown_plugin_worker_runtimes,
    )

    monkeypatch.setattr(worker_runtime, "_RUNTIME_SHUTTING_DOWN", False)
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "fixture"
    workspace.mkdir(parents=True)
    runtime = get_plugin_worker_runtime()
    runtime.register_definition(
        "example",
        WorkerDefinition(
            definition_id="fixture-worker",
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            allowed_profiles=("default",),
            allowed_models=("default",),
            allowed_reasoning=("none",),
            workspace_root=workspace_root,
            max_runtime_seconds=10,
            output_limit_bytes=512,
            cancellation_grace_seconds=1,
        ),
    )
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-global-shutdown",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )

    summary = shutdown_plugin_worker_runtimes(reason="backend_shutdown")

    assert summary.cancelled == 1
    assert runtime.observe("example", "attempt-global-shutdown").status in {"cancelled", "indeterminate"}


def test_backend_shutdown_revokes_idle_worker_definitions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    import plugin_worker_runtime as worker_runtime
    from plugin_worker_runtime import (
        WorkerDefinition,
        get_plugin_worker_runtime,
        shutdown_plugin_worker_runtimes,
    )

    monkeypatch.setattr(worker_runtime, "_RUNTIME_SHUTTING_DOWN", False)
    workspace_root = tmp_path / "workspaces"
    (workspace_root / "fixture").mkdir(parents=True)
    runtime = get_plugin_worker_runtime()
    definition = WorkerDefinition(
        definition_id="fixture-worker",
        argv=(sys.executable, "-c", "print('unexpected')"),
        allowed_profiles=("default",),
        allowed_models=("default",),
        allowed_reasoning=("none",),
        workspace_root=workspace_root,
        max_runtime_seconds=10,
        output_limit_bytes=512,
        cancellation_grace_seconds=1,
    )
    runtime.register_definition("example", definition)

    shutdown_plugin_worker_runtimes(reason="backend_shutdown")

    assert runtime.definition("example", "fixture-worker") is None
    with pytest.raises(RuntimeError, match="shutting down"):
        runtime.launch(
            "example",
            "fixture-worker",
            attempt_id="attempt-after-shutdown",
            work_item_ref="work-1",
            workspace_relative="fixture",
            profile="default",
            model="default",
            reasoning="none",
        )
    with pytest.raises(RuntimeError, match="shutting down"):
        runtime.register_definition("example", definition)


def test_global_shutdown_closes_every_retained_runtime_before_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    import plugin_worker_runtime as worker_runtime
    from plugin_worker_runtime import (
        PluginWorkerRuntime,
        WorkerDefinition,
        shutdown_plugin_worker_runtimes,
    )

    monkeypatch.setattr(worker_runtime, "_RUNTIME_SHUTTING_DOWN", False)
    first_root = tmp_path / "first-workspaces"
    second_root = tmp_path / "second-workspaces"
    (first_root / "fixture").mkdir(parents=True)
    (second_root / "fixture").mkdir(parents=True)
    first = PluginWorkerRuntime(data_root=tmp_path / "first-runtime")
    second = PluginWorkerRuntime(data_root=tmp_path / "second-runtime")
    second.register_definition(
        "example",
        WorkerDefinition(
            definition_id="fixture-worker",
            argv=(sys.executable, "-c", "print('unexpected')"),
            allowed_profiles=("default",),
            allowed_models=("default",),
            allowed_reasoning=("none",),
            workspace_root=second_root,
            max_runtime_seconds=10,
            output_limit_bytes=512,
            cancellation_grace_seconds=1,
        ),
    )
    monkeypatch.setattr(worker_runtime, "_RUNTIMES", {"first": first, "second": second})
    entered_first_cleanup = threading.Event()
    release_first_cleanup = threading.Event()
    original_shutdown = first.shutdown

    def delayed_first_shutdown(*args, **kwargs):
        entered_first_cleanup.set()
        assert release_first_cleanup.wait(timeout=5)
        return original_shutdown(*args, **kwargs)

    monkeypatch.setattr(first, "shutdown", delayed_first_shutdown)
    thread = threading.Thread(
        target=shutdown_plugin_worker_runtimes,
        kwargs={"reason": "backend_shutdown"},
    )
    thread.start()
    try:
        assert entered_first_cleanup.wait(timeout=5)
        with pytest.raises(RuntimeError, match="shutting down"):
            second.launch(
                "example",
                "fixture-worker",
                attempt_id="attempt-during-other-runtime-cleanup",
                work_item_ref="work-1",
                workspace_relative="fixture",
                profile="default",
                model="default",
                reasoning="none",
            )
    finally:
        release_first_cleanup.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_backend_shutdown_rejects_new_runtime_admission_until_process_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    import plugin_worker_runtime as worker_runtime
    from plugin_worker_runtime import (
        WorkerDefinition,
        get_plugin_worker_runtime,
        shutdown_plugin_worker_runtimes,
    )

    monkeypatch.setattr(worker_runtime, "_RUNTIME_SHUTTING_DOWN", False)
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "fixture"
    workspace.mkdir(parents=True)
    runtime = get_plugin_worker_runtime()
    runtime.register_definition(
        "example",
        WorkerDefinition(
            definition_id="fixture-worker",
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            allowed_profiles=("default",),
            allowed_models=("default",),
            allowed_reasoning=("none",),
            workspace_root=workspace_root,
            max_runtime_seconds=10,
            output_limit_bytes=512,
            cancellation_grace_seconds=1,
        ),
    )
    runtime.launch(
        "example",
        "fixture-worker",
        attempt_id="attempt-shutdown-race",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )
    entered_shutdown = threading.Event()
    release_shutdown = threading.Event()
    original_unregister = runtime.unregister_owner

    def delayed_unregister(*args, **kwargs):
        entered_shutdown.set()
        assert release_shutdown.wait(timeout=5)
        return original_unregister(*args, **kwargs)

    monkeypatch.setattr(runtime, "unregister_owner", delayed_unregister)
    thread = threading.Thread(
        target=shutdown_plugin_worker_runtimes,
        kwargs={"reason": "backend_shutdown"},
    )
    thread.start()
    try:
        assert entered_shutdown.wait(timeout=5)
        with pytest.raises(RuntimeError, match="backend is shutting down"):
            get_plugin_worker_runtime()
    finally:
        release_shutdown.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_duplicate_definition_registration_is_rejected(runtime, tmp_path):
    definition = worker_definition(tmp_path)
    runtime.register_definition("example", definition)

    with pytest.raises(ValueError, match="already registered"):
        runtime.register_definition("example", definition)

    assert runtime.definition("example", "fixture-worker") == definition


def test_definition_disposal_preserves_sibling_worker_policy(runtime, tmp_path):
    first = worker_definition(tmp_path)
    second = replace(first, definition_id="fixture-worker-two")
    runtime.register_definition("example", first)
    runtime.register_definition("example", second)

    result = runtime.unregister_definition(
        "example",
        "fixture-worker",
        reason="registration_disposed",
    )

    assert result.cancelled == 0
    assert runtime.definition("example", "fixture-worker") is None
    assert runtime.definition("example", "fixture-worker-two") == second
    launched = runtime.launch(
        "example",
        "fixture-worker-two",
        attempt_id="attempt-sibling-survives",
        work_item_ref="work-1",
        workspace_relative="fixture",
        profile="default",
        model="default",
        reasoning="none",
    )
    assert launched.definition_id == "fixture-worker-two"
