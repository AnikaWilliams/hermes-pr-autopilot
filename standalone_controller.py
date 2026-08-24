"""Desktop-lifetime controller adapters for PR Autopilot.

The deterministic GitHub controller stays model-free. This module replaces its
Kanban adapter with plugin-owned Hermes worker attempts and local audit state.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from pr_autopilot import CommandError, Config, Controller, Event, repository_storage_slug
from pr_reconciler import StateStore


_RESULT_MARKER = "PR_AUTOPILOT_RESULT:"
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_FIELD_PATTERNS = {
    "expected_head": re.compile(
        r"^(?:Required starting SHA|Required head SHA|Original reviewed SHA):\s*([0-9a-fA-F]{40})\s*$",
        re.MULTILINE,
    ),
    "head_ref": re.compile(r"^Source branch:\s*([^\r\n]+?)\s*$", re.MULTILINE),
}
_TERMINAL_LOCAL = frozenset({"done", "blocked"})
_TERMINAL_RUNTIME = frozenset({"succeeded", "failed", "cancelled", "indeterminate"})
_STAGE_ROLES = ("analyze", "fix", "verify")
_PARENT_HANDOFF_MARKER = "\n\nVerified parent-stage handoff:\n"
_RECOVERY_OUTPUT_LIMIT = 16_000
_RECOVERY_ENDPOINT = re.compile(r"(?im)^\s*endpoint\s*:\s*(https://[^\s<>'\"]+)")
_LOG = logging.getLogger("pr_autopilot.desktop")


class _LeaseFencedRunner:
    """Reject controller commands after the Desktop lease is no longer current."""

    def __init__(
        self,
        runner: Any,
        lease_lost: threading.Event,
        control_fenced: Callable[[], bool] | None = None,
    ) -> None:
        self._runner = runner
        self._lease_lost = lease_lost
        self._control_fenced = control_fenced

    def _require_lease(self) -> None:
        if self._is_fenced():
            raise CommandError("controller lease was fenced during this cycle")

    def _is_fenced(self) -> bool:
        return self._lease_lost.is_set() or (
            self._control_fenced is not None and self._control_fenced()
        )

    def run(self, arguments: Sequence[str], **kwargs: Any) -> str:
        self._require_lease()
        cancelable = getattr(self._runner, "run_cancelable", None)
        result = (
            cancelable(arguments, abort=self._is_fenced, **kwargs)
            if callable(cancelable)
            else self._runner.run(arguments, **kwargs)
        )
        self._require_lease()
        return result

    def run_json(self, arguments: Sequence[str], **kwargs: Any) -> Any:
        self._require_lease()
        cancelable = getattr(self._runner, "run_json_cancelable", None)
        result = (
            cancelable(arguments, abort=self._is_fenced, **kwargs)
            if callable(cancelable)
            else self._runner.run_json(arguments, **kwargs)
        )
        self._require_lease()
        return result


class _LeaseFencedStore:
    """Reject controller state access after this controller loses its lease."""

    def __init__(
        self,
        store: Any,
        lease_lost: threading.Event,
        control_fenced: Callable[[], bool] | None = None,
    ) -> None:
        self._store = store
        self._lease_lost = lease_lost
        self._control_fenced = control_fenced

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._store, name)
        if not callable(value):
            return value

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if self._lease_lost.is_set() or (
                self._control_fenced is not None and self._control_fenced()
            ):
                raise CommandError("controller lease was fenced during this cycle")
            result = value(*args, **kwargs)
            if self._lease_lost.is_set() or (
                self._control_fenced is not None and self._control_fenced()
            ):
                raise CommandError("controller lease was fenced during this cycle")
            return result

        return guarded


class StandaloneTaskClient:
    """Kanban-shaped adapter backed by the generic Hermes worker runtime."""

    def __init__(
        self,
        config: Config,
        *,
        runtime: Any,
        owner: str,
        definition_ids: Mapping[str, str] | None = None,
        definition_id: str | None = None,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.owner = owner
        self._lease_fence: threading.Event | None = None
        self._control_fenced: Callable[[], bool] | None = None
        if definition_ids is None:
            if not isinstance(definition_id, str) or not definition_id:
                raise ValueError("standalone worker definition ids are required")
            definition_ids = {role: definition_id for role in _STAGE_ROLES}
        elif definition_id is not None:
            raise ValueError("use either definition_ids or definition_id, not both")
        if set(definition_ids) != set(_STAGE_ROLES) or not all(
            isinstance(value, str) and value for value in definition_ids.values()
        ):
            raise ValueError("standalone worker definition ids must cover every stage")
        self.definition_ids = dict(definition_ids)
        StateStore(config.state_path)
        self._initialize()

    def set_lease_fence(self, fence: threading.Event | None) -> None:
        """Fence local task writes when the owning controller loses its lease."""

        self._lease_fence = fence

    def set_control_fence(self, control_fenced: Callable[[], bool] | None) -> None:
        """Fence local task actions after a dashboard reset invalidates a cycle."""

        self._control_fenced = control_fenced

    def _require_active_lease(self) -> None:
        if (self._lease_fence is not None and self._lease_fence.is_set()) or (
            self._control_fenced is not None and self._control_fenced()
        ):
            raise CommandError("controller lease was fenced during this cycle")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.config.state_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS desktop_worker_task (
                    identifier TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    number INTEGER NOT NULL CHECK (number > 0),
                    expected_head TEXT NOT NULL,
                    head_ref TEXT NOT NULL,
                    finding_fingerprint TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('analyze', 'fix', 'verify')),
                    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 3),
                    parent_id TEXT REFERENCES desktop_worker_task(identifier) ON DELETE RESTRICT,
                    definition_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    skill TEXT NOT NULL,
                    max_turns INTEGER NOT NULL CHECK (max_turns > 0),
                    workspace TEXT NOT NULL,
                    base_prompt TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('scheduled', 'running', 'done', 'blocked')),
                    runtime_attempt_id TEXT,
                    output TEXT,
                    output_digest TEXT,
                    exit_code INTEGER,
                    commit_sha TEXT,
                    terminal_reason TEXT,
                    automatic_recovery_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE (repository, number, expected_head, finding_fingerprint, role)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(desktop_worker_task)")
            }
            if "definition_id" not in columns:
                connection.execute("ALTER TABLE desktop_worker_task ADD COLUMN definition_id TEXT")
            if "max_turns" not in columns:
                connection.execute("ALTER TABLE desktop_worker_task ADD COLUMN max_turns INTEGER")
            if "base_prompt" not in columns:
                connection.execute("ALTER TABLE desktop_worker_task ADD COLUMN base_prompt TEXT")
            if "automatic_recovery_count" not in columns:
                connection.execute(
                    "ALTER TABLE desktop_worker_task "
                    "ADD COLUMN automatic_recovery_count INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                UPDATE desktop_worker_task
                SET definition_id = CASE role
                    WHEN 'analyze' THEN ?
                    WHEN 'fix' THEN ?
                    WHEN 'verify' THEN ?
                END
                WHERE definition_id IS NULL OR trim(definition_id) = ''
                """,
                (
                    self.definition_ids["analyze"],
                    self.definition_ids["fix"],
                    self.definition_ids["verify"],
                ),
            )
            connection.execute(
                """
                UPDATE desktop_worker_task
                SET base_prompt = CASE
                    WHEN instr(prompt, ?) > 0 THEN substr(prompt, 1, instr(prompt, ?) - 1)
                    ELSE prompt
                END
                WHERE base_prompt IS NULL OR trim(base_prompt) = ''
                """,
                (_PARENT_HANDOFF_MARKER, _PARENT_HANDOFF_MARKER),
            )
            connection.execute(
                """
                UPDATE desktop_worker_task
                SET max_turns = CASE role
                    WHEN 'analyze' THEN ?
                    WHEN 'fix' THEN ?
                    WHEN 'verify' THEN ?
                END
                WHERE max_turns IS NULL OR max_turns < 1
                """,
                (
                    self.config.analysis_max_turns,
                    self.config.fix_max_turns,
                    self.config.verification_max_turns,
                ),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS desktop_worker_event (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES desktop_worker_task(identifier) ON DELETE RESTRICT,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

    @staticmethod
    def _tenant(repository: str, number: int) -> str:
        return f"pr-autopilot:{repository}#{number}"

    @staticmethod
    def _field(body: str, name: str) -> str:
        match = _FIELD_PATTERNS[name].search(body)
        if match is None:
            raise CommandError(f"standalone worker prompt is missing {name}")
        value = match.group(1).strip()
        if name == "expected_head":
            return value.lower()
        if not value or len(value) > 255:
            raise CommandError("standalone worker prompt has an invalid source branch")
        return value

    @staticmethod
    def _task_id(
        repository: str,
        number: int,
        expected_head: str,
        fingerprint: str,
        role: str,
    ) -> str:
        source = f"{repository.casefold()}\0{number}\0{expected_head}\0{fingerprint}\0{role}"
        return f"task-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:32]}-{role[0]}"

    def _row(self, task_id: str) -> sqlite3.Row:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_worker_task WHERE identifier = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise CommandError(f"standalone worker task does not exist: {task_id}")
        return row

    def _event(self, task_id: str, kind: str, detail: str) -> None:
        self._require_active_lease()
        safe_detail = " ".join(str(detail).split())[:1000] or "event"
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO desktop_worker_event (task_id, kind, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, kind, safe_detail, int(time.time())),
            )
        self._require_active_lease()

    def create_pipeline(
        self,
        *,
        repository: str,
        number: int,
        workspace: Path,
        finding_fingerprint: str,
        analysis_body: str,
        fix_body: str,
        verification_body: str,
    ) -> dict[str, str]:
        """Persist one exact-head pipeline and start Analyze idempotently."""

        self._require_active_lease()
        expected_head = self._field(analysis_body, "expected_head")
        head_ref = self._field(analysis_body, "head_ref")
        if self._field(fix_body, "expected_head") != expected_head:
            raise CommandError("standalone Fix prompt belongs to a different head")
        if self._field(verification_body, "expected_head") != expected_head:
            raise CommandError("standalone Verify prompt belongs to a different head")
        if self._field(fix_body, "head_ref") != head_ref or self._field(
            verification_body, "head_ref"
        ) != head_ref:
            raise CommandError("standalone pipeline source branches do not match")
        resolved_workspace = workspace.resolve()
        try:
            workspace_relative = resolved_workspace.relative_to(
                self.config.worktree_root.resolve()
            ).as_posix()
        except ValueError as error:
            raise CommandError("standalone worker workspace is outside the managed root") from error
        if not resolved_workspace.is_dir():
            raise CommandError("standalone worker workspace does not exist")

        roles = (
            (
                "analyze",
                1,
                self.definition_ids["analyze"],
                self.config.analyzer_profile,
                self.config.analyzer_skill,
                self.config.analysis_max_turns,
                analysis_body,
            ),
            (
                "fix",
                2,
                self.definition_ids["fix"],
                self.config.worker_profile,
                self.config.worker_skill,
                self.config.fix_max_turns,
                fix_body,
            ),
            (
                "verify",
                3,
                self.definition_ids["verify"],
                self.config.verifier_profile,
                self.config.verifier_skill,
                self.config.verification_max_turns,
                verification_body,
            ),
        )
        identifiers = {
            role: self._task_id(repository, number, expected_head, finding_fingerprint, role)
            for role, *_rest in roles
        }
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for role, position, definition_id, profile, skill, max_turns, prompt in roles:
                parent_id = identifiers["analyze"] if role == "fix" else (
                    identifiers["fix"] if role == "verify" else None
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO desktop_worker_task (
                        identifier, repository, number, expected_head, head_ref,
                        finding_fingerprint, role, position, parent_id, definition_id,
                        profile, skill, max_turns, workspace, base_prompt, prompt, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
                    """,
                    (
                        identifiers[role],
                        repository,
                        number,
                        expected_head,
                        head_ref,
                        finding_fingerprint,
                        role,
                        position,
                        parent_id,
                        definition_id,
                        profile,
                        skill,
                        max_turns,
                        workspace_relative,
                        prompt,
                        prompt,
                        now,
                        now,
                    ),
                )
        self._require_active_lease()
        self.status(identifiers["analyze"])
        return identifiers

    def _runtime_output(self, attempt_id: str) -> str:
        try:
            events = self.runtime.list_events(self.owner, attempt_id, limit=100)
        except (KeyError, RuntimeError, ValueError) as error:
            raise CommandError("standalone worker audit output is unavailable") from error
        outputs = [str(event.detail) for event in events if getattr(event, "kind", "") == "output"]
        return outputs[-1] if outputs else ""

    @staticmethod
    def _result(output: str) -> dict[str, Any] | None:
        for line in reversed(output.splitlines()):
            if not line.startswith(_RESULT_MARKER):
                continue
            try:
                value = json.loads(line[len(_RESULT_MARKER) :])
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
        return None

    def _transient_provider_failure(
        self, row: sqlite3.Row
    ) -> dict[str, str] | None:
        """Return safe recovery metadata for one proven provider outage.

        A failed worker can print arbitrary text. Treat it as a recoverable
        provider outage only when the runtime failed, its bounded output has a
        known transient signature, and it names one configured HTTPS endpoint.
        """

        if str(row["terminal_reason"] or "") != "worker ended as failed":
            return None
        output = str(row["output"] or "")[-_RECOVERY_OUTPUT_LIMIT:]
        normalized = output.casefold()
        if not normalized:
            return None

        if any(
            marker in normalized
            for marker in (
                "provider overloaded",
                "overloaded",
                "rate limit",
                "too many requests",
                "http 429",
                "status 429",
            )
        ):
            failure_reason = "overloaded"
        elif (
            re.search(r"\b(?:500|502|503|504)\b", normalized) is not None
            or "internalservererror" in normalized
            or "service unavailable" in normalized
            or "bad gateway" in normalized
            or "gateway timeout" in normalized
        ):
            failure_reason = "server_error"
        elif any(
            marker in normalized
            for marker in (
                "apiconnectionerror",
                "apitimeouterror",
                "connection refused",
                "connection reset",
                "connection timed out",
                "could not resolve host",
                "error connecting to",
                "failed to connect",
                "network is unreachable",
                "provider unreachable",
                "request timed out",
                "temporary failure in name resolution",
                "timeout",
            )
        ):
            failure_reason = "timeout"
        else:
            return None

        match = _RECOVERY_ENDPOINT.search(output)
        if match is None:
            return None
        endpoint = match.group(1).rstrip(".,;)]}")
        try:
            parsed = urlsplit(endpoint)
            hostname = (parsed.hostname or "").casefold()
        except ValueError:
            return None
        if (
            parsed.scheme.casefold() != "https"
            or not hostname
            or hostname not in self.config.recovery_endpoint_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        return {"failure_reason": failure_reason, "endpoint": endpoint}

    def _block(self, task_id: str, reason: str, *, output: str = "") -> str:
        self._require_active_lease()
        now = int(time.time())
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE desktop_worker_task
                SET status = 'blocked', terminal_reason = ?, output = ?, updated_at = ?
                WHERE identifier = ? AND status NOT IN ('done', 'blocked')
                """,
                (reason, output[-262144:], now, task_id),
            )
        self._require_active_lease()
        self._event(task_id, "blocked", reason)
        return "blocked"

    def _transient_retry_head(self, row: sqlite3.Row) -> str:
        """Return the exact clean head required before one stage can retry."""

        expected_head = str(row["expected_head"] or "").lower()
        if not _SHA.fullmatch(expected_head):
            raise CommandError("standalone worker workspace cannot be safely restored")
        if str(row["role"]) != "verify":
            return expected_head
        parent_id = row["parent_id"]
        if not isinstance(parent_id, str) or not parent_id:
            raise CommandError("standalone worker workspace cannot be safely restored")
        parent = self._row(parent_id)
        repaired_head = str(parent["commit_sha"] or "").lower()
        if str(parent["status"]) != "done" or not _SHA.fullmatch(repaired_head):
            raise CommandError("standalone worker workspace cannot be safely restored")
        return repaired_head

    def _restore_transient_workspace(self, row: sqlite3.Row) -> str:
        """Prove a blocked worker can safely restart from its exact head."""

        try:
            repository = str(row["repository"])
            number = int(row["number"])
            slug = repository_storage_slug(repository)
            expected_relative = f"{slug}/pr-{number}"
            if str(row["workspace"]) != expected_relative:
                raise ValueError("unexpected workspace")
            worktree_root = self.config.worktree_root.resolve()
            worktree = (worktree_root / slug / f"pr-{number}").resolve()
            worktree.relative_to(worktree_root)
            retry_head = self._transient_retry_head(row)
        except (TypeError, ValueError, CommandError) as error:
            raise CommandError("standalone worker workspace cannot be safely restored") from error
        if not worktree.is_dir():
            raise CommandError("standalone worker workspace cannot be safely restored")

        def git(*arguments: str) -> str:
            try:
                completed = subprocess.run(
                    ["git", "-C", str(worktree), *arguments],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CommandError("standalone worker workspace cannot be safely restored") from error
            if completed.returncode != 0:
                raise CommandError("standalone worker workspace cannot be safely restored")
            return completed.stdout.strip()

        try:
            if git("rev-parse", "--is-inside-work-tree").lower() != "true":
                raise ValueError("not a worktree")
            if Path(git("rev-parse", "--show-toplevel")).resolve() != worktree:
                raise ValueError("different worktree")
            common_dir = Path(git("rev-parse", "--git-common-dir"))
            if not common_dir.is_absolute():
                common_dir = worktree / common_dir
            expected_common_dir = (self.config.repo_cache_root / slug / ".git").resolve()
            if common_dir.resolve() != expected_common_dir:
                raise ValueError("unmanaged common directory")
            if git("status", "--porcelain", "--untracked-files=all"):
                raise ValueError("dirty worktree")
            if git("rev-parse", "--verify", f"{retry_head}^{{commit}}").lower() != retry_head:
                raise ValueError("retry head is unavailable")
            current_head = git("rev-parse", "HEAD").lower()
            if current_head != retry_head:
                # A local clean commit can be discarded only when it has not
                # reached origin. A pushed repair belongs to fresh-head
                # reconciliation and must never be erased by recovery.
                remotes = set(git("remote").splitlines())
                if "origin" in remotes:
                    head_ref = str(row["head_ref"] or "")
                    git("check-ref-format", "--branch", head_ref)
                    remote_lines = [
                        line.split()
                        for line in git(
                            "ls-remote", "--heads", "origin", f"refs/heads/{head_ref}"
                        ).splitlines()
                        if line.strip()
                    ]
                    if (
                        len(remote_lines) != 1
                        or len(remote_lines[0]) != 2
                        or remote_lines[0][0].lower() != retry_head
                        or remote_lines[0][1] != f"refs/heads/{head_ref}"
                    ):
                        raise ValueError("source branch already advanced")
            git("reset", "--hard", retry_head)
            if git("rev-parse", "HEAD").lower() != retry_head or git(
                "status", "--porcelain", "--untracked-files=all"
            ):
                raise ValueError("reset did not produce a clean exact head")
        except (CommandError, OSError, ValueError) as error:
            raise CommandError("standalone worker workspace cannot be safely restored") from error
        return retry_head

    def _launch_runtime_attempt(self, row: sqlite3.Row, attempt_id: str) -> Any:
        """Launch one immutable worker attempt through the registered role policy."""

        return self.runtime.launch(
            self.owner,
            str(row["definition_id"]),
            attempt_id=attempt_id,
            work_item_ref=str(row["identifier"]),
            workspace_relative=str(row["workspace"]),
            profile=str(row["profile"]),
            model="default",
            reasoning="none",
        )

    def _launch(self, row: sqlite3.Row) -> str:
        self._require_active_lease()
        task_id = str(row["identifier"])
        prior_attempt_id = row["runtime_attempt_id"]
        attempt_id = (
            f"{task_id}-retry-{uuid4().hex}"
            if isinstance(prior_attempt_id, str) and prior_attempt_id
            else task_id
        )
        prompt = str(row["base_prompt"] or row["prompt"])
        parent_id = row["parent_id"]
        if parent_id:
            parent = self._row(str(parent_id))
            parent_output = str(parent["output"] or "").strip()
            if parent_output:
                prompt = (
                    f"{prompt.rstrip()}{_PARENT_HANDOFF_MARKER}"
                    f"{parent_output[:200000]}\n"
                )
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE desktop_worker_task
                SET prompt = ?, runtime_attempt_id = ?, status = 'running', updated_at = ?
                WHERE identifier = ? AND status = 'scheduled'
                """,
                (prompt, attempt_id, int(time.time()), task_id),
            )
        if updated.rowcount != 1:
            return self.status(task_id)
        self._require_active_lease()
        try:
            snapshot = self._launch_runtime_attempt(row, attempt_id)
        except (KeyError, RuntimeError, ValueError) as error:
            return self._block(task_id, f"worker launch rejected: {type(error).__name__}")
        self._event(task_id, "launched", f"{row['role']} worker admitted")
        if getattr(snapshot, "status", "running") in _TERMINAL_RUNTIME:
            return self.status(task_id)
        return "running"

    def status(self, task_id: str) -> str:
        self._require_active_lease()
        try:
            row = self._row(task_id)
        except CommandError:
            # Legacy Kanban identifiers stay visible as quarantined unknown
            # state. They are never adopted by the standalone worker runtime.
            return "unknown"
        local_status = str(row["status"])
        if local_status in _TERMINAL_LOCAL:
            return local_status
        if local_status == "scheduled":
            parent_id = row["parent_id"]
            if parent_id:
                parent_status = self.status(str(parent_id))
                if parent_status == "blocked":
                    return self._block(task_id, "parent stage did not complete")
                if parent_status != "done":
                    return "scheduled"
            return self._launch(row)

        attempt_id = str(row["runtime_attempt_id"] or task_id)
        try:
            snapshot = self.runtime.observe(self.owner, attempt_id)
        except KeyError:
            # A controller crash can leave the local task durable as running
            # before this runtime recorded the attempt. Re-admit the exact
            # immutable attempt id. The runtime itself makes this concurrent
            # retry idempotent if another observer wins the race.
            try:
                snapshot = self._launch_runtime_attempt(row, attempt_id)
            except (KeyError, RuntimeError, ValueError) as error:
                return self._block(
                    task_id, f"worker relaunch rejected: {type(error).__name__}"
                )
            self._require_active_lease()
            self._event(task_id, "relaunched", "runtime attempt was absent after restart")
        except (RuntimeError, ValueError) as error:
            return self._block(task_id, f"worker observation failed: {type(error).__name__}")
        runtime_status = str(getattr(snapshot, "status", "indeterminate"))
        if runtime_status not in _TERMINAL_RUNTIME:
            return "running"
        output = self._runtime_output(attempt_id)
        if runtime_status != "succeeded":
            return self._block(task_id, f"worker ended as {runtime_status}", output=output)
        result = self._result(output)
        role = str(row["role"])
        result_role = result.get("role") if result else None
        if not isinstance(result_role, str):
            return self._block(
                task_id,
                "worker result did not attest to a stage role",
                output=output,
            )
        if result_role != role:
            return self._block(
                task_id,
                "worker result role does not match task",
                output=output,
            )
        commit_sha = result.get("commit_sha") if result else None
        if not isinstance(commit_sha, str) or not _SHA.fullmatch(commit_sha):
            return self._block(task_id, "worker result did not attest to a commit", output=output)
        commit_sha = commit_sha.lower()
        expected_head = str(row["expected_head"])
        if role == "analyze" and commit_sha != expected_head:
            return self._block(task_id, "Analyze changed the exact head", output=output)
        if role in {"fix", "verify"} and commit_sha == expected_head:
            return self._block(task_id, f"{role.title()} did not attest to a repaired head", output=output)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE desktop_worker_task
                SET status = 'done', output = ?, output_digest = ?, exit_code = ?,
                    commit_sha = ?, terminal_reason = NULL, updated_at = ?
                WHERE identifier = ? AND status = 'running'
                """,
                (
                    output[-262144:],
                    getattr(snapshot, "output_digest", None),
                    getattr(snapshot, "exit_code", None),
                    commit_sha,
                    int(time.time()),
                    task_id,
                ),
            )
        self._require_active_lease()
        self._event(task_id, "completed", f"{role} worker completed")
        return "done"

    def peek_status(self, task_id: str) -> str:
        """Observe running work without launching a scheduled downstream stage."""

        self._require_active_lease()
        try:
            row = self._row(task_id)
        except CommandError:
            return "unknown"
        local_status = str(row["status"])
        if local_status == "scheduled":
            return "scheduled"
        return self.status(task_id)

    def details(self, task_id: str) -> dict[str, Any]:
        row = self._row(task_id)
        status = self.status(task_id)
        if status in _TERMINAL_LOCAL:
            # status() can persist a just-finished runtime attempt. Read its
            # durable output and reason, rather than returning the stale row
            # observed before that transition.
            row = self._row(task_id)
        commit_sha = row["commit_sha"]
        transient_failure = (
            self._transient_provider_failure(row) if status == "blocked" else None
        )
        outcome = "completed" if status == "done" else (
            "provider_unavailable" if transient_failure is not None else status
        )
        metadata: dict[str, Any] = {"commit_sha": commit_sha} if commit_sha else {}
        if transient_failure is not None:
            metadata.update(transient_failure)
            metadata["automatic_recovery_count"] = int(
                row["automatic_recovery_count"] or 0
            )
        return {
            "task": {
                "id": task_id,
                "status": status,
                "assignee": str(row["profile"]),
                "tenant": self._tenant(str(row["repository"]), int(row["number"])),
                "block_kind": (
                    "transient" if transient_failure is not None else "terminal"
                )
                if status == "blocked"
                else None,
            },
            "runs": [
                {
                    "id": 1,
                    "profile": str(row["profile"]),
                    "outcome": outcome,
                    "metadata": metadata,
                }
            ],
        }

    def prompt_for(self, task_id: str) -> str:
        return str(self._row(task_id)["prompt"])

    def log(self, task_id: str, *, tail: int = 16000) -> str:
        return str(self._row(task_id)["output"] or "")[-tail:]

    def extend_runtime(self, task_id: str, *, verified_head: str) -> dict[str, Any]:
        del task_id, verified_head
        return {"applied": False}

    def unblock(self, task_id: str, *, reason: str) -> None:
        """Reschedule only a proven transient provider outage for recovery."""

        self._require_active_lease()
        if not isinstance(reason, str) or not reason.strip():
            raise CommandError("standalone worker recovery reason is required")
        initial = self._row(task_id)
        if (
            str(initial["status"]) != "blocked"
            or self._transient_provider_failure(initial) is None
        ):
            raise CommandError("standalone worker is not a recoverable provider outage")
        if int(initial["automatic_recovery_count"] or 0) >= 1:
            raise CommandError("standalone worker automatic recovery limit reached")
        # Git can be slow. Verify and restore before acquiring the write lock,
        # then revalidate the durable blocked state inside the short mutation.
        restored_head = self._restore_transient_workspace(initial)
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM desktop_worker_task WHERE identifier = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise CommandError("standalone worker task does not exist")
            if str(row["status"]) != "blocked" or self._transient_provider_failure(row) is None:
                raise CommandError("standalone worker is not a recoverable provider outage")
            if int(row["automatic_recovery_count"] or 0) >= 1:
                raise CommandError("standalone worker automatic recovery limit reached")
            if self._transient_retry_head(row) != restored_head:
                raise CommandError("standalone worker recovery state changed")
            updated = connection.execute(
                """
                UPDATE desktop_worker_task
                SET status = 'scheduled', prompt = base_prompt, output = NULL,
                    output_digest = NULL, exit_code = NULL, commit_sha = NULL,
                    terminal_reason = NULL, automatic_recovery_count = automatic_recovery_count + 1,
                    updated_at = ?
                WHERE identifier = ? AND status = 'blocked'
                  AND automatic_recovery_count < 1
                """,
                (now, task_id),
            )
            if updated.rowcount != 1:
                raise CommandError("standalone worker recovery state changed")
        self._require_active_lease()
        self._event(task_id, "unblocked", reason.strip()[:1000])

    def retire_pipeline(
        self,
        task_ids: Mapping[str, str],
        *,
        reason: str,
        cancel_running: bool = False,
    ) -> None:
        """Retire a stale pipeline and optionally cancel its active worker."""

        self._require_active_lease()
        if set(task_ids) != set(_STAGE_ROLES) or not all(
            isinstance(task_id, str) and task_id for task_id in task_ids.values()
        ):
            raise CommandError("standalone pipeline is incomplete")
        ordered_ids = tuple(task_ids[role] for role in _STAGE_ROLES)
        if len(set(ordered_ids)) != len(ordered_ids):
            raise CommandError("standalone pipeline has duplicate task identifiers")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT identifier, role, status, runtime_attempt_id FROM desktop_worker_task
                WHERE identifier IN (?, ?, ?)
                """,
                ordered_ids,
            ).fetchall()
            if len(rows) != len(ordered_ids):
                raise CommandError("standalone pipeline task does not exist")
            by_identifier = {str(row["identifier"]): row for row in rows}
            for role, task_id in zip(_STAGE_ROLES, ordered_ids):
                row = by_identifier.get(task_id)
                if row is None or row["role"] != role:
                    raise CommandError("standalone pipeline task roles do not match")
                if row["status"] not in {"done", "blocked", "scheduled", "running"}:
                    raise CommandError("standalone pipeline task status is unknown")
        if cancel_running:
            cancel = getattr(self.runtime, "cancel", None)
            if not callable(cancel):
                raise CommandError("standalone runtime cannot cancel a closed pull request worker")
            for task_id in ordered_ids:
                row = by_identifier[task_id]
                if row["status"] != "running":
                    continue
                attempt_id = row["runtime_attempt_id"]
                if not isinstance(attempt_id, str) or not attempt_id:
                    raise CommandError("standalone running worker has no runtime attempt")
                try:
                    cancellation = cancel(self.owner, attempt_id, reason=reason[:128])
                except (KeyError, RuntimeError, ValueError) as error:
                    raise CommandError(
                        "standalone worker cancellation could not be confirmed"
                    ) from error
                if str(getattr(cancellation, "status", "indeterminate")) not in {
                    "cancelled",
                    "succeeded",
                    "failed",
                }:
                    raise CommandError("standalone worker cancellation is indeterminate")
                # Reconcile the terminal runtime result before changing local
                # state. This cannot launch another stage for a running task.
                self.status(task_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT identifier, role, status FROM desktop_worker_task
                WHERE identifier IN (?, ?, ?)
                """,
                ordered_ids,
            ).fetchall()
            if len(rows) != len(ordered_ids):
                raise CommandError("standalone pipeline task does not exist")
            by_identifier = {str(row["identifier"]): row for row in rows}
            for role, task_id in zip(_STAGE_ROLES, ordered_ids):
                row = by_identifier.get(task_id)
                if row is None or row["role"] != role:
                    raise CommandError("standalone pipeline task roles do not match")
                if row["status"] not in {"done", "blocked", "scheduled", "running"}:
                    raise CommandError("standalone pipeline task status is unknown")
            now = int(time.time())
            connection.execute(
                """
                UPDATE desktop_worker_task
                SET status = 'blocked', terminal_reason = ?, updated_at = ?
                WHERE identifier IN (?, ?, ?)
                  AND (status = 'scheduled' OR (? = 1 AND status = 'running'))
                """,
                (reason[:1000], now, *ordered_ids, int(cancel_running)),
            )
            for task_id in ordered_ids:
                if by_identifier[task_id]["status"] == "scheduled" or (
                    cancel_running and by_identifier[task_id]["status"] == "running"
                ):
                    connection.execute(
                        """
                        INSERT INTO desktop_worker_event (task_id, kind, detail, created_at)
                        VALUES (?, 'retired', ?, ?)
                        """,
                        (task_id, reason[:1000], now),
                    )
        self._require_active_lease()

    def comment_once(self, task_id: str, *, marker: str, text: str) -> bool:
        self._require_active_lease()
        row = self._row(task_id)
        base_prompt = str(row["base_prompt"] or row["prompt"])
        if marker in base_prompt or marker in str(row["output"] or ""):
            return False
        updated_prompt = f"{base_prompt.rstrip()}\n\n{text.strip()}\n"
        with self._connection() as connection:
            connection.execute(
                "UPDATE desktop_worker_task SET base_prompt = ?, prompt = ?, updated_at = ? WHERE identifier = ?",
                (updated_prompt, updated_prompt, int(time.time()), task_id),
            )
        self._require_active_lease()
        self._event(task_id, "comment", marker)
        return True


@dataclass(frozen=True)
class ControllerControl:
    paused: bool
    check_generation: int
    fence_generation: int
    cycle_active: bool
    updated_at: int


class ControllerControlStore:
    """Small durable control and cycle-audit store for the Desktop service."""

    def __init__(self, database: Path) -> None:
        self.database = database
        StateStore(database)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS controller_control (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
                    check_generation INTEGER NOT NULL CHECK (check_generation >= 0),
                    fence_generation INTEGER NOT NULL DEFAULT 0 CHECK (fence_generation >= 0),
                    cycle_active INTEGER NOT NULL DEFAULT 0 CHECK (cycle_active IN (0, 1)),
                    cycle_lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (cycle_lease_generation >= 0),
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO controller_control (
                    singleton, paused, check_generation, fence_generation, cycle_active,
                    cycle_lease_generation, updated_at
                ) VALUES (1, 1, 0, 0, 0, 0, 0)
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(controller_control)")
            }
            if "fence_generation" not in columns:
                connection.execute(
                    "ALTER TABLE controller_control ADD COLUMN fence_generation INTEGER NOT NULL DEFAULT 0"
                )
            if "cycle_active" not in columns:
                connection.execute(
                    "ALTER TABLE controller_control ADD COLUMN cycle_active INTEGER NOT NULL DEFAULT 0"
                )
            if "cycle_lease_generation" not in columns:
                connection.execute(
                    "ALTER TABLE controller_control ADD COLUMN cycle_lease_generation INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS controller_cycle (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('completed', 'failed', 'paused')),
                    event_count INTEGER NOT NULL CHECK (event_count >= 0),
                    summary TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot(self) -> ControllerControl:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT paused, check_generation, fence_generation, cycle_active, updated_at "
                "FROM controller_control WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("controller control state is unavailable")
        return ControllerControl(
            bool(row["paused"]),
            int(row["check_generation"]),
            int(row["fence_generation"]),
            bool(row["cycle_active"]),
            int(row["updated_at"]),
        )

    def set_paused(self, paused: bool) -> ControllerControl:
        if not isinstance(paused, bool):
            raise ValueError("controller pause state must be boolean")
        now = int(time.time())
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE controller_control
                SET fence_generation = fence_generation
                        + CASE WHEN paused = 0 AND ? = 1 THEN 1 ELSE 0 END,
                    paused = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (int(paused), int(paused), now),
            )
        return self.snapshot()

    def request_check(self) -> int:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE controller_control
                SET check_generation = check_generation + 1, updated_at = ?
                WHERE singleton = 1
                """,
                (now,),
            )
            row = connection.execute(
                "SELECT check_generation FROM controller_control WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("controller control state is unavailable")
        return int(row["check_generation"])

    def begin_cycle(
        self, *, owner_id: str, lease_generation: int, now: int
    ) -> bool:
        """Claim a cycle only for the current lease generation.

        A newer lease generation can replace a claim left by a terminated
        backend. The lease check and claim update share one transaction.
        """

        if not owner_id or lease_generation <= 0:
            raise ValueError("current controller lease ownership is required")

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute(
                """
                UPDATE controller_control
                SET cycle_active = 1, cycle_lease_generation = ?, updated_at = ?
                WHERE singleton = 1
                  AND paused = 0
                  AND (cycle_active = 0 OR cycle_lease_generation != ?)
                  AND EXISTS (
                      SELECT 1 FROM runtime_lease
                      WHERE name = 'standalone-controller'
                        AND owner_id = ?
                        AND generation = ?
                        AND expires_at > ?
                  )
                """,
                (
                    lease_generation,
                    now,
                    lease_generation,
                    owner_id,
                    lease_generation,
                    now,
                ),
            )
        return claimed.rowcount == 1

    def finish_cycle(self, *, lease_generation: int) -> None:
        """Release only the claim owned by this lease generation."""

        with self._connection() as connection:
            connection.execute(
                """
                UPDATE controller_control
                SET cycle_active = 0, cycle_lease_generation = 0, updated_at = ?
                WHERE singleton = 1 AND cycle_lease_generation = ?
                """,
                (int(time.time()), lease_generation),
            )

    def record_cycle(
        self,
        *,
        started_at: int,
        outcome: str,
        events: list[Event],
        summary: str,
    ) -> None:
        if outcome not in {"completed", "failed", "paused"}:
            raise ValueError("controller cycle outcome is invalid")
        safe_summary = " ".join(summary.split())[:2000] or outcome
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO controller_cycle (
                    started_at, finished_at, outcome, event_count, summary
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (started_at, int(time.time()), outcome, len(events), safe_summary),
            )
            connection.execute(
                """
                DELETE FROM controller_cycle
                WHERE sequence NOT IN (
                    SELECT sequence FROM controller_cycle ORDER BY sequence DESC LIMIT 200
                )
                """
            )


class StandaloneController(Controller):
    """Deterministic controller with local workers and local merge audit."""

    def __init__(
        self,
        config: Config,
        *,
        task_client: StandaloneTaskClient,
        runner: Any | None = None,
    ) -> None:
        super().__init__(config, runner=runner)
        self.kanban = task_client

    def _publish_pending_merge_history(self, *, dry_run: bool) -> list[Event]:
        events: list[Event] = []
        for record in self.store.pending_merge_history():
            if not dry_run:
                self.store.mark_merge_history_recorded(
                    record.repository,
                    record.number,
                    self._utc_timestamp(),
                    head_sha=record.head_sha,
                )
            events.append(
                Event(
                    record.repository,
                    record.number,
                    "would record local merge audit" if dry_run else "recorded local merge audit",
                )
            )
        return events


class ControllerService:
    """Run deterministic controller checks for the Desktop backend lifetime."""

    def __init__(
        self,
        controller: StandaloneController,
        *,
        interval_seconds: int = 30,
        poll_seconds: int = 2,
        lease_ttl_seconds: int = 90,
    ) -> None:
        if interval_seconds < 5 or poll_seconds < 1 or lease_ttl_seconds < 30:
            raise ValueError("controller service timing is outside safe bounds")
        self.controller = controller
        self.store = controller.store
        self.controls = ControllerControlStore(controller.config.state_path)
        self.interval_seconds = interval_seconds
        self.poll_seconds = poll_seconds
        self.lease_ttl_seconds = lease_ttl_seconds
        self.owner_id = f"desktop-{uuid4().hex}"
        self._lease_generation: int | None = None
        self._lease_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _acquire_or_renew(self) -> bool:
        if self._stop_event.is_set():
            return False
        with self._lease_lock:
            lease = self.store.acquire_runtime_lease(
                name="standalone-controller",
                owner_id=self.owner_id,
                now=int(time.time()),
                ttl_seconds=self.lease_ttl_seconds,
                renew_generation=self._lease_generation,
            )
            if lease is None:
                return False
            self._lease_generation = lease.generation
            return True

    def _has_lease(self) -> bool:
        with self._lease_lock:
            return self._lease_generation is not None

    def _lease_token(self) -> int | None:
        with self._lease_lock:
            return self._lease_generation

    def _forget_lease(self) -> None:
        with self._lease_lock:
            self._lease_generation = None

    def _release(self) -> None:
        with self._lease_lock:
            generation = self._lease_generation
            if generation is None:
                return
            self._lease_generation = None
        self.store.release_runtime_lease(
            name="standalone-controller",
            owner_id=self.owner_id,
            generation=generation,
        )

    def _lease_renewal_interval(self) -> float:
        return self.lease_ttl_seconds / 3

    def _renew_lease_during_cycle(
        self, cycle_finished: threading.Event, lease_lost: threading.Event
    ) -> None:
        """Renew ownership until a synchronous controller cycle returns."""

        deadline = time.monotonic() + self._lease_renewal_interval()
        while not cycle_finished.is_set():
            # Stop is also a command fence. Check it frequently enough that
            # unload does not wait for a full lease-renewal period before the
            # heartbeat stops writing lease state.
            if self._stop_event.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
                lease_lost.set()
                return
            if cycle_finished.is_set():
                return
            if time.monotonic() < deadline:
                continue
            try:
                renewed = self._acquire_or_renew()
            except Exception:
                self._forget_lease()
                lease_lost.set()
                _LOG.exception("PR Autopilot controller lease renewal failed")
                return
            if not renewed:
                self._forget_lease()
                lease_lost.set()
                _LOG.error("PR Autopilot controller lease was fenced during a cycle")
                return
            deadline = time.monotonic() + self._lease_renewal_interval()

    @contextmanager
    def _fence_controller_commands(
        self, lease_lost: threading.Event, fence_generation: int
    ) -> Iterator[None]:
        """Fence all controller command adapters as soon as lease renewal fails."""

        def control_fenced() -> bool:
            if self._stop_event.is_set():
                return True
            try:
                return self.controls.snapshot().fence_generation != fence_generation
            except Exception:
                return True

        original_runner = self.controller.runner
        original_store = self.controller.store
        fenced_runner = _LeaseFencedRunner(original_runner, lease_lost, control_fenced)
        fenced_store = _LeaseFencedStore(original_store, lease_lost, control_fenced)
        targets = (
            self.controller,
            self.controller.github,
            self.controller.workspaces,
            self.controller.kanban,
        )
        replaced: list[Any] = []
        for target in targets:
            if getattr(target, "runner", None) is original_runner:
                target.runner = fenced_runner
                replaced.append(target)
        self.controller.store = fenced_store
        task_client = self.controller.kanban
        set_fence = getattr(task_client, "set_lease_fence", None)
        set_control_fence = getattr(task_client, "set_control_fence", None)
        if callable(set_fence):
            set_fence(lease_lost)
        if callable(set_control_fence):
            set_control_fence(control_fenced)
        try:
            yield
        finally:
            if callable(set_fence):
                set_fence(None)
            if callable(set_control_fence):
                set_control_fence(None)
            if self.controller.store is fenced_store:
                self.controller.store = original_store
            for target in replaced:
                if getattr(target, "runner", None) is fenced_runner:
                    target.runner = original_runner

    def start(self) -> None:
        """Start one backend-owned thread; repeat calls are idempotent."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="plugin:pr-autopilot:controller",
        )
        self._thread.start()

    def stop(self) -> None:
        """Fence work and wait until controller and heartbeat writes have ended."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        if thread is None or not thread.is_alive():
            self._release()

    def run(self) -> None:
        """Run until plugin unload signals this backend-owned service."""

        next_cycle = 0.0
        next_renewal = 0.0
        seen_check_generation = -1
        try:
            while not self._stop_event.is_set():
                now_monotonic = time.monotonic()
                if not self._has_lease():
                    if not self._acquire_or_renew():
                        self._stop_event.wait(self.poll_seconds)
                        continue
                    _LOG.info("PR Autopilot controller lease acquired")
                    next_cycle = 0.0
                    next_renewal = now_monotonic + self._lease_renewal_interval()

                if now_monotonic >= next_renewal:
                    if not self._acquire_or_renew():
                        _LOG.error("PR Autopilot controller lease was fenced")
                        self._forget_lease()
                        self._stop_event.wait(self.poll_seconds)
                        continue
                    next_renewal = now_monotonic + self._lease_renewal_interval()

                control = self.controls.snapshot()
                if self._stop_event.is_set():
                    break
                requested = control.check_generation != seen_check_generation
                due = now_monotonic >= next_cycle
                if requested or due:
                    seen_check_generation = control.check_generation
                    started_at = int(time.time())
                    if control.paused:
                        self.controls.record_cycle(
                            started_at=started_at,
                            outcome="paused",
                            events=[],
                            summary="Controller check skipped because the operator paused Autopilot.",
                        )
                    else:
                        cycle_lease_generation = self._lease_token()
                        if cycle_lease_generation is None or not self.controls.begin_cycle(
                            owner_id=self.owner_id,
                            lease_generation=cycle_lease_generation,
                            now=int(time.time()),
                        ):
                            next_cycle = time.monotonic() + self.interval_seconds
                            self._stop_event.wait(self.poll_seconds)
                            continue
                        cycle_finished = threading.Event()
                        lease_lost = threading.Event()
                        heartbeat = threading.Thread(
                            target=self._renew_lease_during_cycle,
                            args=(cycle_finished, lease_lost),
                            daemon=True,
                            name="plugin:pr-autopilot:lease-heartbeat",
                        )
                        cycle_error: Exception | None = None
                        events: list[Event] = []
                        try:
                            heartbeat.start()
                            try:
                                with self._fence_controller_commands(
                                    lease_lost, control.fence_generation
                                ):
                                    events = self.controller.run(
                                        dry_run=False,
                                        verbose=True,
                                        force=requested,
                                    )
                            except Exception as error:
                                cycle_error = error
                                _LOG.exception("PR Autopilot controller check failed")
                        finally:
                            cycle_finished.set()
                            if heartbeat.is_alive():
                                heartbeat.join()
                            self.controls.finish_cycle(
                                lease_generation=cycle_lease_generation
                            )
                        if lease_lost.is_set():
                            _LOG.error(
                                "PR Autopilot controller cycle ended after its lease was fenced"
                            )
                            self._stop_event.set()
                            break
                        if cycle_error is not None:
                            safe_error = type(cycle_error).__name__
                            self.controls.record_cycle(
                                started_at=started_at,
                                outcome="failed",
                                events=[],
                                summary=f"Controller check failed: {safe_error}",
                            )
                        else:
                            summary = "; ".join(event.message for event in events[:20])
                            self.controls.record_cycle(
                                started_at=started_at,
                                outcome="completed",
                                events=events,
                                summary=summary or "Controller check completed with no state changes.",
                            )
                            for event in events:
                                _LOG.info(
                                    "%s#%s: %s",
                                    event.repository,
                                    event.number,
                                    event.message,
                                )
                    next_cycle = time.monotonic() + self.interval_seconds
                self._stop_event.wait(self.poll_seconds)
        finally:
            self._release()
            _LOG.info("PR Autopilot controller lease released")
