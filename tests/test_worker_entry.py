"""Safety checks for the fixed PR Autopilot worker entry point."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import worker_entry
from standalone_controller import StandaloneTaskClient
from tests.test_standalone_controller import _Runtime, _config
from worker_entry import (
    AgentExecution,
    StageVerdict,
    WorkerContractError,
    WorkerResult,
    _default_agent,
    execute_task,
    main,
)


def _git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


class _AgentProcess:
    """A bounded byte stream returned by the mocked Hermes child process."""

    def __init__(self, output: bytes, exit_code: int = 0) -> None:
        self.stdout = BytesIO(output)
        self._exit_code = exit_code

    def wait(self) -> int:
        return self._exit_code


class WorkerEntryTests(unittest.TestCase):
    @staticmethod
    def _successful_execution(role: str) -> AgentExecution:
        """Return the one accepted verdict for a successful stage fixture."""
        return AgentExecution(
            exit_code=0,
            verdict=StageVerdict(role=role, status="success", ready=role == "verify"),
        )

    @staticmethod
    def _verdict_output(
        role: str, status: str = "success", *, ready: bool | None = None
    ) -> bytes:
        """Return one protocol line for the streamed default-agent contract."""
        payload: dict[str, object] = {"role": role, "status": status}
        if ready is not None:
            payload["ready"] = ready
        return (
            "PR_AUTOPILOT_STAGE_VERDICT:"
            + json.dumps(payload, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")

    def _assert_agent_environment_is_remote_and_credential_free(
        self, environment: dict[str, str]
    ) -> None:
        """The Hermes child cannot obtain a remote or inherited credentials."""
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "4")
        self.assertEqual(
            [
                (environment[f"GIT_CONFIG_KEY_{index}"], environment[f"GIT_CONFIG_VALUE_{index}"])
                for index in range(4)
            ],
            [
                ("remote.origin.url", "pr-autopilot-remote-disabled://agent"),
                ("remote.origin.pushurl", "pr-autopilot-push-disabled://agent"),
                ("credential.helper", ""),
                ("http.extraHeader", ""),
            ],
        )
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        for name in (
            "GH_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_TOKEN",
            "GIT_ASKPASS",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "SSH_ASKPASS",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
        ):
            self.assertNotIn(name, environment)

    def _assert_trusted_hook_free_push(
        self,
        guarded_git: object,
        workspace: Path,
        expected_head: str,
        trusted_push_url: str,
    ) -> None:
        """The wrapper must disable hooks before its one leased push."""
        push_calls = [
            call.args
            for call in guarded_git.call_args_list  # type: ignore[attr-defined]
            if "push" in call.args[1:]
        ]
        self.assertEqual(len(push_calls), 1, push_calls)
        arguments = push_calls[0]
        push_index = arguments.index("push")
        self.assertEqual(arguments[0], workspace.resolve())
        self.assertEqual(arguments[push_index - 2], "-c")
        self.assertTrue(str(arguments[push_index - 1]).startswith("core.hooksPath="))
        self.assertEqual(arguments[push_index + 1], "--no-verify")
        self.assertEqual(
            arguments[push_index + 2 :],
            (
                f"--force-with-lease=refs/heads/test-pr:{expected_head}",
                trusted_push_url,
                "HEAD:refs/heads/test-pr",
            ),
        )

    def _analyze_task(self, root: Path) -> tuple[Path, Path, str]:
        workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
        workspace.mkdir(parents=True)
        _git(workspace, "init")
        _git(workspace, "config", "user.email", "test@example.invalid")
        _git(workspace, "config", "user.name", "PR Autopilot Test")
        (workspace / "README.md").write_text("fixture\n", encoding="utf-8")
        _git(workspace, "add", "README.md")
        _git(workspace, "commit", "-m", "fixture")
        head = _git(workspace, "rev-parse", "HEAD")
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
            analysis_body=f"Required starting SHA: {head}\nSource branch: test-pr",
            fix_body=f"Required head SHA: {head}\nSource branch: test-pr",
            verification_body=f"Original reviewed SHA: {head}\nSource branch: test-pr",
        )
        return config.state_path, workspace, pipeline["analyze"]

    def _fix_task(self, root: Path) -> tuple[Path, Path, str, str, str]:
        workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
        workspace.mkdir(parents=True)
        origin = root / "origin.git"
        _git(root, "init", "--bare", str(origin))
        _git(workspace, "init")
        _git(workspace, "config", "user.email", "test@example.invalid")
        _git(workspace, "config", "user.name", "PR Autopilot Test")
        (workspace / "README.md").write_text("base fixture\n", encoding="utf-8")
        _git(workspace, "add", "README.md")
        _git(workspace, "commit", "-m", "base fixture")
        base_head = _git(workspace, "rev-parse", "HEAD")
        (workspace / "README.md").write_text("reviewed fixture\n", encoding="utf-8")
        _git(workspace, "add", "README.md")
        _git(workspace, "commit", "-m", "reviewed fixture")
        expected_head = _git(workspace, "rev-parse", "HEAD")
        _git(workspace, "remote", "add", "origin", str(origin))
        _git(workspace, "push", "origin", "HEAD:refs/heads/test-pr")
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
            analysis_body=f"Required starting SHA: {expected_head}\nSource branch: test-pr",
            fix_body=f"Required head SHA: {expected_head}\nSource branch: test-pr",
            verification_body=f"Original reviewed SHA: {expected_head}\nSource branch: test-pr",
        )
        return config.state_path, workspace, pipeline["fix"], base_head, expected_head

    def _verify_task(self, root: Path) -> tuple[Path, Path, str, str, str]:
        workspace = root / "worktrees" / "AnikaWilliams__example" / "pr-42"
        workspace.mkdir(parents=True)
        origin = root / "origin.git"
        _git(root, "init", "--bare", str(origin))
        _git(workspace, "init")
        _git(workspace, "config", "user.email", "test@example.invalid")
        _git(workspace, "config", "user.name", "PR Autopilot Test")
        (workspace / "README.md").write_text("reviewed fixture\n", encoding="utf-8")
        _git(workspace, "add", "README.md")
        _git(workspace, "commit", "-m", "reviewed fixture")
        expected_head = _git(workspace, "rev-parse", "HEAD")
        (workspace / "README.md").write_text("repaired fixture\n", encoding="utf-8")
        _git(workspace, "add", "README.md")
        _git(workspace, "commit", "-m", "repair fixture")
        repaired_head = _git(workspace, "rev-parse", "HEAD")
        _git(workspace, "remote", "add", "origin", str(origin))
        _git(workspace, "push", "origin", "HEAD:refs/heads/test-pr")
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
            analysis_body=f"Required starting SHA: {expected_head}\nSource branch: test-pr",
            fix_body=f"Required head SHA: {expected_head}\nSource branch: test-pr",
            verification_body=f"Original reviewed SHA: {expected_head}\nSource branch: test-pr",
        )
        return config.state_path, workspace, pipeline["verify"], expected_head, repaired_head

    def test_analyze_enforces_read_only_exact_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id = self._analyze_task(Path(temporary_directory))
            prompts: list[tuple[str, str]] = []

            result = execute_task(
                database=database,
                task_id=task_id,
                workspace=workspace,
                run_agent=lambda prompt, skill, _workspace, _max_turns: prompts.append(
                    (prompt, skill)
                )
                or self._successful_execution("analyze"),
            )

            self.assertEqual(result.commit_sha, _git(workspace, "rev-parse", "HEAD"))
            self.assertEqual(result.role, "analyze")
            self.assertIn("Required starting SHA", prompts[0][0])
            self.assertEqual(prompts[0][1], "pr-autopilot-analyzer")

    def test_analyze_rejects_a_mismatched_head_before_the_agent_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id = self._analyze_task(Path(temporary_directory))
            (workspace / "README.md").write_text("advanced fixture\n", encoding="utf-8")
            _git(workspace, "add", "README.md")
            _git(workspace, "commit", "-m", "advance fixture")
            agent_calls: list[tuple[str, str, Path]] = []

            with self.assertRaisesRegex(WorkerContractError, "required exact head"):
                execute_task(
                    database=database,
                    task_id=task_id,
                    workspace=workspace,
                    run_agent=lambda prompt, skill, target, _max_turns: agent_calls.append(
                        (prompt, skill, target)
                    )
                    or 0,
                )

            self.assertEqual(agent_calls, [])

    def test_analyze_and_verify_reject_preexisting_worktree_residue(self) -> None:
        """Read-only stages must start clean before the agent gets control."""
        for role, build_task in (
            ("analyze", self._analyze_task),
            ("verify", self._verify_task),
        ):
            for residue in ("unstaged", "untracked"):
                with self.subTest(role=role, residue=residue), tempfile.TemporaryDirectory() as temporary_directory:
                    values = build_task(Path(temporary_directory))
                    database, workspace, task_id = values[:3]
                    if residue == "unstaged":
                        (workspace / "README.md").write_text(
                            "uncommitted fixture\n", encoding="utf-8"
                        )
                    else:
                        (workspace / "untracked.txt").write_text(
                            "fixture\n", encoding="utf-8"
                        )
                    agent_calls: list[tuple[object, ...]] = []

                    with self.assertRaisesRegex(
                        WorkerContractError, "not clean before the stage"
                    ):
                        execute_task(
                            database=database,
                            task_id=task_id,
                            workspace=workspace,
                            run_agent=lambda *arguments: agent_calls.append(arguments) or 0,
                        )

                    self.assertEqual(agent_calls, [])

    def test_analyze_rejects_a_successful_agent_that_mutates_the_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id = self._analyze_task(Path(temporary_directory))

            def mutate(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                (target / "unexpected.txt").write_text("unsafe\n", encoding="utf-8")
                return self._successful_execution("analyze")

            with self.assertRaisesRegex(WorkerContractError, "modified the worktree"):
                execute_task(
                    database=database,
                    task_id=task_id,
                    workspace=workspace,
                    run_agent=mutate,
                )

    def test_analyze_rejects_a_clean_commit_created_by_the_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id = self._analyze_task(Path(temporary_directory))

            def commit(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                (target / "README.md").write_text("changed by analyzer\n", encoding="utf-8")
                _git(target, "add", "README.md")
                _git(target, "commit", "-m", "unexpected analyzer commit")
                return self._successful_execution("analyze")

            with self.assertRaisesRegex(WorkerContractError, "changed the exact head"):
                execute_task(
                    database=database,
                    task_id=task_id,
                    workspace=workspace,
                    run_agent=commit,
                )
            self.assertEqual(_git(workspace, "status", "--porcelain"), "")

    def test_execute_task_passes_the_persisted_turn_budget_to_the_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id = self._analyze_task(Path(temporary_directory))
            received: list[int] = []

            execute_task(
                database=database,
                task_id=task_id,
                workspace=workspace,
                run_agent=lambda _prompt, _skill, _target, max_turns: received.append(
                    max_turns
                )
                or self._successful_execution("analyze"),
            )

            self.assertEqual(received, [18])

    def test_default_agent_uses_the_supported_chat_turn_budget_argument(self) -> None:
        workspace = Path("C:/worker-fixture")
        with patch(
            "worker_entry._spawn_agent_process",
            return_value=_AgentProcess(self._verdict_output("analyze")),
        ) as launch:
            self.assertEqual(
                _default_agent("Inspect the pull request", "pr-autopilot-analyzer", workspace, 18),
                self._successful_execution("analyze"),
            )

        arguments = launch.call_args.args[0]
        self.assertEqual(
            arguments,
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "chat",
                "--query",
                "Inspect the pull request",
                "--skills",
                "pr-autopilot-analyzer",
                "--toolsets",
                "file,terminal,gortex",
                "--in",
                str(workspace),
                "--max-turns",
                "18",
                "--quiet",
            ],
        )
        self._assert_agent_environment_is_remote_and_credential_free(
            launch.call_args.args[2]
        )

    def test_analyze_and_verify_agent_processes_have_only_constrained_commands(
        self,
    ) -> None:
        """Read-only stages can inspect and validate, but cannot bypass command safety."""
        workspace = Path("C:/worker-fixture")
        for role, skill in (
            ("analyze", "pr-autopilot-analyzer"),
            ("verify", "pr-autopilot-verifier"),
        ):
            with self.subTest(role=role):
                with patch(
                    "worker_entry._spawn_agent_process",
                    return_value=_AgentProcess(
                        self._verdict_output(role, ready=True if role == "verify" else None)
                    ),
                ) as launch:
                    self.assertEqual(
                        _default_agent("Inspect", skill, workspace, 18),
                        self._successful_execution(role),
                    )

                arguments = launch.call_args.args[0]
                self.assertIn("--toolsets", arguments)
                self.assertEqual(
                    arguments[arguments.index("--toolsets") + 1],
                    "file,terminal,gortex",
                )
                self.assertNotIn("--yolo", arguments)
                self._assert_agent_environment_is_remote_and_credential_free(
                    launch.call_args.args[2]
                )

    def test_analyze_and_verify_cannot_push_with_the_scrubbed_visible_remote(self) -> None:
        """A child can only see a disabled push URL, even through git -c."""
        for role in ("analyze", "verify"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary_directory:
                database, workspace, _task_id, _expected_head, remote_head = self._verify_task(
                    Path(temporary_directory)
                )
                del database
                (workspace / "README.md").write_text("local agent change\n", encoding="utf-8")
                _git(workspace, "add", "README.md")
                _git(workspace, "commit", "-m", "local agent change")
                environment = worker_entry._agent_environment(role)
                visible = subprocess.run(
                    ["git", "-C", str(workspace), "config", "--get", "remote.origin.pushurl"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )

                self.assertEqual(visible.returncode, 0)
                visible_pushurl = visible.stdout.strip()
                self.assertEqual(visible_pushurl, "pr-autopilot-push-disabled://agent")
                attempted_push = subprocess.run(
                    [
                        "git",
                        "-c",
                        f"remote.origin.pushurl={visible_pushurl}",
                        "-C",
                        str(workspace),
                        "push",
                        "origin",
                        "HEAD:refs/heads/test-pr",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )

                self.assertNotEqual(attempted_push.returncode, 0)
                self.assertEqual(
                    _git(workspace, "--git-dir", str(Path(temporary_directory) / "origin.git"), "rev-parse", "refs/heads/test-pr"),
                    remote_head,
                )

    def test_fix_agent_uses_constrained_commands_without_yolo(self) -> None:
        workspace = Path("C:/worker-fixture")
        with patch(
            "worker_entry._spawn_agent_process",
            return_value=_AgentProcess(self._verdict_output("fix")),
        ) as launch:
            self.assertEqual(
                _default_agent("Repair", "pr-autopilot-fixer", workspace, 40),
                self._successful_execution("fix"),
            )

        arguments = launch.call_args.args[0]
        self.assertIn("--toolsets", arguments)
        self.assertEqual(
            arguments[arguments.index("--toolsets") + 1],
            "file,terminal,gortex",
        )
        self.assertNotIn("--yolo", arguments)
        self._assert_agent_environment_is_remote_and_credential_free(
            launch.call_args.args[2]
        )

    def test_default_agent_requires_one_valid_role_matching_verdict(self) -> None:
        """A zero-exit stage cannot pass without one exact success verdict."""
        builders = {
            "analyze": self._analyze_task,
            "fix": self._fix_task,
            "verify": self._verify_task,
        }
        for role, build_task in builders.items():
            wrong_role = "fix" if role != "fix" else "analyze"
            cases = (
                ("no verdict", b"Hermes completed without a verdict\n", "structured stage verdict"),
                (
                    "malformed verdict",
                    b"PR_AUTOPILOT_STAGE_VERDICT:{not-json}\n",
                    "stage verdict is malformed",
                ),
                (
                    "duplicate verdict lines",
                    self._verdict_output(role, ready=True if role == "verify" else None)
                    + self._verdict_output(role, ready=True if role == "verify" else None),
                    "duplicate stage verdicts",
                ),
                (
                    "duplicate verdict field",
                    (
                        "PR_AUTOPILOT_STAGE_VERDICT:"
                        f'{{"role":"{role}","role":"{role}","status":"success"}}\n'
                    ).encode("utf-8"),
                    "stage verdict is malformed",
                ),
                (
                    "wrong verdict role",
                    self._verdict_output(
                        wrong_role, ready=True if wrong_role == "verify" else None
                    ),
                    "verdict role does not match task",
                ),
                (
                    "blocked verdict",
                    self._verdict_output(role, "blocked"),
                    "reported blocked",
                ),
                (
                    "failure verdict",
                    self._verdict_output(role, "failure"),
                    "reported failure",
                ),
            )
            if role == "verify":
                cases += (
                    (
                        "readiness rejected",
                        self._verdict_output("verify", ready=False),
                        "stage verdict is malformed",
                    ),
                )
            for name, output, error in cases:
                with self.subTest(role=role, case=name), tempfile.TemporaryDirectory() as temporary_directory:
                    values = build_task(Path(temporary_directory))
                    database, workspace, task_id = values[:3]
                    head_before = _git(workspace, "rev-parse", "HEAD")
                    with (
                        redirect_stdout(StringIO()),
                        patch(
                            "worker_entry._spawn_agent_process",
                            return_value=_AgentProcess(output),
                        ),
                        patch("worker_entry._git", wraps=worker_entry._git) as guarded_git,
                        self.assertRaisesRegex(WorkerContractError, error),
                    ):
                        execute_task(database=database, task_id=task_id, workspace=workspace)

                    if role == "fix":
                        self.assertFalse(
                            any("push" in call.args[1:] for call in guarded_git.call_args_list),
                            guarded_git.call_args_list,
                        )
                        self.assertFalse(
                            any("commit" in call.args[1:] for call in guarded_git.call_args_list),
                            guarded_git.call_args_list,
                        )
                    if role == "verify":
                        self.assertEqual(_git(workspace, "rev-parse", "HEAD"), head_before)

    def test_valid_default_agent_verdicts_allow_each_stage_wrapper_flow(self) -> None:
        """Only a valid role verdict allows the trusted stage wrapper to proceed."""
        for role, build_task in (
            ("analyze", self._analyze_task),
            ("fix", self._fix_task),
            ("verify", self._verify_task),
        ):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary_directory:
                values = build_task(Path(temporary_directory))
                database, workspace, task_id = values[:3]

                def launch_child(*_args: object, **_kwargs: object) -> _AgentProcess:
                    if role == "fix":
                        (workspace / "README.md").write_text(
                            "repair from a successful verdict\n", encoding="utf-8"
                        )
                    return _AgentProcess(
                        b"stage progress\n"
                        + self._verdict_output(
                            role, ready=True if role == "verify" else None
                        )
                    )

                with redirect_stdout(StringIO()), patch(
                    "worker_entry._spawn_agent_process", side_effect=launch_child
                ):
                    result = execute_task(database=database, task_id=task_id, workspace=workspace)

                self.assertEqual(result.role, role)
                self.assertEqual(result.commit_sha, _git(workspace, "rev-parse", "HEAD"))
                if role == "fix":
                    remote_head = _git(
                        workspace, "ls-remote", "--heads", "origin", "refs/heads/test-pr"
                    ).split()[0]
                    self.assertEqual(remote_head, result.commit_sha)

    def test_every_stage_rejects_agent_local_git_configuration_without_running_it(self) -> None:
        """Post-agent wrapper commands must not execute repository Git configuration."""

        for role, build_task in (
            ("analyze", self._analyze_task),
            ("fix", self._fix_task),
            ("verify", self._verify_task),
        ):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                database, workspace, task_id, *_remaining = build_task(root)
                sentinel_marker = root / "agent-git-config-sentinel-ran.txt"
                sentinel = root / "agent-git-config-sentinel.sh"
                sentinel.write_text(
                    "#!/bin/sh\n"
                    f"printf 'agent Git configuration ran\\n' >> {shlex.quote(sentinel_marker.as_posix())}\n"
                    "cat\n",
                    encoding="utf-8",
                    newline="\n",
                )
                sentinel.chmod(0o755)
                post_agent_call_start: list[int] = []

                with patch("worker_entry._git", wraps=worker_entry._git) as guarded_git:

                    def plant_hostile_configuration(
                        _prompt: str, _skill: str, target: Path, _max_turns: int
                    ) -> AgentExecution:
                        post_agent_call_start.append(len(guarded_git.call_args_list))
                        _git(target, "config", "--local", "core.fsmonitor", str(sentinel))
                        _git(
                            target,
                            "config",
                            "--local",
                            "credential.helper",
                            f"!{sentinel}",
                        )
                        _git(
                            target,
                            "config",
                            "--local",
                            "filter.autopilot-sentinel.clean",
                            str(sentinel),
                        )
                        if role == "fix":
                            (target / ".gitattributes").write_text(
                                "README.md filter=autopilot-sentinel\n",
                                encoding="utf-8",
                            )
                            (target / "README.md").write_text(
                                "repair that must not run the filter\n", encoding="utf-8"
                            )
                        return self._successful_execution(role)

                    with self.assertRaisesRegex(
                        WorkerContractError, "changed trusted local Git configuration"
                    ):
                        execute_task(
                            database=database,
                            task_id=task_id,
                            workspace=workspace,
                            run_agent=plant_hostile_configuration,
                        )

                self.assertFalse(sentinel_marker.exists())
                self.assertEqual(len(post_agent_call_start), 1)
                post_agent_calls = [
                    call.args[1:]
                    for call in guarded_git.call_args_list[post_agent_call_start[0] :]
                ]
                self.assertEqual(
                    post_agent_calls,
                    [("config", "--local", "--includes", "--null", "--list")],
                )

    @unittest.skipUnless(os.name == "nt", "the GitHub CLI isolation fixture uses a cmd shim")
    def test_each_agent_uses_an_empty_ephemeral_github_cli_config(self) -> None:
        """No worker stage can read a parent GitHub CLI credential or mutate through it."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            commands = root / "commands"
            commands.mkdir()
            mutation_marker = root / "gh-mutation-ran.txt"
            (commands / "gh.cmd").write_text(
                "@echo off\r\n"
                "setlocal\r\n"
                "set \"CONFIG_DIR=%GH_CONFIG_DIR%\"\r\n"
                "if \"%CONFIG_DIR%\"==\"\" set \"CONFIG_DIR=%XDG_CONFIG_HOME%\\gh\"\r\n"
                "if \"%CONFIG_DIR%\"==\"\" set \"CONFIG_DIR=%HOME%\\.config\\gh\"\r\n"
                "if not exist \"%CONFIG_DIR%\\hosts.yml\" exit /b 1\r\n"
                "findstr /c:\"parent-gh-token\" \"%CONFIG_DIR%\\hosts.yml\" >nul\r\n"
                "if errorlevel 1 exit /b 1\r\n"
                "if /i \"%~1 %~2\"==\"auth token\" (\r\n"
                "  type \"%CONFIG_DIR%\\hosts.yml\"\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "if /i \"%~1\"==\"api\" (\r\n"
                "  echo mutation > \"%GH_TEST_MUTATION_MARKER%\"\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 1\r\n",
                encoding="utf-8",
            )
            parent_config = root / "parent-gh"
            xdg_config = root / "xdg"
            home = root / "home"
            for directory in (
                parent_config,
                xdg_config / "gh",
                home / ".config" / "gh",
            ):
                directory.mkdir(parents=True)
                (directory / "hosts.yml").write_text(
                    "github.com:\n  oauth_token: parent-gh-token\n", encoding="utf-8"
                )
            hermes_home = root / "hermes"
            (hermes_home / "profiles" / "prfix").mkdir(parents=True)
            parent_environment = {
                "GH_CONFIG_DIR": str(parent_config),
                "XDG_CONFIG_HOME": str(xdg_config),
                "HOME": str(home),
                "GH_TOKEN": "parent-environment-token",
                "HERMES_HOME": str(hermes_home),
                "HERMES_PROFILE": "prfix",
                "GH_TEST_MUTATION_MARKER": str(mutation_marker),
                "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
            }
            isolated_config_paths: list[Path] = []
            real_popen = subprocess.Popen

            def run_gh(arguments: list[str], environment: dict[str, str]) -> SimpleNamespace:
                process = real_popen(
                    arguments,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, stderr = process.communicate()
                return SimpleNamespace(
                    returncode=process.returncode,
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                )

            def inspect_agent_child(
                arguments: object, _workspace: object, child_environment: object
            ) -> _AgentProcess:
                self.assertIsInstance(child_environment, dict)
                environment = child_environment
                isolated_config = Path(environment["GH_CONFIG_DIR"])
                isolated_config_paths.append(isolated_config)
                self.assertTrue(isolated_config.is_dir())
                self.assertEqual(list(isolated_config.iterdir()), [])
                self.assertNotEqual(isolated_config, parent_config)
                self.assertEqual(environment["HERMES_HOME"], str(hermes_home))
                self.assertEqual(environment["HERMES_PROFILE"], "prfix")
                self.assertTrue((Path(environment["HERMES_HOME"]) / "profiles" / "prfix").is_dir())
                self.assertNotIn("GH_TOKEN", environment)

                token = run_gh(
                    ["gh", "auth", "token"],
                    environment,
                )
                self.assertNotEqual(token.returncode, 0)
                self.assertNotIn("parent-gh-token", token.stdout + token.stderr)
                mutation = run_gh(
                    ["gh", "api", "--method", "POST", "repos/example/unsafe"],
                    environment,
                )
                self.assertNotEqual(mutation.returncode, 0)
                self.assertFalse(mutation_marker.exists())
                command = arguments
                self.assertIsInstance(command, list)
                skill = command[command.index("--skills") + 1]
                role = {
                    "pr-autopilot-analyzer": "analyze",
                    "pr-autopilot-fixer": "fix",
                    "pr-autopilot-verifier": "verify",
                }[skill]
                return _AgentProcess(
                    self._verdict_output(role, ready=True if role == "verify" else None)
                )

            with patch.dict(os.environ, parent_environment, clear=False):
                with patch(
                    "worker_entry._spawn_agent_process", side_effect=inspect_agent_child
                ) as run:
                    for role, skill in (
                        ("analyze", "pr-autopilot-analyzer"),
                        ("fix", "pr-autopilot-fixer"),
                        ("verify", "pr-autopilot-verifier"),
                    ):
                        with self.subTest(role=role):
                            self.assertEqual(
                                _default_agent("Inspect", skill, root, 18, role),
                                self._successful_execution(role),
                            )

            self.assertEqual(run.call_count, 3)
            self.assertEqual(len(isolated_config_paths), 3)
            self.assertTrue(all(not path.exists() for path in isolated_config_paths))
            self.assertFalse(mutation_marker.exists())

    def test_nonzero_agent_exit_fails_each_stage_without_result_or_push(self) -> None:
        """A failed Hermes stage has no success marker and never uses the wrapper push."""
        builders = {
            "analyze": self._analyze_task,
            "fix": self._fix_task,
            "verify": self._verify_task,
        }
        for role, build_task in builders.items():
            with (
                self.subTest(role=role),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                values = build_task(Path(temporary_directory))
                database, workspace, task_id = values[:3]
                output = StringIO()
                errors = StringIO()
                original_execute = worker_entry.execute_task

                def fail_agent(**kwargs: object) -> WorkerResult:
                    return original_execute(
                        **kwargs,
                        run_agent=lambda *_args: AgentExecution(17, None),
                    )

                with patch.object(worker_entry, "execute_task", side_effect=fail_agent), patch(
                    "worker_entry._git", wraps=worker_entry._git
                ) as guarded_git, redirect_stdout(output), redirect_stderr(errors):
                    exit_code = main(
                        [
                            "--database",
                            str(database),
                            "--task-id",
                            task_id,
                            "--workspace",
                            str(workspace),
                        ]
                    )

                self.assertEqual(exit_code, 1)
                self.assertNotIn("PR_AUTOPILOT_RESULT:", output.getvalue())
                self.assertIn("exit code 17", errors.getvalue())
                self.assertFalse(
                    any(call.args[1:2] == ("push",) for call in guarded_git.call_args_list),
                    guarded_git.call_args_list,
                )

    def test_fix_wrapper_pushes_with_an_exact_reviewed_head_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id, _base_head, expected_head = self._fix_task(
                Path(temporary_directory)
            )
            trusted_push_url = _git(workspace, "remote", "get-url", "--push", "origin")

            def leave_repair_diff(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                (target / "README.md").write_text("repaired fixture\n", encoding="utf-8")
                return self._successful_execution("fix")

            with patch("worker_entry._git", wraps=worker_entry._git) as guarded_git:
                result = execute_task(
                    database=database,
                    task_id=task_id,
                    workspace=workspace,
                    run_agent=leave_repair_diff,
                )

            self._assert_trusted_hook_free_push(
                guarded_git, workspace, expected_head, trusted_push_url
            )
            remote_head = _git(
                workspace, "ls-remote", "--heads", "origin", "refs/heads/test-pr"
            ).split()[0]
            self.assertEqual(remote_head, result.commit_sha)
            self.assertNotEqual(result.commit_sha, expected_head)
            self.assertEqual(_git(workspace, "status", "--porcelain"), "")

    def test_fix_wrapper_commits_and_pushes_an_untracked_repair(self) -> None:
        """The trusted wrapper must include an untracked Fix file in its repair commit."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id, _base_head, expected_head = self._fix_task(
                Path(temporary_directory)
            )
            trusted_push_url = _git(workspace, "remote", "get-url", "--push", "origin")

            def leave_untracked_repair(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                (target / "new_repair.py").write_text("REPAIRED = True\n", encoding="utf-8")
                return self._successful_execution("fix")

            with patch("worker_entry._git", wraps=worker_entry._git) as guarded_git:
                result = execute_task(
                    database=database,
                    task_id=task_id,
                    workspace=workspace,
                    run_agent=leave_untracked_repair,
                )

            self._assert_trusted_hook_free_push(
                guarded_git, workspace, expected_head, trusted_push_url
            )
            self.assertIn("new_repair.py", _git(workspace, "show", "--format=", "--name-only", result.commit_sha))
            remote_head = _git(
                workspace, "ls-remote", "--heads", "origin", "refs/heads/test-pr"
            ).split()[0]
            self.assertEqual(remote_head, result.commit_sha)
            self.assertEqual(_git(workspace, "status", "--porcelain"), "")

    def test_fix_wrapper_never_runs_agent_controlled_commit_or_push_hooks(self) -> None:
        """The trusted wrapper must disable hooks for both commit and push."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database, workspace, task_id, _base_head, expected_head = self._fix_task(root)
            marker_path = root / "agent-hook-ran.txt"
            credential_capture_path = root / "agent-credential-capture.txt"
            trusted_refs_before = _git(workspace, "ls-remote", "--heads", "origin")

            def plant_hooks_and_leave_repair(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                hooks_directory = target / ".git" / "hooks"
                hooks_directory.mkdir(parents=True, exist_ok=True)
                marker = shlex.quote(marker_path.as_posix())
                capture = shlex.quote(credential_capture_path.as_posix())
                script = (
                    "#!/bin/sh\n"
                    f"printf 'agent hook ran\\n' >> {marker}\n"
                    f"printenv GH_TOKEN > {capture}\n"
                    "exit 0\n"
                )
                for hook_name in ("pre-commit", "post-commit", "pre-push"):
                    hook = hooks_directory / hook_name
                    hook.write_text(script, encoding="utf-8", newline="\n")
                    hook.chmod(0o755)
                (target / "README.md").write_text("repaired fixture\n", encoding="utf-8")
                return self._successful_execution("fix")

            result = execute_task(
                database=database,
                task_id=task_id,
                workspace=workspace,
                run_agent=plant_hooks_and_leave_repair,
            )

            self.assertFalse(marker_path.exists())
            self.assertFalse(credential_capture_path.exists())
            trusted_head = _git(
                workspace, "ls-remote", "--heads", "origin", "refs/heads/test-pr"
            ).split()[0]
            self.assertEqual(trusted_head, result.commit_sha)
            self.assertNotEqual(trusted_head, expected_head)
            trusted_refs_after = _git(workspace, "ls-remote", "--heads", "origin")
            self.assertEqual(
                [line for line in trusted_refs_after.splitlines() if "refs/heads/test-pr" not in line],
                [line for line in trusted_refs_before.splitlines() if "refs/heads/test-pr" not in line],
            )

    def test_fix_rejects_changed_origin_configuration_before_wrapper_push(self) -> None:
        """Fix must not redirect a trusted wrapper push to an attacker remote."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database, workspace, task_id, _base_head, expected_head = self._fix_task(root)
            attacker_remote = root / "attacker.git"
            _git(root, "init", "--bare", str(attacker_remote))
            _git(
                workspace,
                "push",
                str(attacker_remote),
                f"{expected_head}:refs/heads/test-pr",
            )

            def redirect_origin_and_leave_repair(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                _git(target, "remote", "set-url", "origin", str(attacker_remote))
                _git(target, "remote", "set-url", "--push", "origin", str(attacker_remote))
                (target / "README.md").write_text("redirected repair\n", encoding="utf-8")
                return self._successful_execution("fix")

            with patch("worker_entry._git", wraps=worker_entry._git) as guarded_git:
                with self.assertRaisesRegex(
                    WorkerContractError, "trusted origin configuration"
                ):
                    execute_task(
                        database=database,
                        task_id=task_id,
                        workspace=workspace,
                        run_agent=redirect_origin_and_leave_repair,
                    )

            self.assertFalse(
                any(call.args[1:2] == ("push",) for call in guarded_git.call_args_list),
                guarded_git.call_args_list,
            )
            for remote in (root / "origin.git", attacker_remote):
                with self.subTest(remote=remote.name):
                    remote_head = _git(
                        workspace,
                        "ls-remote",
                        "--heads",
                        str(remote),
                        "refs/heads/test-pr",
                    ).split()[0]
                    self.assertEqual(remote_head, expected_head)

    def test_fix_rejects_local_url_rewrite_before_wrapper_push(self) -> None:
        """Fix cannot redirect the trusted push URL through local insteadOf rules."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database, workspace, task_id, _base_head, expected_head = self._fix_task(root)
            trusted_push_url = _git(workspace, "remote", "get-url", "--push", "origin")
            attacker_remote = root / "attacker.git"
            attacker_url = attacker_remote.as_uri()
            _git(root, "init", "--bare", str(attacker_remote))
            _git(
                workspace,
                "push",
                attacker_url,
                f"{expected_head}:refs/heads/test-pr",
            )

            def add_url_rewrite_and_leave_repair(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                _git(
                    target,
                    "config",
                    "--local",
                    "--add",
                    f"url.{attacker_url}.insteadOf",
                    trusted_push_url,
                )
                (target / "README.md").write_text("redirected repair\n", encoding="utf-8")
                return self._successful_execution("fix")

            with patch("worker_entry._git", wraps=worker_entry._git) as guarded_git:
                with self.assertRaisesRegex(
                    WorkerContractError, "trusted origin configuration"
                ):
                    execute_task(
                        database=database,
                        task_id=task_id,
                        workspace=workspace,
                        run_agent=add_url_rewrite_and_leave_repair,
                    )

            self.assertFalse(
                any("push" in call.args[1:] for call in guarded_git.call_args_list),
                guarded_git.call_args_list,
            )
            for remote in (root / "origin.git", attacker_remote):
                with self.subTest(remote=remote.name):
                    remote_head = _git(
                        workspace,
                        "ls-remote",
                        "--heads",
                        str(remote),
                        "refs/heads/test-pr",
                    ).split()[0]
                    self.assertEqual(remote_head, expected_head)

    def test_fix_rejects_preexisting_worktree_residue_before_agent_or_push(self) -> None:
        """Fix must begin from a clean tracked and untracked worktree."""
        for residue in ("unstaged", "untracked"):
            with self.subTest(residue=residue), tempfile.TemporaryDirectory() as temporary_directory:
                database, workspace, task_id, _base_head, expected_head = self._fix_task(
                    Path(temporary_directory)
                )
                if residue == "unstaged":
                    (workspace / "README.md").write_text("uncommitted fixture\n", encoding="utf-8")
                else:
                    (workspace / "untracked.txt").write_text("fixture\n", encoding="utf-8")
                agent_calls: list[tuple[object, ...]] = []

                def must_not_run(*arguments: object) -> int:
                    agent_calls.append(arguments)
                    return 0

                with patch("worker_entry._git", wraps=worker_entry._git) as guarded_git:
                    with self.assertRaisesRegex(WorkerContractError, "not clean before the stage"):
                        execute_task(
                            database=database,
                            task_id=task_id,
                            workspace=workspace,
                            run_agent=must_not_run,
                        )

                self.assertEqual(agent_calls, [])
                self.assertFalse(
                    any(call.args[1:2] == ("push",) for call in guarded_git.call_args_list),
                    guarded_git.call_args_list,
                )
                remote_head = _git(
                    workspace, "ls-remote", "--heads", "origin", "refs/heads/test-pr"
                ).split()[0]
                self.assertEqual(remote_head, expected_head)

    def test_fix_wrapper_rejects_a_rewound_source_branch_without_restoring_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id, base_head, _expected_head = self._fix_task(
                Path(temporary_directory)
            )

            def commit_after_author_rewind(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                (target / "README.md").write_text("stale repair\n", encoding="utf-8")
                _git(target, "add", "README.md")
                _git(target, "commit", "-m", "stale repair")
                _git(
                    target,
                    "push",
                    "--force",
                    "origin",
                    f"{base_head}:refs/heads/test-pr",
                )
                return self._successful_execution("fix")

            with self.assertRaisesRegex(
                WorkerContractError, "only the wrapper can commit"
            ):
                execute_task(
                    database=database,
                    task_id=task_id,
                    workspace=workspace,
                    run_agent=commit_after_author_rewind,
                )

            remote_head = _git(
                workspace, "ls-remote", "--heads", "origin", "refs/heads/test-pr"
            ).split()[0]
            self.assertEqual(remote_head, base_head)

    def test_fix_wrapper_rejects_a_repair_that_rewrites_the_reviewed_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id, base_head, expected_head = self._fix_task(
                Path(temporary_directory)
            )

            def rewrite_history(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                _git(target, "reset", "--hard", base_head)
                (target / "README.md").write_text("rewritten repair\n", encoding="utf-8")
                _git(target, "add", "README.md")
                _git(target, "commit", "-m", "rewritten repair")
                return self._successful_execution("fix")

            with patch("worker_entry._git", wraps=worker_entry._git) as guarded_git:
                with self.assertRaises(WorkerContractError):
                    execute_task(
                        database=database,
                        task_id=task_id,
                        workspace=workspace,
                        run_agent=rewrite_history,
                    )

            self.assertFalse(
                any(call.args[1:2] == ("push",) for call in guarded_git.call_args_list),
                guarded_git.call_args_list,
            )
            remote_head = _git(
                workspace, "ls-remote", "--heads", "origin", "refs/heads/test-pr"
            ).split()[0]
            self.assertEqual(remote_head, expected_head)

    def test_fix_noop_exit_zero_does_not_push(self) -> None:
        """The wrapper must not push when Fix provides no repair diff."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id, _base_head, expected_head = self._fix_task(
                Path(temporary_directory)
            )

            with patch("worker_entry._git", wraps=worker_entry._git) as guarded_git:
                with self.assertRaisesRegex(
                    WorkerContractError, "Fix completed without a repair diff"
                ):
                    execute_task(
                        database=database,
                        task_id=task_id,
                        workspace=workspace,
                        run_agent=lambda *_args: self._successful_execution("fix"),
                    )

            self.assertFalse(
                any(call.args[1:2] == ("push",) for call in guarded_git.call_args_list),
                guarded_git.call_args_list,
            )
            remote_head = _git(
                workspace, "ls-remote", "--heads", "origin", "refs/heads/test-pr"
            ).split()[0]
            self.assertEqual(remote_head, expected_head)

    def test_verify_accepts_the_persisted_repaired_head_and_uses_its_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id, expected_head, repaired_head = self._verify_task(
                Path(temporary_directory)
            )
            prompts: list[tuple[str, str, int]] = []

            result = execute_task(
                database=database,
                task_id=task_id,
                workspace=workspace,
                run_agent=lambda prompt, skill, _workspace, max_turns: prompts.append(
                    (prompt, skill, max_turns)
                )
                or self._successful_execution("verify"),
            )

            self.assertEqual(result, WorkerResult(role="verify", commit_sha=repaired_head))
            self.assertEqual(len(prompts), 1)
            self.assertIn(f"Original reviewed SHA: {expected_head}", prompts[0][0])
            self.assertEqual(prompts[0][1:], ("pr-autopilot-verifier", 20))

    def test_verify_rejects_a_source_head_mismatch_before_the_agent_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id, expected_head, _repaired_head = self._verify_task(
                Path(temporary_directory)
            )
            _git(workspace, "push", "--force", "origin", f"{expected_head}:refs/heads/test-pr")
            agent_calls: list[object] = []

            with self.assertRaisesRegex(WorkerContractError, "pushed source head"):
                execute_task(
                    database=database,
                    task_id=task_id,
                    workspace=workspace,
                    run_agent=lambda *_args: agent_calls.append(object()) or 0,
                )

            self.assertEqual(agent_calls, [])

    def test_verify_rejects_an_agent_that_commits_a_worktree_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id, _expected_head, _repaired_head = self._verify_task(
                Path(temporary_directory)
            )

            def mutate(
                _prompt: str, _skill: str, target: Path, _max_turns: int
            ) -> AgentExecution:
                (target / "README.md").write_text("unsafe verify change\n", encoding="utf-8")
                _git(target, "add", "README.md")
                _git(target, "commit", "-m", "unexpected verifier commit")
                return self._successful_execution("verify")

            with self.assertRaisesRegex(WorkerContractError, "Verify changed the exact head"):
                execute_task(
                    database=database,
                    task_id=task_id,
                    workspace=workspace,
                    run_agent=mutate,
                )

    def test_verify_rejects_unstaged_or_untracked_worktree_changes(self) -> None:
        for change in ("unstaged", "untracked"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                database, workspace, task_id, _expected_head, _repaired_head = self._verify_task(
                    Path(temporary_directory)
                )

                def dirty(
                    _prompt: str, _skill: str, target: Path, _max_turns: int
                ) -> AgentExecution:
                    target_file = (
                        target / "README.md"
                        if change == "unstaged"
                        else target / "untracked.txt"
                    )
                    target_file.write_text("unsafe verify change\n", encoding="utf-8")
                    return self._successful_execution("verify")

                with self.assertRaisesRegex(
                    WorkerContractError,
                    "Verify modified the worktree without a clean commit",
                ):
                    execute_task(
                        database=database,
                        task_id=task_id,
                        workspace=workspace,
                        run_agent=dirty,
                    )

    def test_cli_emits_a_compact_json_result_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database, workspace, task_id = self._analyze_task(Path(temporary_directory))
            output = StringIO()
            result = WorkerResult(role="analyze", commit_sha="a" * 40)

            with patch("worker_entry.execute_task", return_value=result) as execute:
                with redirect_stdout(output):
                    exit_code = main(
                        [
                            "--database",
                            str(database),
                            "--task-id",
                            task_id,
                            "--workspace",
                            str(workspace),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                output.getvalue(),
                'PR_AUTOPILOT_RESULT:{"commit_sha":"' + ("a" * 40) + '","role":"analyze"}\n',
            )
            execute.assert_called_once_with(
                database=database,
                task_id=task_id,
                workspace=workspace,
            )


if __name__ == "__main__":
    unittest.main()
