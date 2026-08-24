"""Plugin-neutral, backend-lifetime worker process ownership.

This service gives trusted plugins a small, deliberately constrained worker
launch surface. A plugin registers fixed definitions during registration. A
launch can select only policy-approved values; it cannot send a command line,
shell text, environment map, executable path, or arbitrary workspace.

The service is intentionally not an agent task scheduler. It owns child process
lifetime, redacted bounded audit data, and fail-closed recovery after a backend
restart. Domain policy belongs to the consuming plugin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Iterable

from agent.deadline import kill_process_tree
from hermes_cli._subprocess_compat import (
    IS_WINDOWS,
    windows_detach_flags_without_breakaway,
)
from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists
from hermes_constants import get_hermes_home


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_WORKSPACE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ALLOWED_SLOTS = frozenset({"{attempt_id}", "{work_item_ref}", "{workspace}", "{profile}", "{model}", "{reasoning}"})
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "indeterminate"})
_STARTUP_GATE_RELEASE = b"1"
_REDACTION_MARKER = b"[REDACTED]"


@dataclass(frozen=True)
class WorkerDefinition:
    """Trusted, immutable execution policy registered by one plugin."""

    definition_id: str
    argv: tuple[str, ...]
    allowed_profiles: tuple[str, ...]
    allowed_models: tuple[str, ...]
    allowed_reasoning: tuple[str, ...]
    workspace_root: Path
    max_runtime_seconds: int
    output_limit_bytes: int
    cancellation_grace_seconds: int


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    detail: str
    created_at: str


@dataclass(frozen=True)
class WorkerAttempt:
    attempt_id: str
    definition_id: str
    status: str
    settings: dict[str, str]
    output_digest: str | None
    exit_code: int | None
    started_at: str
    terminal_at: str | None


@dataclass(frozen=True)
class ShutdownResult:
    cancelled: int
    indeterminate: int


class _StreamingSecretRedactor:
    """Redact byte secrets without releasing a prefix at a pipe boundary."""

    def __init__(self, secrets: Iterable[str]) -> None:
        encoded = {
            secret.encode("utf-8")
            for secret in secrets
            if secret
        }
        self._secrets = tuple(sorted(encoded, key=len, reverse=True))
        self._longest_secret = max((len(secret) for secret in self._secrets), default=0)
        self._pending = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        """Return bytes that cannot begin a later secret match."""

        self._pending.extend(chunk)
        return self._drain(final=False)

    def finish(self) -> bytes:
        """Redact the final incomplete suffix without exposing a secret prefix."""

        return self._drain(final=True)

    def _drain(self, *, final: bool) -> bytes:
        if not self._secrets:
            result = bytes(self._pending)
            self._pending.clear()
            return result

        safe_end = len(self._pending) if final else max(0, len(self._pending) - self._longest_secret)
        position = 0
        result = bytearray()
        while position < safe_end:
            if final and any(
                candidate.startswith(bytes(self._pending[position:]))
                for candidate in self._secrets
            ):
                result.extend(_REDACTION_MARKER)
                position = len(self._pending)
                break
            secret = next(
                (
                    candidate
                    for candidate in self._secrets
                    if self._pending.startswith(candidate, position)
                ),
                None,
            )
            if secret is not None:
                result.extend(_REDACTION_MARKER)
                position += len(secret)
                continue
            result.append(self._pending[position])
            position += 1

        if position:
            del self._pending[:position]
        return bytes(result)


class PluginWorkerRuntime:
    """Own bounded worker processes and redacted durable audit state."""

    def __init__(self, *, data_root: Path | None = None) -> None:
        self.data_root = (data_root or get_hermes_home() / "plugin-data" / "worker-runtime").resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._database_path = self.data_root / "worker-runtime.sqlite3"
        self._definitions: dict[tuple[str, str], WorkerDefinition] = {}
        self._processes: dict[tuple[str, str], subprocess.Popen[bytes]] = {}
        # Admission is closed before an owner snapshot is taken for unload.
        # The same lock covers definition lookup through process registration,
        # so an in-flight launch is either included in that snapshot or
        # rejected before it can spawn.
        self._owners_closing: set[str] = set()
        self._definitions_closing: set[tuple[str, str]] = set()
        # Incremented on each fresh definition lifecycle and when unload starts.
        # A launch records the generation it read, then rechecks it before
        # spawning; it can never cross an unload/reload boundary.
        self._owner_generations: dict[str, int] = {}
        self._shutting_down = False
        self._shutdown_cleanup_started = False
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                    owner TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    definition_id TEXT NOT NULL,
                    work_item_ref TEXT NOT NULL,
                    workspace_relative TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    argv_json TEXT NOT NULL,
                    pid INTEGER,
                    pid_created_at REAL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    output_digest TEXT,
                    output_preview TEXT,
                    started_at TEXT NOT NULL,
                    terminal_at TEXT,
                    cancel_reason TEXT,
                    PRIMARY KEY (owner, attempt_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    owner TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (owner, attempt_id, sequence)
                )
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _require_identifier(value: str, field: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{field} must be a safe opaque identifier")
        return value

    @staticmethod
    def _normalized_reasoning(value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"none", "low", "medium", "high", "xhigh"}:
            raise ValueError("reasoning is not a supported reasoning level")
        return normalized

    @staticmethod
    def _validate_definition(definition: WorkerDefinition) -> WorkerDefinition:
        PluginWorkerRuntime._require_identifier(definition.definition_id, "definition id")
        if not definition.argv or any(not isinstance(part, str) or not part for part in definition.argv):
            raise ValueError("worker definition requires a fixed argv array")
        for token in definition.argv:
            if "{" not in token and "}" not in token:
                continue
            if token not in _ALLOWED_SLOTS:
                raise ValueError("worker argv contains an unsupported template slot")
        if not definition.allowed_profiles or not definition.allowed_models or not definition.allowed_reasoning:
            raise ValueError("worker definition requires non-empty setting allowlists")
        profiles = tuple(normalize_profile_name(profile) for profile in definition.allowed_profiles)
        models = tuple(PluginWorkerRuntime._require_identifier(model, "model") for model in definition.allowed_models)
        reasoning = tuple(PluginWorkerRuntime._normalized_reasoning(level) for level in definition.allowed_reasoning)
        root = definition.workspace_root.resolve()
        if not root.is_dir():
            raise ValueError("worker workspace root does not exist")
        if (
            isinstance(definition.max_runtime_seconds, bool)
            or definition.max_runtime_seconds < 1
            or definition.max_runtime_seconds > 86_400
            or isinstance(definition.output_limit_bytes, bool)
            or definition.output_limit_bytes < 128
            or definition.output_limit_bytes > 1_048_576
            or isinstance(definition.cancellation_grace_seconds, bool)
            or definition.cancellation_grace_seconds < 1
            or definition.cancellation_grace_seconds > 60
        ):
            raise ValueError("worker definition has an invalid runtime bound")
        return WorkerDefinition(
            definition_id=definition.definition_id,
            argv=tuple(definition.argv),
            allowed_profiles=profiles,
            allowed_models=models,
            allowed_reasoning=reasoning,
            workspace_root=root,
            max_runtime_seconds=definition.max_runtime_seconds,
            output_limit_bytes=definition.output_limit_bytes,
            cancellation_grace_seconds=definition.cancellation_grace_seconds,
        )

    def register_definition(self, owner: str, definition: WorkerDefinition) -> None:
        owner = self._require_identifier(owner, "plugin owner")
        definition = self._validate_definition(definition)
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("plugin worker runtime is shutting down")
            if owner in self._owners_closing:
                raise RuntimeError("worker admission is closed for this plugin owner")
            # ``get_plugin_worker_runtime`` reconciles a fresh backend before
            # registration. Keep the public runtime class safe too: callers
            # that construct it directly cannot adopt an owner whose prior
            # process still has unresolved durable PID evidence.
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT attempt_id FROM attempts WHERE owner = ? AND ("
                    "status NOT IN ('succeeded', 'failed', 'cancelled', 'indeterminate') "
                    "OR (status = 'indeterminate' AND pid IS NOT NULL))",
                    (owner,),
                ).fetchall()
            # An active process owned by this instance is not a stale-backend
            # admission risk. A fresh runtime has no tracked processes and
            # therefore still fails closed until startup reconciliation.
            unresolved = any(
                (owner, str(row["attempt_id"])) not in self._processes
                for row in rows
            )
            if unresolved:
                self._owners_closing.add(owner)
                raise RuntimeError("worker admission is closed for this plugin owner")
            key = (owner, definition.definition_id)
            if key in self._definitions_closing:
                raise RuntimeError("worker definition admission is closed")
            current = self._definitions.get(key)
            if current is not None:
                raise ValueError("worker definition is already registered")
            if current is None and not any(existing_owner == owner for existing_owner, _ in self._definitions):
                self._owner_generations[owner] = self._owner_generations.get(owner, 0) + 1
            self._definitions[key] = definition

    def definition(self, owner: str, definition_id: str) -> WorkerDefinition | None:
        return self._definitions.get((owner, definition_id))

    def _definition(self, owner: str, definition_id: str) -> WorkerDefinition:
        try:
            return self._definitions[(owner, definition_id)]
        except KeyError as error:
            raise ValueError("worker definition is not registered for this plugin") from error

    @staticmethod
    def _process_started_at(pid: int) -> float | None:
        try:
            import psutil

            return float(psutil.Process(pid).create_time())
        except Exception:
            return None

    @staticmethod
    def _process_identity_state(pid: int, created_at: float | None) -> bool | None:
        """Return live, gone, or unknown for one durable process identity.

        ``None`` is intentionally distinct from a gone process. Missing
        process-inspection capability must keep worker admission closed.
        """

        if pid <= 0 or created_at is None:
            return None
        try:
            import psutil
        except Exception:
            return None
        try:
            actual_created_at = float(psutil.Process(pid).create_time())
        except psutil.NoSuchProcess:
            return False
        except Exception:
            return None
        return abs(actual_created_at - created_at) < 0.01

    @staticmethod
    def _same_process_identity(pid: int, created_at: float | None) -> bool:
        return PluginWorkerRuntime._process_identity_state(pid, created_at) is True

    @staticmethod
    def _secret_values() -> tuple[str, ...]:
        return tuple(
            value
            for key, value in os.environ.items()
            if value and any(marker in key.upper() for marker in ("SECRET", "TOKEN", "PASSWORD", "API_KEY"))
        )

    @staticmethod
    def _bounded_redacted(text: str, *, limit: int, secrets: Iterable[str]) -> str:
        redacted = text
        for secret in secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        encoded = redacted.encode("utf-8", errors="replace")
        if len(encoded) <= limit:
            return redacted
        return encoded[: max(0, limit - 16)].decode("utf-8", errors="ignore") + "\n[TRUNCATED]"

    def _append_event(self, connection: sqlite3.Connection, owner: str, attempt_id: str, kind: str, detail: str) -> None:
        next_sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE owner = ? AND attempt_id = ?",
            (owner, attempt_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO events (owner, attempt_id, sequence, kind, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (owner, attempt_id, next_sequence, kind, detail, self._now()),
        )

    def _snapshot(self, row: sqlite3.Row) -> WorkerAttempt:
        return WorkerAttempt(
            attempt_id=str(row["attempt_id"]),
            definition_id=str(row["definition_id"]),
            status=str(row["status"]),
            settings={
                "profile": str(row["profile"]),
                "model": str(row["model"]),
                "reasoning": str(row["reasoning"]),
            },
            output_digest=str(row["output_digest"]) if row["output_digest"] else None,
            exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
            started_at=str(row["started_at"]),
            terminal_at=str(row["terminal_at"]) if row["terminal_at"] else None,
        )

    def _resolve_workspace(self, definition: WorkerDefinition, workspace_relative: str) -> Path:
        if not isinstance(workspace_relative, str) or not workspace_relative or len(workspace_relative) > 384:
            raise ValueError("workspace must be an allowed relative directory path")
        relative = Path(workspace_relative)
        parts = relative.parts
        root = definition.workspace_root.resolve()
        if relative.is_absolute():
            # Reject absolute renderer input even if it happens to point under
            # the root. Report a resolved escape separately so callers cannot
            # mistake an out-of-root absolute path for a valid relative slot.
            try:
                relative.resolve().relative_to(root)
            except ValueError as error:
                raise ValueError("workspace escapes the registered workspace root") from error
            raise ValueError("workspace must be an allowed relative directory path")
        if (
            not 1 <= len(parts) <= 4
            or any(not _WORKSPACE_COMPONENT.fullmatch(part) for part in parts)
        ):
            raise ValueError("workspace must be an allowed relative directory path")
        workspace = (root / relative).resolve()
        try:
            workspace.relative_to(root)
        except ValueError as error:
            raise ValueError("workspace escapes the registered workspace root") from error
        if not workspace.is_dir():
            raise ValueError("workspace does not exist")
        return workspace

    @staticmethod
    def _startup_gate_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
        """Run fixed worker argv only after the parent releases its pipe gate."""

        return (
            sys.executable,
            str(Path(__file__).resolve()),
            "--startup-gate",
            "--",
            *argv,
        )

    @staticmethod
    def _close_startup_gate(process: subprocess.Popen[bytes], *, release: bool) -> bool:
        """Release or fail closed one child that is still blocked at startup."""

        stream = process.stdin
        if stream is None:
            return False
        try:
            if release:
                stream.write(_STARTUP_GATE_RELEASE)
                stream.flush()
            return True
        except OSError:
            return False
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _stop_unreleased_process(process: subprocess.Popen[bytes]) -> None:
        """Close the gate first; tree termination is only a bounded backstop."""

        PluginWorkerRuntime._close_startup_gate(process, release=False)
        try:
            kill_process_tree(process.pid)
        except OSError:
            pass

    def launch(
        self,
        owner: str,
        definition_id: str,
        *,
        attempt_id: str,
        work_item_ref: str,
        workspace_relative: str,
        profile: str,
        model: str,
        reasoning: str,
    ) -> WorkerAttempt:
        owner = self._require_identifier(owner, "plugin owner")
        attempt_id = self._require_identifier(attempt_id, "attempt id")
        work_item_ref = self._require_identifier(work_item_ref, "work item")
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("plugin worker runtime is shutting down")
            if owner in self._owners_closing:
                raise RuntimeError("worker admission is closed for this plugin owner")
            if (owner, definition_id) in self._definitions_closing:
                raise RuntimeError("worker definition admission is closed")
            definition = self._definition(owner, definition_id)
            owner_generation = self._owner_generations.get(owner, 0)
        profile = normalize_profile_name(profile)
        model = self._require_identifier(model, "model")
        reasoning = self._normalized_reasoning(reasoning)
        if profile not in definition.allowed_profiles or not profile_exists(profile):
            raise ValueError("profile is not allowed by this worker definition")
        if model not in definition.allowed_models:
            raise ValueError("model is not allowed by this worker definition")
        if reasoning not in definition.allowed_reasoning:
            raise ValueError("reasoning is not allowed by this worker definition")
        workspace = self._resolve_workspace(definition, workspace_relative)
        values = {
            "{attempt_id}": attempt_id,
            "{work_item_ref}": work_item_ref,
            "{workspace}": str(workspace),
            "{profile}": profile,
            "{model}": model,
            "{reasoning}": reasoning,
        }
        argv = tuple(values.get(part, part) for part in definition.argv)
        key = (owner, attempt_id)
        immutable = (definition_id, work_item_ref, workspace_relative, profile, model, reasoning, json.dumps(argv))
        # Persist the pre-spawn phase before Popen. A PID-less ``launching`` row
        # is proof that no released worker can exist: the bootstrap gate is not
        # created until after this transaction commits.
        with self._lock, self._connect() as connection:
            if (
                self._shutting_down
                or owner in self._owners_closing
                or (owner, definition_id) in self._definitions_closing
                or self._owner_generations.get(owner, 0) != owner_generation
                or self._definitions.get((owner, definition_id)) != definition
            ):
                raise RuntimeError("worker admission is closed for this plugin owner")
            row = connection.execute(
                "SELECT * FROM attempts WHERE owner = ? AND attempt_id = ?", key
            ).fetchone()
            if row is not None:
                existing = (
                    row["definition_id"], row["work_item_ref"], row["workspace_relative"],
                    row["profile"], row["model"], row["reasoning"], row["argv_json"],
                )
                if existing != immutable:
                    raise ValueError("attempt id belongs to a different immutable launch")
                return self._snapshot(row)
            connection.execute(
                """
                INSERT INTO attempts (
                    owner, attempt_id, definition_id, work_item_ref, workspace_relative,
                    profile, model, reasoning, argv_json, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'launching', ?)
                """,
                (*key, definition_id, work_item_ref, workspace_relative, profile, model, reasoning, json.dumps(argv), self._now()),
            )
            self._append_event(connection, owner, attempt_id, "launch_recorded", "launch admitted")

        try:
            child_environment = os.environ.copy()
            child_environment["HERMES_HOME"] = str(get_profile_dir(profile))
            popen_kwargs: dict[str, object] = {
                "cwd": str(workspace),
                "env": child_environment,
                # The trusted bootstrap consumes this pipe. EOF means that the
                # parent died or rejected admission, so it exits before exec.
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
            }
            if IS_WINDOWS:
                popen_kwargs["creationflags"] = windows_detach_flags_without_breakaway()
            else:
                popen_kwargs["start_new_session"] = True
            process = subprocess.Popen(list(self._startup_gate_argv(argv)), **popen_kwargs)
        except OSError as error:
            with self._lock, self._connect() as connection:
                connection.execute(
                    "UPDATE attempts SET status = 'failed', terminal_at = ? WHERE owner = ? AND attempt_id = ? AND status = 'launching'",
                    (self._now(), owner, attempt_id),
                )
                self._append_event(connection, owner, attempt_id, "spawn_failed", type(error).__name__)
            raise RuntimeError("worker process could not start") from error

        started_identity = self._process_started_at(process.pid)
        if started_identity is None:
            self._stop_unreleased_process(process)
            with self._lock, self._connect() as connection:
                connection.execute(
                    "UPDATE attempts SET status = 'failed', terminal_at = ? WHERE owner = ? AND attempt_id = ? AND status = 'launching'",
                    (self._now(), owner, attempt_id),
                )
                self._append_event(connection, owner, attempt_id, "spawn_identity_unavailable", "worker was not released")
            raise RuntimeError("worker process identity is unavailable")

        admitted = False
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT status FROM attempts WHERE owner = ? AND attempt_id = ?", key
                ).fetchone()
                if (
                    row is not None
                    and str(row["status"]) == "launching"
                    and not self._shutting_down
                    and owner not in self._owners_closing
                    and (owner, definition_id) not in self._definitions_closing
                    and self._owner_generations.get(owner, 0) == owner_generation
                    and self._definitions.get((owner, definition_id)) == definition
                ):
                    connection.execute(
                        "UPDATE attempts SET status = 'starting', pid = ?, pid_created_at = ? WHERE owner = ? AND attempt_id = ?",
                        (process.pid, started_identity, owner, attempt_id),
                    )
                    self._append_event(connection, owner, attempt_id, "spawn_recorded", "worker process is durably gated")
                    self._processes[key] = process
                    admitted = True
                elif row is not None and str(row["status"]) == "launching":
                    connection.execute(
                        "UPDATE attempts SET status = 'cancelled', terminal_at = ? WHERE owner = ? AND attempt_id = ?",
                        (self._now(), owner, attempt_id),
                    )
                    self._append_event(connection, owner, attempt_id, "launch_rejected", "worker was not released")
        except BaseException:
            # The bootstrap remains blocked until this exact transaction commits.
            # Do not leave it alive if SQLite rejects the PID ownership record.
            self._stop_unreleased_process(process)
            self._processes.pop(key, None)
            raise
        if not admitted:
            self._stop_unreleased_process(process)
            raise RuntimeError("worker admission is closed for this plugin owner")

        # Keep the runtime lock while releasing. An unload either rejects this
        # attempt before release or sees its durable PID and can cancel it.
        with self._lock:
            if (
                self._shutting_down
                or owner in self._owners_closing
                or (owner, definition_id) in self._definitions_closing
                or self._owner_generations.get(owner, 0) != owner_generation
                or self._definitions.get((owner, definition_id)) != definition
                or not self._close_startup_gate(process, release=True)
            ):
                self._stop_unreleased_process(process)
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE attempts SET status = 'cancelled', terminal_at = ? WHERE owner = ? AND attempt_id = ? AND status IN ('launching', 'starting')",
                        (self._now(), owner, attempt_id),
                    )
                    self._append_event(connection, owner, attempt_id, "launch_rejected", "worker was not released")
                self._processes.pop(key, None)
                raise RuntimeError("worker admission is closed for this plugin owner")
            with self._connect() as connection:
                connection.execute(
                    "UPDATE attempts SET status = 'running' WHERE owner = ? AND attempt_id = ? AND status = 'starting'",
                    key,
                )
                self._append_event(connection, owner, attempt_id, "released", "worker process released after durable registration")
        threading.Thread(
            target=self._monitor,
            args=(owner, attempt_id, definition, process),
            daemon=True,
            name=f"plugin-worker:{owner}:{attempt_id}",
        ).start()
        return self.observe(owner, attempt_id)

    def _monitor(self, owner: str, attempt_id: str, definition: WorkerDefinition, process: subprocess.Popen[bytes]) -> None:
        """Wait for one worker while retaining at most its configured output bound.

        ``communicate()`` collects the entire stream before callers can truncate
        it. A worker can therefore exhaust backend memory before an after-the-
        fact output cap runs. Read binary chunks instead and terminate the whole
        owned process tree immediately when the next chunk would exceed policy.
        """
        output = bytearray()
        raw_output_bytes = 0
        redactor = _StreamingSecretRedactor(self._secret_values())
        output_exceeded = threading.Event()
        reader_done = threading.Event()
        reader_failed = threading.Event()

        def _capture_redacted(chunk: bytes) -> None:
            """Keep the redacted audit buffer bounded even when markers grow."""

            remaining = definition.output_limit_bytes - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])

        def _read_output() -> None:
            nonlocal raw_output_bytes
            stream = process.stdout
            if stream is None:
                reader_done.set()
                return
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        return
                    raw_output_bytes += len(chunk)
                    _capture_redacted(redactor.feed(chunk))
                    if raw_output_bytes > definition.output_limit_bytes:
                        output_exceeded.set()
                        # The outcome is final as soon as the byte policy is
                        # crossed. Persist it before Windows whole-tree cleanup,
                        # which can take longer than one observation interval.
                        with self._lock, self._connect() as connection:
                            updated = connection.execute(
                                "UPDATE attempts SET status = 'failed', terminal_at = ? "
                                "WHERE owner = ? AND attempt_id = ? "
                                "AND status NOT IN ('succeeded', 'failed', 'cancelled', 'indeterminate')",
                                (self._now(), owner, attempt_id),
                            )
                            if updated.rowcount == 1:
                                self._append_event(
                                    connection,
                                    owner,
                                    attempt_id,
                                    "output_limit_exceeded",
                                    "worker exceeded its configured output limit",
                                )
                        kill_process_tree(process.pid)
                        return
            except Exception:
                reader_failed.set()
            finally:
                # Do not leave a truncated secret prefix in a durable preview
                # when the process ends between two pipe reads.
                _capture_redacted(redactor.finish())
                reader_done.set()

        reader = threading.Thread(
            target=_read_output,
            daemon=True,
            name=f"plugin-worker-output:{owner}:{attempt_id}",
        )
        reader.start()
        timed_out = False
        monitor_failed = False
        try:
            process.wait(timeout=definition.max_runtime_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(process.pid)
        except Exception:
            monitor_failed = True
            kill_process_tree(process.pid)
        try:
            process.wait(timeout=definition.cancellation_grace_seconds)
        except Exception:
            # A child which remains alive can also keep its inherited output
            # handle open. Kill the owned tree once more, then fail closed.
            kill_process_tree(process.pid)
            monitor_failed = True

        # Do not classify a zero exit until the reader has observed all bytes.
        # Otherwise a finite oversized write can race process.wait() and be
        # persisted as success before ``output_exceeded`` is set.
        reader_drained = reader_done.wait(definition.cancellation_grace_seconds)
        if not reader_drained:
            kill_process_tree(process.pid)
            reader_drained = reader_done.wait(definition.cancellation_grace_seconds)

        if timed_out:
            status = "indeterminate"
            event_kind = "timed_out"
            detail = "worker exceeded its configured runtime"
        elif output_exceeded.is_set():
            status = "failed"
            event_kind = "output_limit_exceeded"
            detail = "worker exceeded its configured output limit"
        elif monitor_failed:
            status = "indeterminate"
            event_kind = "monitor_failed"
            detail = "worker monitor failed"
        elif not reader_drained or reader_failed.is_set():
            status = "indeterminate"
            event_kind = "output_reader_failed"
            detail = "worker output could not be drained safely"
        else:
            status = "succeeded" if process.returncode == 0 else "failed"
            event_kind = "exited"
            detail = f"worker exited with code {process.returncode}"
        decoded_output = bytes(output).decode("utf-8", errors="replace")
        preview = self._bounded_redacted(
            decoded_output, limit=definition.output_limit_bytes, secrets=self._secret_values()
        )
        digest = hashlib.sha256(preview.encode("utf-8")).hexdigest()
        exit_code = process.returncode
        # An indeterminate result is only terminal for admission when this
        # runtime confirms that the direct bootstrap process is gone. Keep the
        # handle and close the owner when it remains alive after bounded tree
        # termination; a later backend must reconcile its durable PID evidence.
        try:
            termination_confirmed = process.poll() is not None
        except Exception:
            termination_confirmed = False
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM attempts WHERE owner = ? AND attempt_id = ?", (owner, attempt_id)
            ).fetchone()
            if row is None:
                return
            prior_status = str(row["status"])
            if prior_status in _TERMINAL_STATUSES:
                if prior_status == "failed" and output_exceeded.is_set():
                    connection.execute(
                        """
                        UPDATE attempts
                        SET exit_code = ?, output_digest = ?, output_preview = ?
                        WHERE owner = ? AND attempt_id = ?
                        """,
                        (exit_code, digest, preview, owner, attempt_id),
                    )
                    if preview:
                        self._append_event(connection, owner, attempt_id, "output", preview)
                if prior_status == "indeterminate" and not termination_confirmed:
                    self._owners_closing.add(owner)
                    return
                if prior_status == "indeterminate":
                    connection.execute(
                        "UPDATE attempts SET pid = NULL, pid_created_at = NULL "
                        "WHERE owner = ? AND attempt_id = ?",
                        (owner, attempt_id),
                    )
                self._processes.pop((owner, attempt_id), None)
                return
            if prior_status == "cancelling":
                if termination_confirmed:
                    status = "cancelled"
                    event_kind = "cancelled"
                    detail = "worker cancelled by owner"
                else:
                    status = "indeterminate"
                    event_kind = "cancellation_indeterminate"
                    detail = "worker did not confirm exit after cancellation"
            connection.execute(
                """
                UPDATE attempts
                SET status = ?, exit_code = ?, output_digest = ?, output_preview = ?, terminal_at = ?
                WHERE owner = ? AND attempt_id = ? AND status NOT IN ('succeeded', 'failed', 'cancelled', 'indeterminate')
                """,
                (status, exit_code, digest, preview, self._now(), owner, attempt_id),
            )
            self._append_event(connection, owner, attempt_id, event_kind, detail)
            if preview:
                self._append_event(connection, owner, attempt_id, "output", preview)
            if status == "indeterminate" and not termination_confirmed:
                self._owners_closing.add(owner)
            else:
                if status == "indeterminate":
                    connection.execute(
                        "UPDATE attempts SET pid = NULL, pid_created_at = NULL "
                        "WHERE owner = ? AND attempt_id = ?",
                        (owner, attempt_id),
                    )
                self._processes.pop((owner, attempt_id), None)

    def observe(self, owner: str, attempt_id: str) -> WorkerAttempt:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE owner = ? AND attempt_id = ?", (owner, attempt_id)
            ).fetchone()
        if row is None:
            raise KeyError("worker attempt does not exist")
        return self._snapshot(row)

    def list_events(self, owner: str, attempt_id: str, *, limit: int = 100) -> list[WorkerEvent]:
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ValueError("event limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT kind, detail, created_at FROM events WHERE owner = ? AND attempt_id = ? ORDER BY sequence LIMIT ?",
                (owner, attempt_id, limit),
            ).fetchall()
        return [WorkerEvent(str(row["kind"]), str(row["detail"]), str(row["created_at"])) for row in rows]

    def cancel(self, owner: str, attempt_id: str, *, reason: str) -> WorkerAttempt:
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise ValueError("cancellation reason is invalid")
        key = (owner, attempt_id)
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE owner = ? AND attempt_id = ?", key).fetchone()
            if row is None:
                raise KeyError("worker attempt does not exist")
            if str(row["status"]) in _TERMINAL_STATUSES:
                raise ValueError("cannot cancel a terminal worker attempt")
            connection.execute(
                "UPDATE attempts SET status = 'cancelling', cancel_reason = ? WHERE owner = ? AND attempt_id = ?",
                (reason, owner, attempt_id),
            )
            self._append_event(connection, owner, attempt_id, "cancel_requested", reason)
            process = self._processes.get(key)
            pid = int(row["pid"] or 0)
            created_at = row["pid_created_at"]
            grace_seconds = self._definition(
                owner, str(row["definition_id"])
            ).cancellation_grace_seconds
        deadline = time.monotonic() + grace_seconds
        if process is not None:
            if process.poll() is None:
                try:
                    kill_process_tree(process.pid)
                except Exception:
                    # The process can exit between poll and tree discovery.
                    # The bounded wait and final poll below determine whether
                    # cancellation is confirmed or indeterminate.
                    pass
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except Exception:
                pass
            if process.poll() is not None:
                with self._connect() as connection:
                    updated = connection.execute(
                        "UPDATE attempts SET status = 'cancelled', terminal_at = ? WHERE owner = ? AND attempt_id = ? AND status = 'cancelling'",
                        (self._now(), owner, attempt_id),
                    )
                    if updated.rowcount == 1:
                        self._append_event(
                            connection, owner, attempt_id, "cancelled", "worker exited after cancellation"
                        )
                return self.observe(owner, attempt_id)
        elif self._same_process_identity(pid, float(created_at) if created_at is not None else None):
            kill_process_tree(pid)
        while True:
            snapshot = self.observe(owner, attempt_id)
            if snapshot.status in _TERMINAL_STATUSES:
                return snapshot
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            threading.Event().wait(min(0.05, remaining))
        if snapshot.status == "cancelling":
            with self._connect() as connection:
                connection.execute(
                    "UPDATE attempts SET status = 'indeterminate', terminal_at = ? WHERE owner = ? AND attempt_id = ? AND status = 'cancelling'",
                    (self._now(), owner, attempt_id),
                )
                self._append_event(connection, owner, attempt_id, "cancellation_indeterminate", "worker did not confirm exit within grace period")
            snapshot = self.observe(owner, attempt_id)
        return snapshot

    def unregister_definition(
        self,
        owner: str,
        definition_id: str,
        *,
        reason: str,
    ) -> ShutdownResult:
        """Remove one worker policy without disturbing sibling definitions.

        A registration handle represents one definition. Closing that key before
        the attempt snapshot prevents a launch that already resolved policy from
        spawning after the definition has been disposed.
        """
        owner = self._require_identifier(owner, "plugin owner")
        definition_id = self._require_identifier(definition_id, "definition id")
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise ValueError("cancellation reason is invalid")
        key = (owner, definition_id)
        with self._lock:
            if owner in self._owners_closing or key in self._definitions_closing:
                return ShutdownResult(cancelled=0, indeterminate=0)
            if key not in self._definitions:
                return ShutdownResult(cancelled=0, indeterminate=0)
            self._definitions_closing.add(key)
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT attempt_id FROM attempts WHERE owner = ? AND definition_id = ? "
                    "AND status NOT IN ('succeeded', 'failed', 'cancelled', 'indeterminate')",
                    (owner, definition_id),
                ).fetchall()
        cancelled = 0
        indeterminate = 0
        try:
            for row in rows:
                try:
                    result = self.cancel(owner, str(row["attempt_id"]), reason=reason)
                    cancelled += 1
                    if result.status == "indeterminate":
                        indeterminate += 1
                except (KeyError, ValueError):
                    continue
            return ShutdownResult(cancelled=cancelled, indeterminate=indeterminate)
        finally:
            with self._lock:
                self._definitions.pop(key, None)
                self._definitions_closing.discard(key)
                with self._connect() as connection:
                    unresolved = connection.execute(
                        "SELECT 1 FROM attempts WHERE owner = ? "
                        "AND definition_id = ? AND status = 'indeterminate' "
                        "AND pid IS NOT NULL LIMIT 1",
                        (owner, definition_id),
                    ).fetchone()
                if unresolved is not None:
                    # A cancellation timeout does not prove that its process
                    # stopped. Keep all owner admission closed until a new
                    # backend reconciles the durable process identity.
                    self._owners_closing.add(owner)

    def unregister_owner(self, owner: str, *, reason: str) -> ShutdownResult:
        """Close an owner's admission gate, cancel all current work, and remove policy.

        The runtime closes admission and takes the attempt snapshot under its
        lock. It releases the lock before cancellation waits so worker monitors
        can persist a terminal cancelled state. The closing gate prevents a
        launch from crossing the unload boundary.
        """
        with self._lock:
            if owner in self._owners_closing:
                return ShutdownResult(cancelled=0, indeterminate=0)
            self._owners_closing.add(owner)
            self._owner_generations[owner] = self._owner_generations.get(owner, 0) + 1
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT attempt_id FROM attempts WHERE owner = ? AND status NOT IN "
                    "('succeeded', 'failed', 'cancelled', 'indeterminate')",
                    (owner,),
                ).fetchall()
        cancelled = 0
        indeterminate = 0
        try:
            for row in rows:
                try:
                    result = self.cancel(owner, str(row["attempt_id"]), reason=reason)
                    cancelled += 1
                    if result.status == "indeterminate":
                        indeterminate += 1
                except (KeyError, ValueError):
                    continue
            return ShutdownResult(cancelled=cancelled, indeterminate=indeterminate)
        finally:
            with self._lock:
                for key in tuple(self._definitions):
                    if key[0] == owner:
                        self._definitions.pop(key, None)
                with self._connect() as connection:
                    unresolved = connection.execute(
                        "SELECT 1 FROM attempts WHERE owner = ? "
                        "AND status = 'indeterminate' AND pid IS NOT NULL LIMIT 1",
                        (owner,),
                    ).fetchone()
                if unresolved is None:
                    self._owners_closing.discard(owner)

    def close_admission(self) -> None:
        """Permanently reject new registrations and launches for this runtime.

        Backend shutdown closes every retained runtime before it starts draining
        any one of them. Cleanup stays separate so a pre-closed runtime still
        cancels its owned workers when ``shutdown`` follows.
        """
        with self._lock:
            self._shutting_down = True

    def shutdown(self, *, reason: str) -> ShutdownResult:
        """Permanently close this backend-owned runtime and revoke every owner.

        This also closes retained runtime references. It is intentionally
        separate from the process-global registry gate, which cannot stop code
        that already holds a direct reference to this instance.
        """
        with self._lock:
            if self._shutdown_cleanup_started:
                return ShutdownResult(cancelled=0, indeterminate=0)
            self._shutting_down = True
            self._shutdown_cleanup_started = True
            owners = {owner for owner, _ in self._definitions}
            owners.update(owner for owner, _ in self._processes)
            with self._connect() as connection:
                owners.update(
                    str(row["owner"])
                    for row in connection.execute(
                        "SELECT DISTINCT owner FROM attempts WHERE status NOT IN "
                        "('succeeded', 'failed', 'cancelled', 'indeterminate')"
                    ).fetchall()
                )
        cancelled = 0
        indeterminate = 0
        for owner in owners:
            result = self.unregister_owner(owner, reason=reason)
            cancelled += result.cancelled
            indeterminate += result.indeterminate
        return ShutdownResult(cancelled=cancelled, indeterminate=indeterminate)

    def reconcile_startup(self) -> int:
        """Reconcile every old process that could still execute worker code.

        An indeterminate cancellation is not proof of process death. A new
        backend therefore inspects those durable identities too. It reopens an
        owner only when the old process is proven gone; unknown inspection or
        a surviving process keeps that owner's admission closed.
        """

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT owner, attempt_id, status, pid, pid_created_at FROM attempts "
                "WHERE status NOT IN ('succeeded', 'failed', 'cancelled', 'indeterminate') "
                "OR (status = 'indeterminate' AND pid IS NOT NULL)"
            ).fetchall()
            unresolved_owners: set[str] = set()
            for row in rows:
                owner = str(row["owner"])
                pid = int(row["pid"] or 0)
                created_at = row["pid_created_at"]
                # ``launching`` is the durable pre-spawn phase. The parent
                # records it before Popen; after Popen, the unreleased gate
                # prevents the target command from executing until a PID is
                # committed with status ``starting``. A crash in this phase
                # therefore has no live worker to fence or terminate.
                if (
                    str(row["status"]) == "launching"
                    and pid == 0
                    and created_at is None
                ):
                    connection.execute(
                        "UPDATE attempts SET status = 'cancelled', terminal_at = ? "
                        "WHERE owner = ? AND attempt_id = ? AND status = 'launching' "
                        "AND pid IS NULL AND pid_created_at IS NULL",
                        (self._now(), owner, row["attempt_id"]),
                    )
                    self._append_event(
                        connection,
                        owner,
                        str(row["attempt_id"]),
                        "restart_pre_spawn",
                        "previous backend stopped before worker spawn",
                    )
                    continue
                identity = self._process_identity_state(
                    pid, float(created_at) if created_at is not None else None
                )
                if identity is True:
                    try:
                        kill_process_tree(pid)
                    except Exception:
                        pass
                    identity = self._process_identity_state(
                        pid, float(created_at) if created_at is not None else None
                    )
                if identity is not False:
                    unresolved_owners.add(owner)
                if identity is False:
                    connection.execute(
                        "UPDATE attempts SET status = 'indeterminate', terminal_at = ?, "
                        "pid = NULL, pid_created_at = NULL "
                        "WHERE owner = ? AND attempt_id = ?",
                        (self._now(), row["owner"], row["attempt_id"]),
                    )
                    self._append_event(
                        connection,
                        owner,
                        str(row["attempt_id"]),
                        "restart_reconciled",
                        "previous backend process is confirmed stopped",
                    )
                else:
                    connection.execute(
                        "UPDATE attempts SET status = 'indeterminate', terminal_at = ? "
                        "WHERE owner = ? AND attempt_id = ?",
                        (self._now(), row["owner"], row["attempt_id"]),
                    )
                    self._append_event(
                        connection,
                        owner,
                        str(row["attempt_id"]),
                        "restart_unresolved",
                        "previous backend process could not be confirmed stopped",
                    )
            self._owners_closing.update(unresolved_owners)
        return len(rows)


_RUNTIME_LOCK = threading.Lock()
_RUNTIMES: dict[str, PluginWorkerRuntime] = {}
# Backend shutdown is terminal for this process. Do not create a replacement
# runtime while the current backend is tearing down its existing children.
_RUNTIME_SHUTTING_DOWN = False


def get_plugin_worker_runtime() -> PluginWorkerRuntime:
    """Return the profile-scoped worker runtime service."""
    home = str(get_hermes_home().resolve())
    with _RUNTIME_LOCK:
        if _RUNTIME_SHUTTING_DOWN:
            raise RuntimeError("plugin worker runtime backend is shutting down")
        runtime = _RUNTIMES.get(home)
        if runtime is None:
            runtime = PluginWorkerRuntime()
            # A fresh backend never resumes a prior process. Reconciliation is
            # performed before a plugin can register a definition or launch an
            # attempt, so stale durable work is terminalized fail-closed.
            runtime.reconcile_startup()
            _RUNTIMES[home] = runtime
        return runtime


def shutdown_plugin_worker_runtimes(*, reason: str) -> ShutdownResult:
    """Stop admission and cancel every runtime owned by this backend process.

    The process-local registry deliberately has no cross-backend discovery.
    A different backend cannot inherit this authority after a restart; its
    startup reconciliation must inspect durable records and fail closed.
    """
    global _RUNTIME_SHUTTING_DOWN
    with _RUNTIME_LOCK:
        _RUNTIME_SHUTTING_DOWN = True
        runtimes = tuple(_RUNTIMES.values())
        for runtime in runtimes:
            runtime.close_admission()
        _RUNTIMES.clear()
    cancelled = 0
    indeterminate = 0
    for runtime in runtimes:
        result = runtime.shutdown(reason=reason)
        cancelled += result.cancelled
        indeterminate += result.indeterminate
    return ShutdownResult(cancelled=cancelled, indeterminate=indeterminate)


def _startup_gate_main(arguments: list[str]) -> int:
    """Wait for durable parent release before starting one fixed worker command."""

    if len(arguments) < 2 or arguments[0] != "--":
        return 2
    try:
        released = sys.stdin.buffer.read(1) == _STARTUP_GATE_RELEASE
    except OSError:
        return 125
    if not released:
        return 125
    command = arguments[1:]
    try:
        if IS_WINDOWS:
            return subprocess.run(command, stdin=subprocess.DEVNULL, check=False).returncode
        os.execvpe(command[0], command, os.environ)
    except OSError:
        return 127
    return 127


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--startup-gate":
        raise SystemExit(_startup_gate_main(sys.argv[2:]))
    raise SystemExit(2)
