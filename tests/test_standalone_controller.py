"""Contracts for the Desktop-owned PR Autopilot worker pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pr_autopilot import CommandError, CommandRunner, Config, repository_storage_slug
from pr_reconciler import PRState, StateStore
from standalone_controller import (
    ControllerControlStore,
    StandaloneController,
    StandaloneTaskClient,
    _LeaseFencedRunner,
)


HEAD = "a" * 40
FIXED_HEAD = "b" * 40


def _git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _managed_worktree(root: Path) -> tuple[Path, str]:
    """Create the exact local worktree layout required for safe recovery."""
    repository = "AnikaWilliams/example"
    slug = repository_storage_slug(repository)
    cache = root / "repos" / slug
    workspace = root / "worktrees" / slug / "pr-42"
    cache.parent.mkdir(parents=True)
    _git(root, "init", str(cache))
    _git(cache, "config", "user.email", "test@example.invalid")
    _git(cache, "config", "user.name", "PR Autopilot Test")
    (cache / "README.md").write_text("reviewed fixture\n", encoding="utf-8")
    _git(cache, "add", "README.md")
    _git(cache, "commit", "-m", "reviewed fixture")
    expected_head = _git(cache, "rev-parse", "HEAD")
    workspace.parent.mkdir(parents=True)
    _git(cache, "worktree", "add", "--detach", str(workspace), expected_head)
    return workspace, expected_head


@dataclass(frozen=True)
class _Snapshot:
    status: str
    output_digest: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class _Event:
    kind: str
    detail: str
    created_at: str = "2026-08-22T00:00:00Z"


class _Runtime:
    def __init__(self) -> None:
        self.launches: list[dict[str, str]] = []
        self.snapshots: dict[str, _Snapshot] = {}
        self.outputs: dict[str, str] = {}

    def launch(self, owner: str, definition_id: str, **values: str) -> _Snapshot:
        self.launches.append({"owner": owner, "definition_id": definition_id, **values})
        attempt_id = values["attempt_id"]
        self.snapshots.setdefault(attempt_id, _Snapshot("running"))
        return self.snapshots[attempt_id]

    def observe(self, _owner: str, attempt_id: str) -> _Snapshot:
        return self.snapshots[attempt_id]

    def list_events(self, _owner: str, attempt_id: str, *, limit: int = 100) -> list[_Event]:
        del limit
        output = self.outputs.get(attempt_id)
        return [_Event("output", output)] if output is not None else []

    def succeed(self, attempt_id: str, *, head: str, role: str, handoff: str) -> None:
        result = json.dumps({"commit_sha": head, "role": role}, sort_keys=True)
        self.outputs[attempt_id] = f"{handoff}\nPR_AUTOPILOT_RESULT:{result}\n"
        self.snapshots[attempt_id] = _Snapshot("succeeded", "digest", 0)

    def fail(self, attempt_id: str) -> None:
        self.outputs[attempt_id] = "worker failed safely"
        self.snapshots[attempt_id] = _Snapshot("failed", "digest", 1)


class _LeaseLossRunner:
    def __init__(self) -> None:
        self.side_effect_calls: list[list[str]] = []

    def run_json(self, arguments: list[str], **_kwargs: object) -> object:
        self.side_effect_calls.append(arguments)
        return {"unsafe": "side effect ran after lease loss"}

    def run(self, arguments: list[str], **_kwargs: object) -> str:
        self.side_effect_calls.append(arguments)
        return "unsafe side effect ran after lease loss"


class _LeaseLossController:
    def __init__(self, config: Config, runner: _LeaseLossRunner) -> None:
        from pr_reconciler import StateStore

        self.config = config
        self.store = StateStore(config.state_path)
        self.runner = runner
        self.github = SimpleNamespace(runner=runner)
        self.workspaces = SimpleNamespace(runner=runner)
        self.kanban = SimpleNamespace(runner=runner)
        self.entered_run = threading.Event()
        self.allow_later_command = threading.Event()
        self.fenced = False

    def run(self, **_kwargs: object) -> list[object]:
        self.entered_run.set()
        if not self.allow_later_command.wait(timeout=3):
            raise RuntimeError("test did not release the controller command")
        try:
            self.runner.run_json(["gh", "api", "graphql"])
        except CommandError:
            self.fenced = True
        return []


class _ControlFenceController:
    def __init__(self, config: Config, runner: _LeaseLossRunner) -> None:
        from pr_reconciler import StateStore

        self.config = config
        self.store = StateStore(config.state_path)
        self.runner = runner
        self.github = SimpleNamespace(runner=runner)
        self.workspaces = SimpleNamespace(runner=runner)
        self.kanban = SimpleNamespace(runner=runner)
        self.entered_run = threading.Event()
        self.allow_stale_write = threading.Event()
        self.finished_run = threading.Event()
        self.fenced = False

    def run(self, **_kwargs: object) -> list[object]:
        self.entered_run.set()
        if not self.allow_stale_write.wait(timeout=3):
            raise RuntimeError("test did not release the controller state write")
        try:
            self.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at="2026-08-22T00:00:00Z",
                    head_sha=HEAD,
                    active_task_id="t_verify",
                    active_task_status="blocked",
                    pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                )
            )
        except CommandError:
            self.fenced = True
        finally:
            self.finished_run.set()
        return []


def _config(root: Path) -> Config:
    raw = {
        "analyzer_profile": "prtriage",
        "analyzer_skill": "pr-autopilot-analyzer",
        "analysis_task_timeout": "90m",
        "analysis_max_turns": 18,
        "worker_profile": "prfix",
        "worker_skill": "pr-autopilot-fixer",
        "fix_max_turns": 40,
        "verifier_profile": "prverify",
        "verifier_skill": "pr-autopilot-verifier",
        "verification_task_timeout": "60m",
        "verification_max_turns": 20,
        "policy_revision": 1,
        "pause_labels": ["hermes-pause", "do-not-merge"],
        "excluded_repositories": [],
        "disabled_repositories": [],
        "recovery_endpoint_hosts": ["example.invalid"],
        "recovery_model_id": "test-model",
        "recovery_api_key_env": "TEST_API_KEY",
        "recovery_api_key_file": ".test-key",
        "max_open_prs": 10,
        "max_review_rounds": 5,
        "task_timeout": "180m",
        "fix_progress_extension": "60m",
        "fix_runtime_cap": "48h",
        "task_max_retries": 0,
    }
    path = root / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return Config.load(path)


class StandaloneTaskClientTests(unittest.TestCase):
    def test_stage_definitions_and_turn_budgets_are_persisted_and_launched_by_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            definition_ids = {
                "analyze": "pr-autopilot-analyze",
                "fix": "pr-autopilot-fix",
                "verify": "pr-autopilot-verify",
            }
            config = _config(root)
            client = StandaloneTaskClient(
                config,
                runtime=runtime,
                owner="pr-autopilot",
                definition_ids=definition_ids,
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )

            connection = sqlite3.connect(config.state_path)
            try:
                rows = connection.execute(
                    "SELECT role, definition_id, max_turns FROM desktop_worker_task ORDER BY position"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                rows,
                [
                    ("analyze", "pr-autopilot-analyze", 18),
                    ("fix", "pr-autopilot-fix", 40),
                    ("verify", "pr-autopilot-verify", 20),
                ],
            )
            self.assertEqual(
                [launch["definition_id"] for launch in runtime.launches],
                ["pr-autopilot-analyze"],
            )

            runtime.succeed(
                pipeline["analyze"], head=HEAD, role="analyze", handoff="analyze evidence"
            )
            self.assertEqual(client.status(pipeline["fix"]), "running")
            runtime.succeed(
                pipeline["fix"], head=FIXED_HEAD, role="fix", handoff="fix evidence"
            )
            self.assertEqual(client.status(pipeline["verify"]), "running")

            self.assertEqual(
                [launch["definition_id"] for launch in runtime.launches],
                ["pr-autopilot-analyze", "pr-autopilot-fix", "pr-autopilot-verify"],
            )

    def test_runs_analyze_fix_verify_in_dependency_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )

            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )

            self.assertEqual([launch["profile"] for launch in runtime.launches], ["prtriage"])
            self.assertEqual(client.status(pipeline["analyze"]), "running")
            self.assertEqual(client.status(pipeline["fix"]), "scheduled")
            self.assertEqual(client.status(pipeline["verify"]), "scheduled")

            runtime.succeed(
                pipeline["analyze"], head=HEAD, role="analyze", handoff="analyze evidence"
            )
            self.assertEqual(client.status(pipeline["analyze"]), "done")
            self.assertEqual(client.status(pipeline["fix"]), "running")
            self.assertEqual([launch["profile"] for launch in runtime.launches], ["prtriage", "prfix"])

            runtime.succeed(
                pipeline["fix"], head=FIXED_HEAD, role="fix", handoff="fix evidence"
            )
            self.assertEqual(client.status(pipeline["fix"]), "done")
            self.assertEqual(client.status(pipeline["verify"]), "running")
            self.assertEqual(
                [launch["profile"] for launch in runtime.launches],
                ["prtriage", "prfix", "prverify"],
            )

            runtime.succeed(
                pipeline["verify"], head=FIXED_HEAD, role="verify", handoff="verified"
            )
            self.assertEqual(client.status(pipeline["verify"]), "done")
            details = client.details(pipeline["fix"])
            self.assertEqual(details["task"]["status"], "done")
            self.assertEqual(details["runs"][-1]["metadata"]["commit_sha"], FIXED_HEAD)
            self.assertIn("analyze evidence", client.prompt_for(pipeline["fix"]))
            self.assertIn("fix evidence", client.prompt_for(pipeline["verify"]))

    def test_running_task_without_a_durable_runtime_attempt_relaunches_idempotently(self) -> None:
        """A runtime restart must not permanently block an admitted task."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )
            analyze_id = pipeline["analyze"]
            runtime.snapshots.pop(analyze_id)

            self.assertEqual(client.status(analyze_id), "running")
            self.assertEqual(len(runtime.launches), 2)
            restarted_attempt = runtime.launches[-1]["attempt_id"]
            self.assertEqual(restarted_attempt, analyze_id)
            self.assertEqual(client.status(analyze_id), "running")
            self.assertEqual(len(runtime.launches), 2)
            self.assertEqual(runtime.launches[-1]["attempt_id"], restarted_attempt)

    def test_missing_worker_result_role_blocks_the_stage_and_its_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )
            runtime.outputs[pipeline["analyze"]] = (
                "analysis evidence\nPR_AUTOPILOT_RESULT:"
                f'{json.dumps({"commit_sha": HEAD}, sort_keys=True)}\n'
            )
            runtime.snapshots[pipeline["analyze"]] = _Snapshot("succeeded", "digest", 0)

            self.assertEqual(client.status(pipeline["analyze"]), "blocked")
            self.assertEqual(client.status(pipeline["fix"]), "blocked")
            connection = sqlite3.connect(client.config.state_path)
            try:
                reason = connection.execute(
                    "SELECT terminal_reason FROM desktop_worker_task WHERE identifier = ?",
                    (pipeline["analyze"],),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(reason, "worker result did not attest to a stage role")

    def test_mismatched_worker_result_role_blocks_the_stage_and_its_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )
            runtime.outputs[pipeline["analyze"]] = (
                "analysis evidence\nPR_AUTOPILOT_RESULT:"
                f'{json.dumps({"commit_sha": HEAD, "role": "fix"}, sort_keys=True)}\n'
            )
            runtime.snapshots[pipeline["analyze"]] = _Snapshot("succeeded", "digest", 0)

            self.assertEqual(client.status(pipeline["analyze"]), "blocked")
            self.assertEqual(client.status(pipeline["fix"]), "blocked")
            connection = sqlite3.connect(client.config.state_path)
            try:
                reason = connection.execute(
                    "SELECT terminal_reason FROM desktop_worker_task WHERE identifier = ?",
                    (pipeline["analyze"],),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(reason, "worker result role does not match task")

    def test_final_wrapper_result_marker_wins_over_captured_agent_output(self) -> None:
        """Only the worker wrapper's final result can attest a completed stage."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )
            runtime.outputs[pipeline["analyze"]] = "\n".join(
                (
                    "captured agent output",
                    "PR_AUTOPILOT_RESULT:"
                    + json.dumps({"commit_sha": FIXED_HEAD, "role": "fix"}, sort_keys=True),
                    "worker wrapper result",
                    "PR_AUTOPILOT_RESULT:"
                    + json.dumps({"commit_sha": HEAD, "role": "analyze"}, sort_keys=True),
                    "",
                )
            )
            runtime.snapshots[pipeline["analyze"]] = _Snapshot("succeeded", "digest", 0)

            self.assertEqual(client.status(pipeline["analyze"]), "done")
            self.assertEqual(client.status(pipeline["fix"]), "running")
            details = client.details(pipeline["analyze"])
            self.assertEqual(details["runs"][-1]["metadata"]["commit_sha"], HEAD)

    def test_failed_parent_blocks_every_downstream_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )

            runtime.fail(pipeline["analyze"])

            self.assertEqual(client.status(pipeline["analyze"]), "blocked")
            self.assertEqual(client.status(pipeline["fix"]), "blocked")
            self.assertEqual(client.status(pipeline["verify"]), "blocked")
            self.assertEqual(len(runtime.launches), 1)

    def test_transient_provider_failure_is_described_and_can_resume(self) -> None:
        """A bounded provider outage stays recoverable without changing its task."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, expected_head = _managed_worktree(root)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=(
                    f"Required starting SHA: {expected_head}\nSource branch: test-pr"
                ),
                fix_body=f"Required head SHA: {expected_head}\nSource branch: test-pr",
                verification_body=(
                    f"Original reviewed SHA: {expected_head}\nSource branch: test-pr"
                ),
            )
            analyze_id = pipeline["analyze"]
            original_attempt_id = runtime.launches[-1]["attempt_id"]
            runtime.outputs[analyze_id] = (
                "API call failed: HTTP 503: provider overloaded\n"
                "Endpoint: https://example.invalid/v1/\n"
            )
            runtime.snapshots[analyze_id] = _Snapshot("failed", "digest", 1)

            self.assertEqual(client.status(analyze_id), "blocked")
            details = client.details(analyze_id)
            self.assertEqual(details["task"]["block_kind"], "transient")
            self.assertEqual(details["runs"][-1]["outcome"], "provider_unavailable")
            self.assertEqual(
                details["runs"][-1]["metadata"],
                {
                    "failure_reason": "overloaded",
                    "endpoint": "https://example.invalid/v1/",
                    "automatic_recovery_count": 0,
                },
            )

            client.unblock(
                analyze_id,
                reason="PR Autopilot detected provider connectivity recovery",
            )
            self.assertEqual(client.status(analyze_id), "running")
            self.assertEqual(len(runtime.launches), 2)
            retry_attempt_id = runtime.launches[-1]["attempt_id"]
            self.assertNotEqual(retry_attempt_id, original_attempt_id)
            self.assertTrue(retry_attempt_id.startswith(f"{analyze_id}-retry-"))

    def test_transient_provider_output_has_one_durable_automatic_recovery(self) -> None:
        """Untrusted outage text cannot cause repeated stage relaunches."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, expected_head = _managed_worktree(root)
            runtime = _Runtime()
            config = _config(root)
            client = StandaloneTaskClient(
                config,
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=(
                    f"Required starting SHA: {expected_head}\nSource branch: test-pr"
                ),
                fix_body=f"Required head SHA: {expected_head}\nSource branch: test-pr",
                verification_body=(
                    f"Original reviewed SHA: {expected_head}\nSource branch: test-pr"
                ),
            )
            analyze_id = pipeline["analyze"]
            transient_output = (
                "API call failed: HTTP 503: provider overloaded\n"
                "Endpoint: https://example.invalid/v1/\n"
            )
            original_attempt_id = runtime.launches[-1]["attempt_id"]
            runtime.outputs[original_attempt_id] = transient_output
            runtime.snapshots[original_attempt_id] = _Snapshot("failed", "digest", 1)
            self.assertEqual(client.status(analyze_id), "blocked")

            client.unblock(
                analyze_id,
                reason="PR Autopilot detected provider connectivity recovery",
            )
            self.assertEqual(client.status(analyze_id), "running")
            retry_attempt_id = runtime.launches[-1]["attempt_id"]
            self.assertNotEqual(retry_attempt_id, original_attempt_id)
            runtime.outputs[retry_attempt_id] = transient_output
            runtime.snapshots[retry_attempt_id] = _Snapshot("failed", "digest", 1)
            self.assertEqual(client.status(analyze_id), "blocked")

            restarted_client = StandaloneTaskClient(
                config,
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            details = restarted_client.details(analyze_id)
            self.assertEqual(
                details["runs"][-1]["metadata"]["automatic_recovery_count"], 1
            )
            with self.assertRaisesRegex(CommandError, "automatic recovery limit"):
                restarted_client.unblock(
                    analyze_id,
                    reason="PR Autopilot detected provider connectivity recovery",
                )

            self.assertEqual(client.status(analyze_id), "blocked")
            self.assertEqual(len(runtime.launches), 2)

    def test_transient_fix_recovery_validates_the_expected_clean_worktree(self) -> None:
        """A recoverable Fix retry runs only from its bound clean starting head."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, expected_head = _managed_worktree(root)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=(
                    f"Required starting SHA: {expected_head}\nSource branch: test-pr"
                ),
                fix_body=f"Required head SHA: {expected_head}\nSource branch: test-pr",
                verification_body=(
                    f"Original reviewed SHA: {expected_head}\nSource branch: test-pr"
                ),
            )
            runtime.succeed(
                pipeline["analyze"],
                head=expected_head,
                role="analyze",
                handoff="analysis evidence",
            )
            fix_id = pipeline["fix"]
            self.assertEqual(client.status(fix_id), "running")
            runtime.outputs[fix_id] = (
                "API call failed: HTTP 503: provider overloaded\n"
                "Endpoint: https://example.invalid/v1/\n"
            )
            runtime.snapshots[fix_id] = _Snapshot("failed", "digest", 1)
            self.assertEqual(client.status(fix_id), "blocked")

            with patch("standalone_controller.subprocess.run", wraps=subprocess.run) as git_run:
                client.unblock(fix_id, reason="PR Autopilot detected provider recovery")

            self.assertEqual(_git(workspace, "rev-parse", "HEAD"), expected_head)
            self.assertEqual(_git(workspace, "status", "--porcelain"), "")
            self.assertTrue(
                any(
                    call.args[0]
                    == ["git", "-C", str(workspace), "reset", "--hard", expected_head]
                    for call in git_run.call_args_list
                ),
                git_run.call_args_list,
            )
            self.assertEqual(client.status(fix_id), "running")

    def test_transient_fix_recovery_rejects_a_dirty_worktree_without_rescheduling(self) -> None:
        """A dirty Fix workspace must remain blocked instead of being reset blindly."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, expected_head = _managed_worktree(root)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=(
                    f"Required starting SHA: {expected_head}\nSource branch: test-pr"
                ),
                fix_body=f"Required head SHA: {expected_head}\nSource branch: test-pr",
                verification_body=(
                    f"Original reviewed SHA: {expected_head}\nSource branch: test-pr"
                ),
            )
            runtime.succeed(
                pipeline["analyze"],
                head=expected_head,
                role="analyze",
                handoff="analysis evidence",
            )
            fix_id = pipeline["fix"]
            self.assertEqual(client.status(fix_id), "running")
            runtime.outputs[fix_id] = (
                "API call failed: HTTP 503: provider overloaded\n"
                "Endpoint: https://example.invalid/v1/\n"
            )
            runtime.snapshots[fix_id] = _Snapshot("failed", "digest", 1)
            self.assertEqual(client.status(fix_id), "blocked")
            (workspace / "untracked-repair.txt").write_text("do not discard\n", encoding="utf-8")
            launches_before_recovery = len(runtime.launches)

            with self.assertRaisesRegex(CommandError, "workspace cannot be safely restored"):
                client.unblock(fix_id, reason="PR Autopilot detected provider recovery")

            self.assertEqual(client.status(fix_id), "blocked")
            self.assertEqual(len(runtime.launches), launches_before_recovery)
            self.assertTrue((workspace / "untracked-repair.txt").is_file())

    def test_terminal_worker_failure_is_not_provider_recoverable(self) -> None:
        """Only attested transient provider failures can resume a blocked stage."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )
            analyze_id = pipeline["analyze"]
            runtime.fail(analyze_id)

            self.assertEqual(client.status(analyze_id), "blocked")
            details = client.details(analyze_id)
            self.assertEqual(details["task"]["block_kind"], "terminal")
            self.assertNotEqual(details["runs"][-1]["outcome"], "provider_unavailable")
            self.assertNotIn("failure_reason", details["runs"][-1]["metadata"])
            self.assertNotIn("endpoint", details["runs"][-1]["metadata"])
            with self.assertRaisesRegex(CommandError, "not a recoverable provider outage"):
                client.unblock(analyze_id, reason="provider recovered")
            self.assertEqual(client.status(analyze_id), "blocked")
            self.assertEqual(len(runtime.launches), 1)

    def test_peek_completed_parent_does_not_launch_the_scheduled_fix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )
            runtime.succeed(
                pipeline["analyze"],
                head=HEAD,
                role="analyze",
                handoff="analysis complete",
            )

            self.assertEqual(client.peek_status(pipeline["analyze"]), "done")
            self.assertEqual(client.peek_status(pipeline["fix"]), "scheduled")
            self.assertEqual(len(runtime.launches), 1)

            self.assertEqual(client.status(pipeline["fix"]), "running")
            self.assertEqual(len(runtime.launches), 2)

    def test_retire_running_stale_pipeline_blocks_only_scheduled_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )

            client.retire_pipeline(
                pipeline,
                reason="retired after the pull request head changed",
            )

            self.assertEqual(client.peek_status(pipeline["analyze"]), "running")
            self.assertEqual(client.peek_status(pipeline["fix"]), "blocked")
            self.assertEqual(client.peek_status(pipeline["verify"]), "blocked")
            self.assertEqual(len(runtime.launches), 1)

    def test_retire_complete_stale_pipeline_terminalizes_scheduled_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            pipeline = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=workspace,
                finding_fingerprint="finding-1",
                analysis_body=f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                fix_body=f"Required head SHA: {HEAD}\nSource branch: test-pr",
                verification_body=f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            )
            runtime.fail(pipeline["analyze"])
            self.assertEqual(client.status(pipeline["analyze"]), "blocked")

            client.retire_pipeline(
                pipeline,
                reason="retired after the pull request head changed",
            )

            self.assertEqual(
                [client.status(pipeline[role]) for role in ("analyze", "fix", "verify")],
                ["blocked", "blocked", "blocked"],
            )
            self.assertEqual(len(runtime.launches), 1)

    def test_same_finding_pipeline_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
            workspace.mkdir(parents=True)
            runtime = _Runtime()
            client = StandaloneTaskClient(
                _config(root),
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            values = {
                "repository": "AnikaWilliams/example",
                "number": 42,
                "workspace": workspace,
                "finding_fingerprint": "finding-1",
                "analysis_body": f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                "fix_body": f"Required head SHA: {HEAD}\nSource branch: test-pr",
                "verification_body": f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
            }

            first = client.create_pipeline(**values)
            second = client.create_pipeline(**values)

            self.assertEqual(first, second)
            self.assertEqual(len(runtime.launches), 1)

    def test_migrates_legacy_handoffs_to_an_immutable_base_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _config(root)
            legacy_base = f"Required head SHA: {HEAD}\nSource branch: test-pr"
            legacy_prompt = (
                f"{legacy_base}\n\nVerified parent-stage handoff:\nold analyze evidence\n"
            )
            config.state_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(config.state_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE desktop_worker_task (
                        identifier TEXT PRIMARY KEY,
                        role TEXT NOT NULL,
                        definition_id TEXT,
                        max_turns INTEGER,
                        prompt TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO desktop_worker_task (identifier, role, definition_id, max_turns, prompt)
                    VALUES ('legacy-fix', 'fix', 'pr-autopilot-fix', 40, ?)
                    """,
                    (legacy_prompt,),
                )
                connection.commit()
            finally:
                connection.close()

            StandaloneTaskClient(
                config,
                runtime=_Runtime(),
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )

            connection = sqlite3.connect(config.state_path)
            try:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(desktop_worker_task)")
                }
                migrated = connection.execute(
                    "SELECT base_prompt, prompt FROM desktop_worker_task WHERE identifier = 'legacy-fix'"
                ).fetchone()
            finally:
                connection.close()

            self.assertIn("base_prompt", columns)
            self.assertEqual(migrated, (legacy_base, legacy_prompt))


class ControllerControlStoreTests(unittest.TestCase):
    def test_fenced_runner_terminates_an_inflight_command_before_completion(self) -> None:
        for as_json in (False, True):
            with self.subTest(as_json=as_json), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                started = root / "started.txt"
                completed = root / "completed.txt"
                code = (
                    "from pathlib import Path; import sys, time; "
                    "Path(sys.argv[1]).write_text('started', encoding='utf-8'); "
                    "time.sleep(5); "
                    "Path(sys.argv[2]).write_text('completed', encoding='utf-8'); "
                    "print('{}')"
                )
                fence = threading.Event()
                runner = _LeaseFencedRunner(CommandRunner(), fence)
                errors: list[BaseException] = []

                def run_command() -> None:
                    try:
                        method = runner.run_json if as_json else runner.run
                        method([sys.executable, "-c", code, str(started), str(completed)])
                    except BaseException as error:
                        errors.append(error)

                thread = threading.Thread(target=run_command, daemon=True)
                thread.start()
                deadline = time.monotonic() + 3
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.exists())

                fence.set()
                thread.join(timeout=3)

                self.assertFalse(thread.is_alive())
                self.assertFalse(completed.exists())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], CommandError)
                self.assertIn("terminated", str(errors[0]))

    def test_new_lease_generation_replaces_only_a_stale_cycle_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = _config(Path(temporary_directory))
            store = StateStore(config.state_path)
            controls = ControllerControlStore(config.state_path)
            controls.set_paused(False)
            first = store.acquire_runtime_lease(
                name="standalone-controller",
                owner_id="first-backend",
                now=0,
                ttl_seconds=30,
            )
            self.assertIsNotNone(first)
            assert first is not None
            self.assertTrue(
                controls.begin_cycle(
                    owner_id=first.owner_id,
                    lease_generation=first.generation,
                    now=1,
                )
            )

            replacement = store.acquire_runtime_lease(
                name="standalone-controller",
                owner_id="replacement-backend",
                now=31,
                ttl_seconds=30,
            )
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertTrue(
                controls.begin_cycle(
                    owner_id=replacement.owner_id,
                    lease_generation=replacement.generation,
                    now=31,
                )
            )

            controls.finish_cycle(lease_generation=first.generation)
            self.assertTrue(controls.snapshot().cycle_active)
            controls.finish_cycle(lease_generation=replacement.generation)
            self.assertFalse(controls.snapshot().cycle_active)

    def test_reset_fence_stops_an_inflight_cycle_from_restoring_blocked_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _config(root)
            controller = _ControlFenceController(config, _LeaseLossRunner())
            controller.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at="2026-08-22T00:00:00Z",
                    head_sha=HEAD,
                    active_task_id="t_verify",
                    active_task_status="scheduled",
                    pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                )
            )
            from standalone_controller import ControllerService

            controls = ControllerControlStore(config.state_path)
            controls.set_paused(False)
            service = ControllerService(
                controller, interval_seconds=5, poll_seconds=1, lease_ttl_seconds=30
            )
            service.start()
            self.assertTrue(controller.entered_run.wait(timeout=3))

            connection = sqlite3.connect(config.state_path)
            try:
                connection.execute(
                    """
                    UPDATE controller_control
                    SET fence_generation = fence_generation + 1,
                        check_generation = check_generation + 1
                    WHERE singleton = 1
                    """
                )
                connection.commit()
            finally:
                connection.close()

            controller.allow_stale_write.set()
            self.assertTrue(controller.finished_run.wait(timeout=3))
            service.stop()

            state = controller.store.load("AnikaWilliams/example", 42)
            self.assertTrue(controller.fenced)
            self.assertEqual(state.active_task_status if state else None, "scheduled")

    def test_pause_fence_stops_an_inflight_cycle_from_restoring_blocked_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _config(root)
            controller = _ControlFenceController(config, _LeaseLossRunner())
            controller.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at="2026-08-22T00:00:00Z",
                    head_sha=HEAD,
                    active_task_id="t_verify",
                    active_task_status="scheduled",
                    pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                )
            )
            from standalone_controller import ControllerService

            controls = ControllerControlStore(config.state_path)
            controls.set_paused(False)
            starting_fence = controls.snapshot().fence_generation
            service = ControllerService(
                controller, interval_seconds=5, poll_seconds=1, lease_ttl_seconds=30
            )
            service.start()
            self.assertTrue(controller.entered_run.wait(timeout=3))

            paused = controls.set_paused(True)
            self.assertEqual(paused.fence_generation, starting_fence + 1)
            self.assertEqual(
                controls.set_paused(True).fence_generation,
                paused.fence_generation,
            )

            controller.allow_stale_write.set()
            self.assertTrue(controller.finished_run.wait(timeout=3))
            service.stop()

            state = controller.store.load("AnikaWilliams/example", 42)
            self.assertTrue(controller.fenced)
            self.assertEqual(state.active_task_status if state else None, "scheduled")

    def test_lease_loss_fences_the_next_controller_side_effect_during_a_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _config(root)
            runner = _LeaseLossRunner()
            controller = _LeaseLossController(config, runner)
            from standalone_controller import ControllerService

            ControllerControlStore(config.state_path).set_paused(False)
            service = ControllerService(
                controller, interval_seconds=5, poll_seconds=1, lease_ttl_seconds=30
            )
            renewals = 0
            acquire_current_lease = service._acquire_or_renew

            def acquire_then_lose_lease() -> bool:
                nonlocal renewals
                renewals += 1
                if renewals == 1:
                    return acquire_current_lease()
                return False

            with (
                patch.object(service, "_acquire_or_renew", side_effect=acquire_then_lose_lease),
                patch.object(service, "_lease_renewal_interval", return_value=0.01),
            ):
                service.start()
                self.assertTrue(controller.entered_run.wait(timeout=3))
                deadline = time.monotonic() + 3
                while service._has_lease() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(service._has_lease())
                controller.allow_later_command.set()
                thread = service._thread
                self.assertIsNotNone(thread)
                assert thread is not None
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

            self.assertTrue(controller.fenced)
            self.assertEqual(runner.side_effect_calls, [])
            connection = sqlite3.connect(config.state_path)
            try:
                cycle_count = connection.execute("SELECT count(*) FROM controller_cycle").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(cycle_count, 0)

    def test_pause_and_check_requests_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = _config(Path(temporary_directory))
            store = ControllerControlStore(config.state_path)

            initial = store.snapshot()
            store.set_paused(False)
            requested = store.request_check()

            self.assertTrue(initial.paused)
            self.assertFalse(store.snapshot().paused)
            self.assertEqual(store.snapshot().check_generation, requested)

    def test_standalone_controller_records_merge_history_without_kanban(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _config(root)
            runtime = _Runtime()
            task_client = StandaloneTaskClient(
                config,
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            controller = StandaloneController(config, task_client=task_client)
            controller.store.queue_merge_history_intent(
                repository="AnikaWilliams/example",
                number=42,
                title="Test PR",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                intended_at="2026-08-22T00:00:00Z",
            )
            controller.store.confirm_merge_history(
                "AnikaWilliams/example",
                42,
                head_sha=HEAD,
                merged_at="2026-08-22T00:00:01Z",
            )

            events = controller._publish_pending_merge_history(dry_run=False)

            self.assertEqual(len(events), 1)
            self.assertIn("local merge audit", events[0].message)
            self.assertEqual(controller.store.pending_merge_history(), [])

    def test_controller_service_thread_releases_its_lease_on_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _config(root)
            runtime = _Runtime()
            task_client = StandaloneTaskClient(
                config,
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            controller = StandaloneController(config, task_client=task_client)
            from standalone_controller import ControllerService

            ControllerControlStore(config.state_path).set_paused(False)
            service = ControllerService(
                controller, interval_seconds=5, poll_seconds=1, lease_ttl_seconds=30
            )
            with patch.object(controller, "run", return_value=[]) as run_controller:
                service.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    connection = sqlite3.connect(config.state_path)
                    try:
                        cycles = connection.execute(
                            "SELECT count(*) FROM controller_cycle"
                        ).fetchone()[0]
                    finally:
                        connection.close()
                    if cycles:
                        break
                    time.sleep(0.05)
                service.stop()

            self.assertGreater(cycles, 0)
            run_controller.assert_called_with(
                dry_run=False,
                verbose=True,
                force=True,
            )
            connection = sqlite3.connect(config.state_path)
            try:
                expires_at = connection.execute(
                    "SELECT expires_at FROM runtime_lease WHERE name = 'standalone-controller'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(expires_at, 0)

    def test_controller_service_stop_fences_and_waits_for_an_inflight_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _config(root)
            runtime = _Runtime()
            task_client = StandaloneTaskClient(
                config,
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            runner = _LeaseLossRunner()
            controller = StandaloneController(config, task_client=task_client, runner=runner)
            from standalone_controller import ControllerService

            ControllerControlStore(config.state_path).set_paused(False)
            service = ControllerService(
                controller, interval_seconds=5, poll_seconds=1, lease_ttl_seconds=30
            )
            command_entered = threading.Event()
            command_cancelled = threading.Event()
            allow_command_exit = threading.Event()
            command_fenced = threading.Event()
            heartbeat_stopped = threading.Event()
            stop_returned = threading.Event()

            def cancellable_command(arguments: list[str], **_kwargs: object) -> object:
                runner.side_effect_calls.append(arguments)
                command_entered.set()
                if not service._stop_event.wait(timeout=3):
                    raise RuntimeError("test did not stop the in-flight command")
                command_cancelled.set()
                if not allow_command_exit.wait(timeout=3):
                    raise RuntimeError("test did not release the cancelled command")
                return {"cancelled": True}

            runner.run_json = cancellable_command  # type: ignore[method-assign]

            def run_fenced_command(**_kwargs: object) -> list[object]:
                try:
                    controller.runner.run_json(["gh", "api", "graphql"])
                except CommandError:
                    command_fenced.set()
                    raise
                raise AssertionError("the stop fence allowed the command to complete")

            original_heartbeat = service._renew_lease_during_cycle

            def record_heartbeat(*args: object) -> None:
                try:
                    original_heartbeat(*args)  # type: ignore[arg-type]
                finally:
                    heartbeat_stopped.set()

            def stop_service() -> None:
                service.stop()
                stop_returned.set()

            with (
                patch.object(controller, "run", side_effect=run_fenced_command),
                patch.object(service, "_renew_lease_during_cycle", side_effect=record_heartbeat),
            ):
                service.start()
                self.assertTrue(command_entered.wait(timeout=3))
                thread = service._thread
                self.assertIsNotNone(thread)
                assert thread is not None
                stop_thread = threading.Thread(target=stop_service)
                stop_thread.start()
                self.assertTrue(command_cancelled.wait(timeout=3))
                self.assertFalse(stop_returned.is_set())
                self.assertTrue(thread.is_alive())

                allow_command_exit.set()
                stop_thread.join(timeout=3)
                self.assertFalse(stop_thread.is_alive())
                self.assertFalse(thread.is_alive())

            self.assertTrue(command_fenced.is_set())
            self.assertTrue(heartbeat_stopped.is_set())
            self.assertEqual(runner.side_effect_calls, [["gh", "api", "graphql"]])
            connection = sqlite3.connect(config.state_path)
            try:
                expires_at = connection.execute(
                    "SELECT expires_at FROM runtime_lease WHERE name = 'standalone-controller'"
                ).fetchone()[0]
                cycle_count = connection.execute("SELECT count(*) FROM controller_cycle").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(expires_at, 0)
            self.assertEqual(cycle_count, 0)

    def test_controller_service_renews_lease_during_an_inflight_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = _config(root)
            runtime = _Runtime()
            task_client = StandaloneTaskClient(
                config,
                runtime=runtime,
                owner="pr-autopilot",
                definition_id="pr-autopilot-worker",
            )
            controller = StandaloneController(config, task_client=task_client)
            from standalone_controller import ControllerService

            ControllerControlStore(config.state_path).set_paused(False)
            service = ControllerService(
                controller, interval_seconds=5, poll_seconds=1, lease_ttl_seconds=30
            )
            started = threading.Event()
            allow_exit = threading.Event()
            clock = [100]

            def slow_run(**_kwargs: object) -> list[object]:
                started.set()
                self.assertTrue(allow_exit.wait(timeout=3))
                return []

            with (
                patch("standalone_controller.time.time", side_effect=lambda: clock[0]),
                patch.object(service, "_lease_renewal_interval", return_value=0.01),
                patch.object(controller, "run", side_effect=slow_run),
            ):
                service.start()
                self.assertTrue(started.wait(timeout=3))
                clock[0] = 110
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    connection = sqlite3.connect(config.state_path)
                    try:
                        expires_at = connection.execute(
                            "SELECT expires_at FROM runtime_lease WHERE name = 'standalone-controller'"
                        ).fetchone()[0]
                    finally:
                        connection.close()
                    if expires_at == 140:
                        break
                    time.sleep(0.01)
                self.assertEqual(expires_at, 140)
                self.assertIsNone(
                    controller.store.acquire_runtime_lease(
                        name="standalone-controller",
                        owner_id="replacement",
                        now=131,
                        ttl_seconds=30,
                    )
                )

                allow_exit.set()
                service.stop()
                connection = sqlite3.connect(config.state_path)
                try:
                    expires_at = connection.execute(
                        "SELECT expires_at FROM runtime_lease WHERE name = 'standalone-controller'"
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(expires_at, 0)


if __name__ == "__main__":
    unittest.main()
