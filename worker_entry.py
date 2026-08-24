"""Fixed worker entry point for plugin-owned Hermes PR stages."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Callable, Sequence


_RESULT_MARKER = "PR_AUTOPILOT_RESULT:"
_STAGE_VERDICT_MARKER = "PR_AUTOPILOT_STAGE_VERDICT:"
_STAGE_VERDICT_MARKER_BYTES = _STAGE_VERDICT_MARKER.encode("ascii")
_AGENT_OUTPUT_READ_LIMIT = 4096
_STAGE_VERDICT_LIMIT = 1024


class WorkerContractError(RuntimeError):
    """A stage violated its exact-head or workspace contract."""


@dataclass(frozen=True)
class WorkerResult:
    role: str
    commit_sha: str


@dataclass(frozen=True)
class StageVerdict:
    """One explicit, narrow completion statement from a stage agent."""

    role: str
    status: str
    ready: bool = False


@dataclass(frozen=True)
class AgentExecution:
    """The bounded agent exit result and its optional structured verdict."""

    exit_code: int
    verdict: StageVerdict | None = None
    verdict_error: str | None = None


@dataclass(frozen=True)
class _TrustedOrigin:
    """One pre-agent snapshot of the wrapper's Git route and local policy."""

    configuration: tuple[str, ...]
    fetch_urls: tuple[str, ...]
    push_url: str


AgentRunner = Callable[..., int | AgentExecution]


_READ_ONLY_ROLES = frozenset({"analyze", "verify"})
_ROLE_BY_SKILL = {
    "pr-autopilot-analyzer": "analyze",
    "pr-autopilot-fixer": "fix",
    "pr-autopilot-verifier": "verify",
}


def _run(arguments: Sequence[str], *, workspace: Path) -> str:
    completed = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-1200:]
        raise WorkerContractError(
            f"required command failed ({completed.returncode}): {arguments[0]} {detail}"
        )
    return completed.stdout.strip()


def _git(workspace: Path, *arguments: str) -> str:
    return _run(("git", "-C", str(workspace), *arguments), workspace=workspace)


def _agent_environment(
    role: str, *, github_config_directory: Path | None = None
) -> dict[str, str]:
    """Return a scrubbed Git environment for one approval-gated agent."""

    if role not in {"analyze", "fix", "verify"}:
        raise WorkerContractError("worker stage role is invalid")
    environment = os.environ.copy()
    # No stage receives a configured remote route or inherited credential.
    # Hermes terminal approval separately blocks a command that tries to add
    # another route or credential helper.
    for name in tuple(environment):
        if (
            name == "GIT_CONFIG_COUNT"
            or name == "GIT_CONFIG_PARAMETERS"
            or name.startswith("GIT_CONFIG_KEY_")
            or name.startswith("GIT_CONFIG_VALUE_")
        ):
            environment.pop(name, None)
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
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    # Environment Git configuration has higher precedence than repository
    # configuration. The agent cannot use the normal origin route or a stored
    # credential helper. The trusted wrapper runs outside this environment.
    agent_config = (
        ("remote.origin.url", "pr-autopilot-remote-disabled://agent"),
        ("remote.origin.pushurl", "pr-autopilot-push-disabled://agent"),
        ("credential.helper", ""),
        ("http.extraHeader", ""),
    )
    environment["GIT_CONFIG_COUNT"] = str(len(agent_config))
    for index, (key, value) in enumerate(agent_config):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    if github_config_directory is not None:
        # GH_CONFIG_DIR has precedence over the normal user configuration
        # paths. Do not alter HOME or provider-related Hermes settings.
        environment["GH_CONFIG_DIR"] = str(github_config_directory.resolve())
    return environment


def _forward_agent_output(chunk: bytes) -> None:
    """Forward one bounded child chunk without retaining agent output."""

    binary_output = getattr(sys.stdout, "buffer", None)
    if binary_output is not None:
        binary_output.write(chunk)
        binary_output.flush()
        return
    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
    sys.stdout.flush()


def _json_object_without_duplicates(payload: str) -> dict[str, object]:
    """Decode one small JSON object while rejecting duplicate field names."""

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=pairs)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("malformed verdict") from error
    if not isinstance(value, dict):
        raise ValueError("verdict is not an object")
    return value


def _stage_verdict_from_line(line: bytes) -> StageVerdict:
    """Validate one standalone protocol line without retaining other output."""

    if not line.startswith(_STAGE_VERDICT_MARKER_BYTES):
        raise ValueError("not a verdict")
    payload = line[len(_STAGE_VERDICT_MARKER_BYTES) :]
    if not payload or len(payload) > _STAGE_VERDICT_LIMIT:
        raise ValueError("malformed verdict")
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("malformed verdict") from error
    value = _json_object_without_duplicates(decoded)
    role = value.get("role")
    status = value.get("status")
    ready = value.get("ready", False)
    if not isinstance(role, str) or not isinstance(status, str) or type(ready) is not bool:
        raise ValueError("malformed verdict")
    if status not in {"success", "blocked", "failure"}:
        raise ValueError("malformed verdict")
    expected_fields = {"role", "status"}
    if role == "verify" and status == "success":
        expected_fields.add("ready")
        if ready is not True:
            raise ValueError("malformed verdict")
    elif "ready" in value:
        raise ValueError("malformed verdict")
    if set(value) != expected_fields:
        raise ValueError("malformed verdict")
    return StageVerdict(role=role, status=status, ready=ready)


def _stream_agent_execution(process: subprocess.Popen[bytes]) -> AgentExecution:
    """Stream child output and retain only one bounded stage verdict."""

    stream = process.stdout
    if stream is None:
        raise WorkerContractError("Hermes stage output stream is unavailable")
    verdict: StageVerdict | None = None
    verdict_error: str | None = None
    marker_count = 0
    continuation = False
    while True:
        chunk = stream.readline(_AGENT_OUTPUT_READ_LIMIT)
        if not chunk:
            break
        _forward_agent_output(chunk)
        complete_line = chunk.endswith(b"\n")
        if not continuation and chunk.startswith(_STAGE_VERDICT_MARKER_BYTES):
            marker_count += 1
            if complete_line:
                try:
                    verdict = _stage_verdict_from_line(chunk.rstrip(b"\r\n"))
                except ValueError:
                    verdict_error = "worker stage verdict is malformed"
            else:
                verdict_error = "worker stage verdict is malformed"
        continuation = not complete_line
    exit_code = process.wait()
    if marker_count == 0:
        verdict_error = "worker did not return a structured stage verdict"
    elif marker_count > 1:
        verdict = None
        verdict_error = "worker returned duplicate stage verdicts"
    return AgentExecution(
        exit_code=int(exit_code), verdict=verdict, verdict_error=verdict_error
    )


def _spawn_agent_process(
    arguments: Sequence[str], workspace: Path, environment: dict[str, str]
) -> subprocess.Popen[bytes]:
    """Start the one untrusted child through a narrow, testable boundary."""

    try:
        return subprocess.Popen(
            arguments,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            bufsize=0,
        )
    except OSError as error:
        raise WorkerContractError("Hermes stage could not start") from error


def _default_agent(
    prompt: str,
    skill: str,
    workspace: Path,
    max_turns: int,
    role: str | None = None,
) -> AgentExecution:
    """Run one role with the Hermes capabilities that its contract allows."""

    if role is None:
        role = _ROLE_BY_SKILL.get(skill)
    if role not in {"analyze", "fix", "verify"}:
        raise WorkerContractError("worker stage role is invalid")
    arguments = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "chat",
        "--query",
        prompt,
        "--skills",
        skill,
    ]
    # Every stage uses the approval-gated command path. Fix can edit and test,
    # but it cannot bypass Hermes command approval with --yolo; the trusted
    # wrapper creates the repair commit and performs the leased push.
    arguments.extend(("--toolsets", "file,terminal,gortex"))
    arguments.extend(
        (
            "--in",
            str(workspace),
            "--max-turns",
            str(max_turns),
            "--quiet",
        )
    )
    # The wrapper creates this empty directory for one child process. It
    # overrides parent GitHub CLI credential stores without changing the
    # Hermes profile or provider environment, then is removed after exit.
    with tempfile.TemporaryDirectory(prefix="pr-autopilot-gh-") as temporary_directory:
        agent_environment = _agent_environment(
            role, github_config_directory=Path(temporary_directory)
        )
        process = _spawn_agent_process(arguments, workspace, agent_environment)
        return _stream_agent_execution(process)


def _git_diff_has_changes(workspace: Path, *arguments: str) -> bool:
    """Return whether Git reports a non-empty diff, or raise on Git failure."""

    completed = subprocess.run(
        ["git", "-C", str(workspace), "diff", *arguments, "--quiet"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode == 0:
        return False
    if completed.returncode == 1:
        return True
    detail = (completed.stderr or completed.stdout).strip()[-1200:]
    raise WorkerContractError(f"could not inspect Fix diff: {detail}")


def _commit_fix_diff(
    workspace: Path, starting_head: str, hooks_directory: Path
) -> str:
    """Commit one agent-produced dirty diff through the trusted wrapper."""

    if _git(workspace, "rev-parse", "HEAD").lower() != starting_head:
        raise WorkerContractError("Fix changed the exact head; only the wrapper can commit")
    _git(workspace, "diff", "--check")
    _git(workspace, "diff", "--cached", "--check")
    try:
        _git(workspace, "add", "--all")
        _git(workspace, "diff", "--cached", "--check")
        if not _git_diff_has_changes(workspace, "--cached"):
            raise WorkerContractError("Fix completed without a repair diff")
        # The agent can change worktree files, not trusted wrapper policy. The
        # wrapper creates this empty directory after the agent exits. Do not
        # execute repository-configured hooks while creating this commit.
        _git(
            workspace,
            "-c",
            f"core.hooksPath={hooks_directory}",
            "-c",
            "user.name=PR Autopilot",
            "-c",
            "user.email=pr-autopilot@localhost",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "fix: address current-head Codex findings",
        )
    except BaseException:
        # The stage started clean. Leave the agent's edits visible but return
        # the index to HEAD if trusted validation or commit creation fails.
        try:
            _git(workspace, "reset")
        except WorkerContractError:
            pass
        raise
    return _git(workspace, "rev-parse", "HEAD").lower()


def _task(database: Path, task_id: str) -> sqlite3.Row:
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=10
        )
    except sqlite3.Error as error:
        raise WorkerContractError("worker state is unavailable") from error
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT identifier, expected_head, head_ref, role, skill, max_turns, workspace, prompt
            FROM desktop_worker_task
            WHERE identifier = ?
            """,
            (task_id,),
        ).fetchone()
    except sqlite3.Error as error:
        raise WorkerContractError("worker state is incompatible") from error
    finally:
        connection.close()
    if row is None:
        raise WorkerContractError("worker task does not exist")
    max_turns = row["max_turns"]
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise WorkerContractError("worker task has an invalid max turns value")
    return row


def _run_agent(
    runner: AgentRunner,
    prompt: str,
    skill: str,
    workspace: Path,
    max_turns: int,
    role: str,
) -> AgentExecution:
    """Pass the stored bound and require a structured execution result."""

    arguments = (
        (prompt, skill, workspace, max_turns, role),
        (prompt, skill, workspace, max_turns),
        (prompt, skill, workspace),
    )
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        result = runner(*arguments[-1])
    else:
        for values in arguments:
            try:
                signature.bind(*values)
            except TypeError:
                continue
            result = runner(*values)
            break
        else:
            result = runner(*arguments[-1])
    if isinstance(result, AgentExecution):
        return result
    if isinstance(result, bool) or not isinstance(result, int):
        raise WorkerContractError("worker returned an invalid agent execution")
    # Test callbacks and legacy adapters can still report their exit code, but
    # cannot turn that code into a successful stage without a stage verdict.
    return AgentExecution(exit_code=result)


def _require_successful_verdict(execution: AgentExecution, role: str) -> None:
    """Fail closed unless one stage attests to its exact successful outcome."""

    if isinstance(execution.exit_code, bool) or not isinstance(execution.exit_code, int):
        raise WorkerContractError("worker returned an invalid agent execution")
    if execution.exit_code != 0:
        raise WorkerContractError(f"Hermes stage failed with exit code {execution.exit_code}")
    verdict = execution.verdict
    if verdict is None:
        raise WorkerContractError(
            execution.verdict_error or "worker did not return a structured stage verdict"
        )
    if verdict.role != role:
        raise WorkerContractError("worker stage verdict role does not match task")
    if verdict.status in {"blocked", "failure"}:
        raise WorkerContractError(f"worker stage reported {verdict.status}")
    if verdict.status != "success":
        raise WorkerContractError("worker stage verdict is malformed")
    if role == "verify":
        if verdict.ready is not True:
            raise WorkerContractError("Verify did not return a readiness success verdict")
    elif verdict.ready is not False:
        raise WorkerContractError("worker stage verdict is malformed")


def _remote_urls(workspace: Path, *, push: bool) -> tuple[str, ...]:
    """Return the resolved origin URLs that Git would use at this moment."""

    arguments = ["remote", "get-url"]
    if push:
        arguments.append("--push")
    arguments.extend(("--all", "origin"))
    urls = tuple(line.strip() for line in _git(workspace, *arguments).splitlines() if line.strip())
    if not urls:
        raise WorkerContractError("Fix origin remote has no configured URL")
    return urls


def _local_configuration(workspace: Path) -> tuple[str, ...]:
    """Return the complete local Git config for exact wrapper comparison.

    This command only reads configuration.  The wrapper compares this value
    before it runs any post-agent Git operation that could execute a filter,
    fsmonitor, or credential helper from local configuration.
    """

    output = _git(workspace, "config", "--local", "--includes", "--null", "--list")
    values = tuple(value for value in output.split("\0") if value)
    if not values:
        raise WorkerContractError("Fix local Git configuration is unavailable")
    return values


def _require_trusted_local_configuration(
    workspace: Path, trusted_configuration: tuple[str, ...]
) -> None:
    """Stop before post-agent Git work if the agent changed local policy."""

    if _local_configuration(workspace) != trusted_configuration:
        raise WorkerContractError("worker changed trusted local Git configuration")


def _trusted_origin(workspace: Path) -> _TrustedOrigin:
    """Snapshot one unambiguous wrapper push target before agent execution."""

    configuration = _local_configuration(workspace)
    fetch_urls = _remote_urls(workspace, push=False)
    push_urls = _remote_urls(workspace, push=True)
    if len(push_urls) != 1:
        raise WorkerContractError("Fix origin remote must have exactly one push target")
    return _TrustedOrigin(
        configuration=configuration,
        fetch_urls=fetch_urls,
        push_url=push_urls[0],
    )


def _require_trusted_origin(workspace: Path, trusted_origin: _TrustedOrigin) -> None:
    """Reject agent edits to local Git routing or policy before side effects."""

    try:
        _require_trusted_local_configuration(workspace, trusted_origin.configuration)
    except WorkerContractError as error:
        raise WorkerContractError(
            "worker changed trusted local Git configuration; "
            "trusted origin configuration is no longer valid"
        ) from error
    current = _trusted_origin(workspace)
    if current != trusted_origin:
        raise WorkerContractError("Fix changed the trusted origin configuration")


def _remote_head(workspace: Path, head_ref: str, remote: str = "origin") -> str:
    _git(workspace, "check-ref-format", "--branch", head_ref)
    output = _git(workspace, "ls-remote", "--heads", remote, f"refs/heads/{head_ref}")
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise WorkerContractError("source branch is not available on the origin remote")
    fields = lines[0].split()
    if len(fields) != 2:
        raise WorkerContractError("origin returned an invalid source branch head")
    return fields[0].lower()


def execute_task(
    *,
    database: Path,
    task_id: str,
    workspace: Path,
    run_agent: AgentRunner = _default_agent,
) -> WorkerResult:
    """Run one stored stage and enforce its immutable completion contract."""

    row = _task(database, task_id)
    root = database.resolve().parent.parent
    managed_root = (root / "worktrees").resolve()
    resolved_workspace = workspace.resolve()
    try:
        stored_relative = resolved_workspace.relative_to(managed_root).as_posix()
    except ValueError as error:
        raise WorkerContractError("worker workspace is outside the managed root") from error
    if stored_relative != str(row["workspace"]):
        raise WorkerContractError("worker workspace does not match its stored task")
    if not resolved_workspace.is_dir():
        raise WorkerContractError("worker workspace does not exist")

    role = str(row["role"])
    expected_head = str(row["expected_head"]).lower()
    head_ref = str(row["head_ref"])
    starting_head = _git(resolved_workspace, "rev-parse", "HEAD").lower()
    trusted_origin: _TrustedOrigin | None = None
    trusted_configuration: tuple[str, ...]
    # This explicitly includes untracked paths. Analyze and Verify must start
    # from a clean managed checkout so their read-only result has one meaning.
    if _git(resolved_workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise WorkerContractError("worker worktree is not clean before the stage")
    if role in {"analyze", "fix"} and starting_head != expected_head:
        raise WorkerContractError("worker did not start at the required exact head")
    if role == "fix":
        trusted_origin = _trusted_origin(resolved_workspace)
        trusted_configuration = trusted_origin.configuration
        if _remote_head(resolved_workspace, head_ref, trusted_origin.push_url) != expected_head:
            raise WorkerContractError("Fix source branch moved before the stage started")
    else:
        trusted_configuration = _local_configuration(resolved_workspace)
    if role == "verify":
        if starting_head == expected_head:
            raise WorkerContractError("Verify did not receive a repaired head")
        _git(resolved_workspace, "merge-base", "--is-ancestor", expected_head, starting_head)
        if _remote_head(resolved_workspace, head_ref) != starting_head:
            raise WorkerContractError("Verify did not receive the pushed source head")

    execution = _run_agent(
        run_agent,
        str(row["prompt"]),
        str(row["skill"]),
        resolved_workspace,
        int(row["max_turns"]),
        role,
    )
    _require_successful_verdict(execution, role)

    if trusted_origin is not None:
        # The agent process has ended. Verify the complete local Git config
        # before trusted commit or network operations, then use only this
        # snapshot URL below rather than the mutable remote name.
        _require_trusted_origin(resolved_workspace, trusted_origin)
    else:
        # Compare before diff, status, or any other wrapper Git command. This
        # leaves no path that runs an executable setting written by Analyze or
        # Verify after their agent process has ended.
        _require_trusted_local_configuration(
            resolved_workspace, trusted_configuration
        )
    if role == "fix":
        # Make the hook path only after the untrusted process ended. The same
        # wrapper-owned empty directory protects both commit and push hooks.
        with tempfile.TemporaryDirectory(prefix="pr-autopilot-hooks-") as temporary_directory:
            hooks_directory = Path(temporary_directory)
            final_head = _commit_fix_diff(
                resolved_workspace, starting_head, hooks_directory
            )
            if final_head == expected_head:
                raise WorkerContractError("Fix completed without a repair commit")
            _git(resolved_workspace, "merge-base", "--is-ancestor", expected_head, final_head)
            if _remote_head(resolved_workspace, head_ref, trusted_origin.push_url) != expected_head:
                raise WorkerContractError("Fix source branch moved while the stage was running")
            _git(
                resolved_workspace,
                "-c",
                f"core.hooksPath={hooks_directory}",
                "push",
                "--no-verify",
                f"--force-with-lease=refs/heads/{head_ref}:{expected_head}",
                trusted_origin.push_url,
                f"HEAD:refs/heads/{head_ref}",
            )
            if _remote_head(resolved_workspace, head_ref, trusted_origin.push_url) != final_head:
                raise WorkerContractError("Fix did not push the exact repaired head")
    else:
        final_head = _git(resolved_workspace, "rev-parse", "HEAD").lower()
    if role in _READ_ONLY_ROLES:
        # The trusted wrapper validates this fixed repository invariant after
        # the stage, regardless of its own focused checks.
        _git(resolved_workspace, "diff", "--check")
    if _git(resolved_workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise WorkerContractError(f"{role.title()} modified the worktree without a clean commit")
    if role in {"analyze", "verify"} and final_head != starting_head:
        raise WorkerContractError(f"{role.title()} changed the exact head")
    return WorkerResult(role=role, commit_sha=final_head)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one fixed PR Autopilot worker stage")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = execute_task(
            database=arguments.database,
            task_id=arguments.task_id,
            workspace=arguments.workspace,
        )
    except WorkerContractError as error:
        print(f"PR Autopilot worker blocked: {error}", file=sys.stderr)
        return 1
    payload = json.dumps(
        {"commit_sha": result.commit_sha, "role": result.role},
        sort_keys=True,
        separators=(",", ":"),
    )
    print(f"{_RESULT_MARKER}{payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
