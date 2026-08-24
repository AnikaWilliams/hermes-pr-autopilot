"""Deterministic control plane for the local Codex PR autopilot.

The timed watcher deliberately performs no model inference.  It classifies
GitHub data, creates Kanban cards only for fresh Codex findings, requests a
re-review after a new head is pushed, and merges only after an exact-head clean
verdict.  The Kanban worker is the only MoA-powered component.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Iterator
from uuid import uuid4


BOT_LOGINS = frozenset(
    {
        "chatgpt-codex-connector",
        "chatgpt-codex-connector[bot]",
    }
)

_REVIEWED_SHA = re.compile(
    r"Reviewed commit:\*{0,2}\s*`([0-9a-fA-F]{7,64})`", re.IGNORECASE
)
_QUOTA_MARKERS = (
    "reached your codex usage limits",
    "codex usage limits for code reviews",
)
_ENVIRONMENT_MARKERS = (
    "create an environment for this repo",
    "set up an environment for this repo",
)
_CLEAN_MARKERS = (
    "didn't find any major issues",
    "did not find any major issues",
)
_FINDING_BADGE = re.compile(r"!\[P[0-3] Badge\]", re.IGNORECASE)
CLOSED_UNMERGED_MERGE_INTENT_REASON = (
    "GitHub closed the pull request without merging before confirmation"
)


class Classification(str, Enum):
    """Mutually-exclusive deterministic PR states."""

    CLEAN = "clean"
    FINDINGS = "findings"
    REVIEWING = "reviewing"
    NEEDS_REVIEW = "needs_review"
    QUOTA_EXHAUSTED = "quota_exhausted"
    ENVIRONMENT_BLOCKED = "environment_blocked"


class Action(str, Enum):
    """Safe controller actions after a deterministic classification."""

    WAIT = "wait"
    CREATE_TASK = "create_task"
    REQUEST_REVIEW = "request_review"
    MERGE = "merge"
    BLOCK = "block"


class ClosedUnmergedMergeIntentError(ValueError):
    """A closed PR invalidated the only tracked merge authorization."""


@dataclass(frozen=True)
class ClassificationResult:
    """A Codex classification, tied to a concrete PR head."""

    kind: Classification
    reviewed_sha: str | None = None
    findings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    detail: str = ""


@dataclass(frozen=True)
class WatchState:
    """Minimal durable state used to make polling idempotent."""

    requested_head: str | None = None
    requested_comment_id: str | None = None
    review_rounds: int = 0
    active_task_id: str | None = None
    active_task_status: str | None = None
    last_finding_fingerprint: str | None = None


@dataclass(frozen=True)
class PRState:
    """All durable controller state for one authored pull request."""

    repository: str
    number: int
    updated_at: str
    head_sha: str
    policy_revision: int = 0
    requested_head: str | None = None
    requested_comment_id: str | None = None
    requested_at: str | None = None
    review_rounds: int = 0
    fresh_review_required: bool = False
    active_task_id: str | None = None
    active_task_status: str | None = None
    last_finding_fingerprint: str | None = None
    pending_finding_fingerprint: str | None = None
    pending_findings_json: str | None = None
    pipeline_json: str | None = None
    review_request_token: str | None = None

    def watch_state(self) -> WatchState:
        return WatchState(
            requested_head=self.requested_head,
            requested_comment_id=self.requested_comment_id,
            review_rounds=self.review_rounds,
            active_task_id=self.active_task_id,
            active_task_status=self.active_task_status,
            last_finding_fingerprint=self.last_finding_fingerprint,
        )

    def pipeline_tasks(self) -> dict[str, str]:
        """Return a safe role-to-task map from the persisted pipeline payload."""

        if not self.pipeline_json:
            return {}
        try:
            raw = json.loads(self.pipeline_json)
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(role): str(task_id)
            for role, task_id in raw.items()
            if isinstance(role, str) and isinstance(task_id, str) and task_id
        }


@dataclass(frozen=True)
class MergeHistoryRecord:
    """Durable publication state for one controller-performed merge."""

    repository: str
    number: int
    title: str
    url: str
    head_sha: str
    merged_at: str
    authorization_id: str | None = None
    confirmed_at: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: str | None = None
    task_id: str | None = None
    recorded_at: str | None = None


@dataclass(frozen=True)
class RuntimeLease:
    """Fenced ownership of the local standalone controller database."""

    name: str
    owner_id: str
    generation: int
    expires_at: int


@dataclass(frozen=True)
class StandalonePipeline:
    """A persisted standalone pipeline, including quarantined migration audit state."""

    identifier: str
    repository: str
    number: int
    expected_head: str
    status: str
    block_reason: str | None
    legacy_task_id: str | None


class StateStore:
    """Small SQLite store that makes a cron tick restart-safe and idempotent."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(
                    f"state database does not exist for dry-run: {self.path}"
                )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                timeout=10,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=10)
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
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pr_state (
                    repository TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    policy_revision INTEGER NOT NULL DEFAULT 0,
                    requested_head TEXT,
                    requested_comment_id TEXT,
                    review_request_token TEXT,
                    requested_at TEXT,
                    review_rounds INTEGER NOT NULL DEFAULT 0,
                    fresh_review_required INTEGER NOT NULL DEFAULT 0,
                    active_task_id TEXT,
                    active_task_status TEXT,
                    last_finding_fingerprint TEXT,
                    pending_finding_fingerprint TEXT,
                    pending_findings_json TEXT,
                    pipeline_json TEXT,
                    PRIMARY KEY (repository, number)
                )
                """
            )
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(pr_state)")}
            if "pipeline_json" not in columns:
                connection.execute("ALTER TABLE pr_state ADD COLUMN pipeline_json TEXT")
            if "policy_revision" not in columns:
                connection.execute(
                    "ALTER TABLE pr_state ADD COLUMN policy_revision INTEGER NOT NULL DEFAULT 0"
                )
            if "pending_finding_fingerprint" not in columns:
                connection.execute(
                    "ALTER TABLE pr_state ADD COLUMN pending_finding_fingerprint TEXT"
                )
            if "pending_findings_json" not in columns:
                connection.execute(
                    "ALTER TABLE pr_state ADD COLUMN pending_findings_json TEXT"
                )
            if "fresh_review_required" not in columns:
                connection.execute(
                    "ALTER TABLE pr_state "
                    "ADD COLUMN fresh_review_required INTEGER NOT NULL DEFAULT 0"
                )
            if "review_request_token" not in columns:
                connection.execute(
                    "ALTER TABLE pr_state ADD COLUMN review_request_token TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS merge_history (
                    repository TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    merged_at TEXT NOT NULL,
                    authorization_id TEXT,
                    confirmed_at TEXT,
                    invalidated_at TEXT,
                    invalidation_reason TEXT,
                    task_id TEXT,
                    recorded_at TEXT,
                    PRIMARY KEY (repository, number, head_sha)
                )
                """
            )
            merge_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(merge_history)")
            }
            if "confirmed_at" not in merge_columns:
                connection.execute("ALTER TABLE merge_history ADD COLUMN confirmed_at TEXT")
                connection.execute(
                    "UPDATE merge_history SET confirmed_at = merged_at "
                    "WHERE confirmed_at IS NULL"
                )
            if "authorization_id" not in merge_columns:
                connection.execute("ALTER TABLE merge_history ADD COLUMN authorization_id TEXT")
            if "invalidated_at" not in merge_columns:
                connection.execute("ALTER TABLE merge_history ADD COLUMN invalidated_at TEXT")
            if "invalidation_reason" not in merge_columns:
                connection.execute(
                    "ALTER TABLE merge_history ADD COLUMN invalidation_reason TEXT"
                )
            merge_primary_key = [
                str(row["name"])
                for row in sorted(
                    connection.execute("PRAGMA table_info(merge_history)"),
                    key=lambda row: int(row["pk"]),
                )
                if int(row["pk"])
            ]
            legacy_table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'merge_history_legacy'"
            ).fetchone()
            if legacy_table_exists is not None:
                if merge_primary_key != ["repository", "number", "head_sha"]:
                    raise RuntimeError(
                        "cannot resume merge-history migration: target schema is invalid"
                    )
                legacy_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(merge_history_legacy)")
                }
                authorization_expression = (
                    "authorization_id" if "authorization_id" in legacy_columns else "NULL"
                )
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO merge_history (
                        repository, number, title, url, head_sha, merged_at,
                        authorization_id, confirmed_at, invalidated_at,
                        invalidation_reason, task_id, recorded_at
                    )
                    SELECT repository, number, title, url, head_sha, merged_at,
                           {authorization_expression}, confirmed_at, invalidated_at,
                           invalidation_reason, task_id, recorded_at
                    FROM merge_history_legacy
                    """
                )
                connection.execute("DROP TABLE merge_history_legacy")
            if merge_primary_key != ["repository", "number", "head_sha"]:
                connection.execute("ALTER TABLE merge_history RENAME TO merge_history_legacy")
                connection.execute(
                    """
                    CREATE TABLE merge_history (
                        repository TEXT NOT NULL,
                        number INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        url TEXT NOT NULL,
                        head_sha TEXT NOT NULL,
                        merged_at TEXT NOT NULL,
                        authorization_id TEXT,
                        confirmed_at TEXT,
                        invalidated_at TEXT,
                        invalidation_reason TEXT,
                        task_id TEXT,
                        recorded_at TEXT,
                        PRIMARY KEY (repository, number, head_sha)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO merge_history (
                        repository, number, title, url, head_sha, merged_at,
                        authorization_id, confirmed_at, invalidated_at,
                        invalidation_reason, task_id, recorded_at
                    )
                    SELECT repository, number, title, url, head_sha, merged_at,
                           authorization_id, confirmed_at, invalidated_at,
                           invalidation_reason, task_id, recorded_at
                    FROM merge_history_legacy
                    """
                )
                connection.execute("DROP TABLE merge_history_legacy")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repository_settings (
                    repository TEXT PRIMARY KEY,
                    disabled INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline (
                    identifier TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    number INTEGER NOT NULL CHECK (number > 0),
                    expected_head TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ready', 'blocked_migration')),
                    block_reason TEXT,
                    legacy_task_id TEXT,
                    legacy_pipeline_json TEXT,
                    created_at INTEGER NOT NULL,
                    UNIQUE (repository, number, expected_head)
                )
                """
            )
            pipeline_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(pipeline)")
            }
            if "legacy_task_id" not in pipeline_columns:
                connection.execute("ALTER TABLE pipeline ADD COLUMN legacy_task_id TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_stage (
                    identifier TEXT PRIMARY KEY,
                    pipeline_id TEXT NOT NULL REFERENCES pipeline(identifier) ON DELETE RESTRICT,
                    role TEXT NOT NULL CHECK (role = 'analyze'),
                    status TEXT NOT NULL CHECK (status IN ('ready', 'blocked_migration')),
                    position INTEGER NOT NULL CHECK (position = 1),
                    UNIQUE (pipeline_id, role),
                    UNIQUE (pipeline_id, position)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_attempt (
                    identifier TEXT PRIMARY KEY,
                    stage_id TEXT NOT NULL REFERENCES pipeline_stage(identifier) ON DELETE RESTRICT,
                    expected_head TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role = 'analyze'),
                    status TEXT NOT NULL CHECK (status IN ('ready', 'blocked')),
                    terminal_reason TEXT,
                    lease_generation INTEGER NOT NULL CHECK (lease_generation > 0),
                    created_at INTEGER NOT NULL,
                    UNIQUE (stage_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_lease (
                    name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    expires_at INTEGER NOT NULL
                )
                """
            )
            self._quarantine_legacy_nonterminal_pipelines(connection)

    @staticmethod
    def _quarantine_legacy_nonterminal_pipelines(connection: sqlite3.Connection) -> None:
        """Copy legacy active card references as permanently blocked audit records."""

        rows = connection.execute(
            """
            SELECT repository, number, head_sha, active_task_id, active_task_status, pipeline_json
            FROM pr_state
            WHERE (active_task_id IS NOT NULL OR pipeline_json IS NOT NULL)
              AND coalesce(active_task_status, 'unknown') NOT IN ('done', 'archived')
            """
        ).fetchall()
        for row in rows:
            repository = str(row["repository"])
            number = int(row["number"])
            # Legacy controller state predates the standalone exact-head
            # contract. Preserve it as audit data, but canonicalize its SHA
            # spelling so a 40-character GitHub head cannot evade quarantine
            # merely through case variation.
            expected_head = str(row["head_sha"]).lower()
            existing = connection.execute(
                """
                SELECT identifier FROM pipeline
                WHERE repository = ? AND number = ? AND expected_head = ?
                """,
                (repository, number, expected_head),
            ).fetchone()
            if existing is not None:
                continue
            pipeline_id = f"legacy-{uuid4().hex}"
            stage_id = f"legacy-stage-{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO pipeline (
                    identifier, repository, number, expected_head, status, block_reason,
                    legacy_task_id, legacy_pipeline_json, created_at
                ) VALUES (?, ?, ?, ?, 'blocked_migration', 'legacy_nonterminal_quarantined', ?, ?, 0)
                """,
                (
                    pipeline_id,
                    repository,
                    number,
                    expected_head,
                    row["active_task_id"],
                    row["pipeline_json"],
                ),
            )
            connection.execute(
                """
                INSERT INTO pipeline_stage (identifier, pipeline_id, role, status, position)
                VALUES (?, ?, 'analyze', 'blocked_migration', 1)
                """,
                (stage_id, pipeline_id),
            )

    def standalone_schema_tables(self) -> set[str]:
        """Return the normalized tables owned by the non-live standalone slice."""

        wanted = {"pipeline", "pipeline_stage", "worker_attempt", "runtime_lease"}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {str(row["name"]) for row in rows}.intersection(wanted)

    @staticmethod
    def _canonical_head_sha(expected_head: str) -> str:
        """Validate a full Git commit SHA and return its canonical spelling."""

        if not isinstance(expected_head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", expected_head):
            raise ValueError("a full 40-character commit SHA is required")
        return expected_head.lower()

    def pipeline_for(
        self, *, repository: str, number: int, expected_head: str
    ) -> StandalonePipeline | None:
        """Load one standalone exact-head pipeline without interpreting legacy cards."""

        expected_head = self._canonical_head_sha(expected_head)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT identifier, repository, number, expected_head, status, block_reason,
                       legacy_task_id
                FROM pipeline
                WHERE repository = ? AND number = ? AND expected_head = ?
                """,
                (repository, number, expected_head),
            ).fetchone()
        if row is None:
            return None
        return StandalonePipeline(
            identifier=str(row["identifier"]),
            repository=str(row["repository"]),
            number=int(row["number"]),
            expected_head=str(row["expected_head"]),
            status=str(row["status"]),
            block_reason=(str(row["block_reason"]) if row["block_reason"] is not None else None),
            legacy_task_id=(
                str(row["legacy_task_id"]) if row["legacy_task_id"] is not None else None
            ),
        )

    def acquire_runtime_lease(
        self,
        *,
        name: str,
        owner_id: str,
        now: int,
        ttl_seconds: int,
        renew_generation: int | None = None,
    ) -> RuntimeLease | None:
        """Atomically acquire or renew a fenced lease; conflicts fail closed."""

        if not name or not owner_id or ttl_seconds <= 0:
            raise ValueError("runtime lease name, owner, and positive TTL are required")
        expires_at = now + ttl_seconds
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id, generation, expires_at FROM runtime_lease WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                generation = 1
                connection.execute(
                    """
                    INSERT INTO runtime_lease (name, owner_id, generation, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, owner_id, generation, expires_at),
                )
            elif str(row["owner_id"]) == owner_id and int(row["expires_at"]) > now:
                # A fresh runtime with the same human-readable owner label must
                # not inherit a still-valid fence. Only the current holder can
                # renew, and it proves that by presenting its generation.
                if renew_generation is None or int(row["generation"]) != renew_generation:
                    return None
                generation = int(row["generation"])
                connection.execute(
                    "UPDATE runtime_lease SET expires_at = ? WHERE name = ? AND generation = ?",
                    (expires_at, name, generation),
                )
            elif int(row["expires_at"]) <= now:
                generation = int(row["generation"]) + 1
                updated = connection.execute(
                    """
                    UPDATE runtime_lease
                    SET owner_id = ?, generation = ?, expires_at = ?
                    WHERE name = ? AND generation = ? AND expires_at <= ?
                    """,
                    (owner_id, generation, expires_at, name, int(row["generation"]), now),
                )
                if updated.rowcount != 1:
                    return None
            else:
                return None
        return RuntimeLease(name, owner_id, generation, expires_at)

    def release_runtime_lease(
        self, *, name: str, owner_id: str, generation: int
    ) -> bool:
        """Expire only the exact current fenced lease; stale owners cannot release it."""

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE runtime_lease SET expires_at = 0
                WHERE name = ? AND owner_id = ? AND generation = ?
                """,
                (name, owner_id, generation),
            )
        return updated.rowcount == 1

    def create_or_load_analyze_attempt(
        self,
        *,
        repository: str,
        number: int,
        expected_head: str,
        owner_id: str,
        lease_generation: int,
        now: int,
    ) -> dict[str, str | int]:
        """Idempotently admit exactly one local Analyze attempt for one exact head."""

        if not repository or number <= 0:
            raise ValueError("repository and positive PR number are required")
        expected_head = self._canonical_head_sha(expected_head)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT owner_id, generation, expires_at FROM runtime_lease WHERE name = ?",
                ("standalone-controller",),
            ).fetchone()
            if (
                lease is None
                or str(lease["owner_id"]) != owner_id
                or int(lease["generation"]) != lease_generation
                or int(lease["expires_at"]) <= now
            ):
                raise ValueError("controller lease is missing or fenced by another runtime")
            pipeline = connection.execute(
                """
                SELECT identifier, status FROM pipeline
                WHERE repository = ? AND number = ? AND expected_head = ?
                """,
                (repository, number, expected_head),
            ).fetchone()
            if pipeline is None:
                pipeline_id = f"pipeline-{uuid4().hex}"
                stage_id = f"stage-{uuid4().hex}"
                attempt_id = f"attempt-{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO pipeline (
                        identifier, repository, number, expected_head, status, created_at
                    ) VALUES (?, ?, ?, ?, 'ready', 0)
                    """,
                    (pipeline_id, repository, number, expected_head),
                )
                connection.execute(
                    """
                    INSERT INTO pipeline_stage (identifier, pipeline_id, role, status, position)
                    VALUES (?, ?, 'analyze', 'ready', 1)
                    """,
                    (stage_id, pipeline_id),
                )
                connection.execute(
                    """
                    INSERT INTO worker_attempt (
                        identifier, stage_id, expected_head, role, status, lease_generation, created_at
                    ) VALUES (?, ?, ?, 'analyze', 'ready', ?, 0)
                    """,
                    (attempt_id, stage_id, expected_head, lease_generation),
                )
            else:
                if str(pipeline["status"]) != "ready":
                    raise ValueError("pipeline is blocked and cannot be resumed")
                row = connection.execute(
                    """
                    SELECT worker_attempt.identifier
                    FROM worker_attempt
                    JOIN pipeline_stage ON pipeline_stage.identifier = worker_attempt.stage_id
                    WHERE pipeline_stage.pipeline_id = ? AND pipeline_stage.role = 'analyze'
                    """,
                    (str(pipeline["identifier"]),),
                ).fetchone()
                if row is None:
                    raise ValueError("ready pipeline is missing its Analyze attempt")
                attempt_id = str(row["identifier"])
        record = self.load_analyze_attempt(attempt_id)
        if record is None:
            raise RuntimeError("failed to persist Analyze attempt")
        return record

    def block_analyze_attempt(
        self,
        attempt_id: str,
        *,
        reason: str,
        owner_id: str,
        lease_generation: int,
        now: int,
    ) -> dict[str, str | int]:
        """Terminalize a ready attempt only while its exact lease is current."""

        if not reason:
            raise ValueError("blocked attempt requires a reason")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT owner_id, generation, expires_at FROM runtime_lease WHERE name = ?",
                ("standalone-controller",),
            ).fetchone()
            if (
                lease is None
                or str(lease["owner_id"]) != owner_id
                or int(lease["generation"]) != lease_generation
                or int(lease["expires_at"]) <= now
            ):
                raise ValueError("controller lease is missing or fenced by another runtime")
            updated = connection.execute(
                """
                UPDATE worker_attempt
                SET status = 'blocked', terminal_reason = ?
                WHERE identifier = ? AND status = 'ready'
                """,
                (reason, attempt_id),
            )
            if updated.rowcount != 1:
                raise ValueError("only a ready Analyze attempt can be interrupted")
        record = self.load_analyze_attempt(attempt_id)
        if record is None:
            raise RuntimeError("interrupted Analyze attempt disappeared")
        return record

    def load_analyze_attempt(self, attempt_id: str) -> dict[str, str | int] | None:
        """Read the safe, persisted identity and status for one Analyze attempt."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT worker_attempt.identifier, pipeline.repository, pipeline.number,
                       worker_attempt.expected_head, worker_attempt.role, worker_attempt.status,
                       worker_attempt.terminal_reason
                FROM worker_attempt
                JOIN pipeline_stage ON pipeline_stage.identifier = worker_attempt.stage_id
                JOIN pipeline ON pipeline.identifier = pipeline_stage.pipeline_id
                WHERE worker_attempt.identifier = ?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "identifier": str(row["identifier"]),
            "repository": str(row["repository"]),
            "number": int(row["number"]),
            "expected_head": str(row["expected_head"]),
            "role": str(row["role"]),
            "status": str(row["status"]),
            "terminal_reason": (
                str(row["terminal_reason"]) if row["terminal_reason"] is not None else None
            ),
        }

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> PRState:
        return PRState(
            repository=row["repository"],
            number=row["number"],
            updated_at=row["updated_at"],
            head_sha=row["head_sha"],
            policy_revision=row["policy_revision"],
            requested_head=row["requested_head"],
            requested_comment_id=row["requested_comment_id"],
            review_request_token=row["review_request_token"],
            requested_at=row["requested_at"],
            review_rounds=row["review_rounds"],
            fresh_review_required=bool(row["fresh_review_required"]),
            active_task_id=row["active_task_id"],
            active_task_status=row["active_task_status"],
            last_finding_fingerprint=row["last_finding_fingerprint"],
            pending_finding_fingerprint=row["pending_finding_fingerprint"],
            pending_findings_json=row["pending_findings_json"],
            pipeline_json=row["pipeline_json"],
        )

    def load(self, repository: str, number: int) -> PRState | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pr_state WHERE repository = ? AND number = ?",
                (repository, number),
            ).fetchone()
        return self._state_from_row(row) if row is not None else None

    def nonterminal_pipelines(self) -> tuple[PRState, ...]:
        """Return durable pipelines that must be reconciled on every cycle."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pr_state
                WHERE active_task_id IS NOT NULL
                  AND coalesce(active_task_status, 'unknown') NOT IN ('done', 'archived')
                ORDER BY repository COLLATE NOCASE, number
                """
            ).fetchall()
        return tuple(self._state_from_row(row) for row in rows)

    def pending_review_requests(self) -> tuple[PRState, ...]:
        """Return live controller-owned review requests that still need polling.

        A Verify completion can clear ``active_task_id`` before the controller
        has observed the replacement Codex request.  That request remains
        controller-owned work, not a new pull request.  Archived rows,
        explicitly invalidated requests, and heads with a durable merge intent
        are terminal for this polling path.
        """

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT state.* FROM pr_state AS state
                WHERE trim(coalesce(state.requested_head, '')) != ''
                  AND (
                      trim(coalesce(state.requested_comment_id, '')) != ''
                      OR trim(coalesce(state.review_request_token, '')) != ''
                  )
                  AND coalesce(state.active_task_status, '') != 'archived'
                  AND state.fresh_review_required = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM merge_history AS history
                      WHERE history.repository = state.repository
                        AND history.number = state.number
                        AND history.head_sha = state.requested_head
                        AND history.invalidated_at IS NULL
                  )
                ORDER BY state.repository COLLATE NOCASE, state.number
                """
            ).fetchall()
        return tuple(self._state_from_row(row) for row in rows)

    def save(self, state: PRState) -> None:
        values = (
            state.repository,
            state.number,
            state.updated_at,
            state.head_sha,
            state.policy_revision,
            state.requested_head,
            state.requested_comment_id,
            state.review_request_token,
            state.requested_at,
            state.review_rounds,
            int(state.fresh_review_required),
            state.active_task_id,
            state.active_task_status,
            state.last_finding_fingerprint,
            state.pending_finding_fingerprint,
            state.pending_findings_json,
            state.pipeline_json,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO pr_state (
                    repository, number, updated_at, head_sha, policy_revision, requested_head,
                    requested_comment_id, review_request_token, requested_at, review_rounds, fresh_review_required,
                    active_task_id, active_task_status, last_finding_fingerprint,
                    pending_finding_fingerprint, pending_findings_json, pipeline_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository, number) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    head_sha = excluded.head_sha,
                    policy_revision = excluded.policy_revision,
                    requested_head = excluded.requested_head,
                    requested_comment_id = excluded.requested_comment_id,
                    review_request_token = excluded.review_request_token,
                    requested_at = excluded.requested_at,
                    review_rounds = excluded.review_rounds,
                    fresh_review_required = excluded.fresh_review_required,
                    active_task_id = excluded.active_task_id,
                    active_task_status = excluded.active_task_status,
                    last_finding_fingerprint = excluded.last_finding_fingerprint,
                    pending_finding_fingerprint = excluded.pending_finding_fingerprint,
                    pending_findings_json = excluded.pending_findings_json,
                    pipeline_json = excluded.pipeline_json
                """,
                values,
            )

    def disabled_repositories(self) -> frozenset[str]:
        """Return the set of repositories the operator has switched off in the UI."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT repository FROM repository_settings WHERE disabled = 1"
            ).fetchall()
        return frozenset(str(row["repository"]).lower() for row in rows)

    def set_disabled_repositories(self, repositories: Iterable[str]) -> None:
        """Persist the operator-selected disabled-repository set (case-insensitive)."""

        desired = frozenset(repository.strip().lower() for repository in repositories if repository.strip())
        with self._connection() as connection:
            connection.execute("DELETE FROM repository_settings")
            for repository in sorted(desired):
                connection.execute(
                    "INSERT INTO repository_settings (repository, disabled) VALUES (?, 1)",
                    (repository,),
                )

    def all_repository_settings(self) -> dict[str, bool]:
        """Return repository -> disabled flag for every stored repository."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT repository, disabled FROM repository_settings"
            ).fetchall()
        return {
            str(row["repository"]).lower(): bool(row["disabled"])
            for row in rows
        }

    @staticmethod
    def _merge_history_record(row: sqlite3.Row) -> MergeHistoryRecord:
        return MergeHistoryRecord(
            repository=row["repository"],
            number=row["number"],
            title=row["title"],
            url=row["url"],
            head_sha=row["head_sha"],
            merged_at=row["merged_at"],
            authorization_id=row["authorization_id"],
            confirmed_at=row["confirmed_at"],
            invalidated_at=row["invalidated_at"],
            invalidation_reason=row["invalidation_reason"],
            task_id=row["task_id"],
            recorded_at=row["recorded_at"],
        )

    def queue_merge_history(
        self,
        *,
        repository: str,
        number: int,
        title: str,
        url: str,
        head_sha: str,
        merged_at: str,
    ) -> MergeHistoryRecord:
        """Persist a future merge once without replacing publication state."""

        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO merge_history (
                    repository, number, title, url, head_sha, merged_at, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (repository, number, title, url, head_sha, merged_at, merged_at),
            )
            row = connection.execute(
                "SELECT * FROM merge_history "
                "WHERE repository = ? AND number = ? AND head_sha = ?",
                (repository, number, head_sha),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"failed to queue merge history for {repository}#{number}")
        return self._merge_history_record(row)

    def queue_merge_history_intent(
        self,
        *,
        repository: str,
        number: int,
        title: str,
        url: str,
        head_sha: str,
        intended_at: str,
        authorization_id: str | None = None,
    ) -> MergeHistoryRecord:
        """Persist an exact-head merge intent without making it publishable.

        An invalidated intent can reopen only after a different tracked review
        request authorizes the same head again. This prevents a historical
        clean verdict from reviving a failed merge attempt.
        """

        normalized_authorization_id = (
            authorization_id.strip()
            if isinstance(authorization_id, str) and authorization_id.strip()
            else None
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO merge_history (
                    repository, number, title, url, head_sha, merged_at,
                    authorization_id, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(repository, number, head_sha) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    merged_at = excluded.merged_at,
                    authorization_id = COALESCE(
                        excluded.authorization_id, merge_history.authorization_id
                    ),
                    confirmed_at = NULL,
                    invalidated_at = NULL,
                    invalidation_reason = NULL,
                    task_id = NULL
                WHERE merge_history.confirmed_at IS NULL
                  AND merge_history.recorded_at IS NULL
                  AND (
                      merge_history.invalidated_at IS NULL
                      OR (
                          excluded.authorization_id IS NOT NULL
                          AND excluded.authorization_id <> ''
                          AND merge_history.authorization_id IS NOT excluded.authorization_id
                      )
                  )
                """,
                (
                    repository,
                    number,
                    title,
                    url,
                    head_sha,
                    intended_at,
                    normalized_authorization_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM merge_history "
                "WHERE repository = ? AND number = ? AND head_sha = ?",
                (repository, number, head_sha),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"failed to queue merge intent for {repository}#{number}")
        record = self._merge_history_record(row)
        if record.invalidated_at is not None:
            if record.invalidation_reason == CLOSED_UNMERGED_MERGE_INTENT_REASON:
                raise ClosedUnmergedMergeIntentError(
                    f"merge history intent was invalidated by an unmerged close for "
                    f"{repository}#{number}@{head_sha}"
                )
            raise ValueError(
                f"merge history intent remains invalidated for {repository}#{number}@{head_sha}"
            )
        return record

    def unconfirmed_merge_history(self) -> list[MergeHistoryRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM merge_history
                WHERE confirmed_at IS NULL
                  AND invalidated_at IS NULL
                  AND recorded_at IS NULL
                ORDER BY merged_at, repository, number
                """
            ).fetchall()
        return [self._merge_history_record(row) for row in rows]

    def confirm_merge_history(
        self,
        repository: str,
        number: int,
        *,
        head_sha: str,
        merged_at: str,
    ) -> MergeHistoryRecord:
        """Confirm a durable intent only for its exact expected head."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM merge_history "
                "WHERE repository = ? AND number = ? AND head_sha = ?",
                (repository, number, head_sha),
            ).fetchone()
            if row is None:
                existing_head = connection.execute(
                    "SELECT head_sha FROM merge_history "
                    "WHERE repository = ? AND number = ? ORDER BY merged_at DESC LIMIT 1",
                    (repository, number),
                ).fetchone()
                if existing_head is not None:
                    raise ValueError(
                        f"merge history head mismatch for {repository}#{number}: "
                        f"expected {existing_head['head_sha']}, got {head_sha}"
                    )
                raise KeyError(f"merge history not found for {repository}#{number}@{head_sha}")
            if row["invalidated_at"] is not None:
                raise ValueError(
                    f"merge history intent is invalidated for {repository}#{number}"
                )
            connection.execute(
                """
                UPDATE merge_history
                SET merged_at = ?, confirmed_at = ?
                WHERE repository = ? AND number = ? AND head_sha = ?
                  AND recorded_at IS NULL
                """,
                (merged_at, merged_at, repository, number, head_sha),
            )
            row = connection.execute(
                "SELECT * FROM merge_history "
                "WHERE repository = ? AND number = ? AND head_sha = ?",
                (repository, number, head_sha),
            ).fetchone()
        return self._merge_history_record(row)

    def invalidate_merge_history(
        self,
        repository: str,
        number: int,
        *,
        head_sha: str,
        invalidated_at: str,
        reason: str,
    ) -> MergeHistoryRecord:
        """Terminalize an unconfirmed exact-head intent without publishing it."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM merge_history "
                "WHERE repository = ? AND number = ? AND head_sha = ?",
                (repository, number, head_sha),
            ).fetchone()
            if row is None:
                raise KeyError(f"merge history not found for {repository}#{number}@{head_sha}")
            connection.execute(
                """
                UPDATE merge_history
                SET invalidated_at = ?, invalidation_reason = ?
                WHERE repository = ? AND number = ? AND head_sha = ?
                  AND confirmed_at IS NULL
                  AND invalidated_at IS NULL
                  AND recorded_at IS NULL
                """,
                (invalidated_at, reason, repository, number, head_sha),
            )
            row = connection.execute(
                "SELECT * FROM merge_history "
                "WHERE repository = ? AND number = ? AND head_sha = ?",
                (repository, number, head_sha),
            ).fetchone()
        return self._merge_history_record(row)

    def load_merge_history(
        self, repository: str, number: int, *, head_sha: str | None = None
    ) -> MergeHistoryRecord | None:
        with self._connection() as connection:
            if head_sha is None:
                row = connection.execute(
                    "SELECT * FROM merge_history WHERE repository = ? AND number = ? "
                    "ORDER BY merged_at DESC LIMIT 1",
                    (repository, number),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM merge_history "
                    "WHERE repository = ? AND number = ? AND head_sha = ?",
                    (repository, number, head_sha),
                ).fetchone()
        return self._merge_history_record(row) if row is not None else None

    def pending_merge_history(self) -> list[MergeHistoryRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM merge_history
                WHERE confirmed_at IS NOT NULL
                  AND invalidated_at IS NULL
                  AND recorded_at IS NULL
                ORDER BY merged_at, repository, number
                """
            ).fetchall()
        return [self._merge_history_record(row) for row in rows]

    def set_merge_history_task(
        self, repository: str, number: int, task_id: str, *, head_sha: str | None = None
    ) -> MergeHistoryRecord:
        with self._connection() as connection:
            head_sha = head_sha or self._latest_merge_head(connection, repository, number)
            connection.execute(
                """
                UPDATE merge_history SET task_id = ?
                WHERE repository = ? AND number = ? AND head_sha = ?
                  AND confirmed_at IS NOT NULL
                  AND invalidated_at IS NULL
                  AND recorded_at IS NULL
                """,
                (task_id, repository, number, head_sha),
            )
            row = connection.execute(
                "SELECT * FROM merge_history "
                "WHERE repository = ? AND number = ? AND head_sha = ?",
                (repository, number, head_sha),
            ).fetchone()
        if row is None:
            raise KeyError(f"merge history not found for {repository}#{number}@{head_sha}")
        return self._merge_history_record(row)

    def mark_merge_history_recorded(
        self,
        repository: str,
        number: int,
        recorded_at: str,
        *,
        head_sha: str | None = None,
    ) -> MergeHistoryRecord:
        with self._connection() as connection:
            head_sha = head_sha or self._latest_merge_head(connection, repository, number)
            connection.execute(
                """
                UPDATE merge_history SET recorded_at = ?
                WHERE repository = ? AND number = ? AND head_sha = ?
                """,
                (recorded_at, repository, number, head_sha),
            )
            row = connection.execute(
                "SELECT * FROM merge_history "
                "WHERE repository = ? AND number = ? AND head_sha = ?",
                (repository, number, head_sha),
            ).fetchone()
        if row is None:
            raise KeyError(f"merge history not found for {repository}#{number}@{head_sha}")
        return self._merge_history_record(row)

    @staticmethod
    def _latest_merge_head(
        connection: sqlite3.Connection, repository: str, number: int
    ) -> str:
        row = connection.execute(
            "SELECT head_sha FROM merge_history WHERE repository = ? AND number = ? "
            "ORDER BY merged_at DESC LIMIT 1",
            (repository, number),
        ).fetchone()
        if row is None:
            raise KeyError(f"merge history not found for {repository}#{number}")
        return str(row["head_sha"])


def findings_fingerprint(head_sha: str, findings: Iterable[dict[str, Any]]) -> str:
    """Stable ID for the exact Codex finding set on one PR head."""

    identifiers = sorted(
        str(finding.get("databaseId") or finding.get("id") or finding.get("body") or "")
        for finding in findings
    )
    payload = json.dumps({"head": head_sha, "findings": identifiers}, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_action(
    result: ClassificationResult,
    state: WatchState,
    *,
    head_sha: str,
    current_fingerprint: str | None,
    max_review_rounds: int,
) -> Action:
    """Choose a side effect without invoking a model or GitHub yet."""

    if state.active_task_id and state.active_task_status not in {"done", "archived"}:
        return Action.WAIT
    if result.kind in {Classification.QUOTA_EXHAUSTED, Classification.ENVIRONMENT_BLOCKED}:
        return Action.BLOCK
    if result.kind == Classification.FINDINGS:
        if current_fingerprint and current_fingerprint == state.last_finding_fingerprint:
            return Action.BLOCK
        return Action.CREATE_TASK
    if result.kind == Classification.CLEAN:
        return Action.MERGE
    if result.kind == Classification.NEEDS_REVIEW:
        return (
            Action.REQUEST_REVIEW
            if state.review_rounds < max_review_rounds
            else Action.BLOCK
        )
    return Action.WAIT


def reviewed_sha_matches_head(reviewed_sha: str | None, head_sha: str | None) -> bool:
    """Return whether a full or abbreviated reviewed SHA identifies ``head_sha``."""

    if not reviewed_sha or not head_sha:
        return False
    reviewed = reviewed_sha.lower()
    head = head_sha.lower()
    return len(reviewed) >= 7 and head.startswith(reviewed)


def _nodes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    nodes = value.get("nodes", [])
    return [node for node in nodes if isinstance(node, dict)]


def _login(node: dict[str, Any]) -> str:
    author = node.get("author")
    if not isinstance(author, dict):
        return ""
    login = author.get("login")
    return str(login).lower() if login else ""


def _is_bot(node: dict[str, Any]) -> bool:
    return _login(node) in BOT_LOGINS


def _body(node: dict[str, Any]) -> str:
    body = node.get("body")
    return str(body) if body else ""


def _comment_identifier(node: dict[str, Any]) -> str:
    value = node.get("databaseId")
    if value is None:
        value = node.get("id")
    return str(value) if value is not None else ""


def _reviewed_sha(text: str) -> str | None:
    match = _REVIEWED_SHA.search(text)
    return match.group(1) if match else None


def _review_sha(review: dict[str, Any]) -> str | None:
    commit = review.get("commit")
    if isinstance(commit, dict):
        oid = commit.get("oid")
        if oid:
            return str(oid)
    return _reviewed_sha(_body(review))


def _review_can_contribute_findings(review: dict[str, Any]) -> bool:
    """Exclude review states that GitHub has not published or has withdrawn."""

    return str(review.get("state") or "").upper() not in {"DISMISSED", "PENDING"}


def _reaction_from_bot(comment: dict[str, Any], content: str) -> bool:
    expected = content.upper()
    for reaction in _nodes(comment.get("reactions")):
        actor = reaction.get("user")
        login = ""
        if isinstance(actor, dict) and actor.get("login"):
            login = str(actor["login"]).lower()
        if login in BOT_LOGINS and str(reaction.get("content", "")).upper() == expected:
            return True
    return False


def _comments_after_request(
    comments: list[dict[str, Any]], requested_comment_id: str | None
) -> Iterable[dict[str, Any]]:
    """Yield comments after the stored @codex-review request, if it is present."""

    if not requested_comment_id:
        return comments
    for index, comment in enumerate(comments):
        if _comment_identifier(comment) == str(requested_comment_id):
            return comments[index + 1 :]
    # Missing tracked comment must never make a historical message authoritative.
    return []


def _bot_messages_after_request(
    comments: list[dict[str, Any]], requested_comment_id: str | None
) -> list[dict[str, Any]]:
    return [
        comment
        for comment in _comments_after_request(comments, requested_comment_id)
        if _is_bot(comment)
    ]


def _github_timestamp(value: Any) -> datetime | None:
    """Parse one GitHub timestamp, or reject an ambiguous value."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _submitted_after_request(
    item: dict[str, Any], request_time: datetime | None, *fields: str
) -> bool:
    """Return true only when a review object has a safe request-time link."""

    if request_time is None:
        return False
    for field in fields:
        timestamp = _github_timestamp(item.get(field))
        if timestamp is not None:
            return timestamp >= request_time
    return False


def _reviews_for_request(
    payload: dict[str, Any],
    comments: list[dict[str, Any]],
    requested_comment_id: str | None,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """Return only reviews that were submitted after the tracked request."""

    reviews = _nodes(payload.get("reviews"))
    if not requested_comment_id:
        return reviews, None
    request = next(
        (
            comment
            for comment in comments
            if _comment_identifier(comment) == str(requested_comment_id)
        ),
        None,
    )
    request_time = _github_timestamp((request or {}).get("createdAt"))
    if request_time is None:
        # A tracked request that lacks an exact creation time must not grant
        # authority to a historical review object or its inline comments.
        return [], None
    return (
        [
            review
            for review in reviews
            if _submitted_after_request(review, request_time, "submittedAt", "createdAt")
        ],
        request_time,
    )


def _current_head_findings(
    payload: dict[str, Any],
    head_sha: str,
    bot_messages: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    request_time: datetime | None,
) -> tuple[dict[str, Any], ...]:
    """Return badge-marked messages and inline reviews tied to the current head."""

    findings = [
        message
        for message in bot_messages
        if reviewed_sha_matches_head(_reviewed_sha(_body(message)), head_sha)
        and _FINDING_BADGE.search(_body(message))
    ]
    for review in reviews:
        if not _is_bot(review) or not _review_can_contribute_findings(review):
            continue
        review_body = _body(review)
        reviewed_sha = _review_sha(review)
        if not reviewed_sha_matches_head(reviewed_sha, head_sha):
            continue
        if _FINDING_BADGE.search(review_body):
            findings.append(review)
        for comment in _nodes(review.get("comments")):
            # A reply is untrusted discussion, even when GraphQL returns it
            # through the trusted Codex review's comment connection. Only the
            # review's original inline findings may enter a repair pipeline.
            if (
                _body(comment).strip()
                and comment.get("inReplyTo") is None
                and (
                    request_time is None
                    or _submitted_after_request(comment, request_time, "createdAt")
                )
            ):
                findings.append(comment)
    return tuple(findings)


def classify_pr(
    payload: dict[str, Any],
    requested_head: str | None,
    requested_comment_id: str | None = None,
) -> ClassificationResult:
    """Classify the exact current head from GitHub GraphQL response data.

    A bot reaction is trustworthy only when it belongs to the stored request
    comment and that request was made for the current head.  Textual clean
    verdicts and inline reviews are similarly accepted only when their embedded
    reviewed SHA identifies the current head.
    """

    head_sha = str(payload.get("headRefOid") or "")
    comments = _nodes(payload.get("comments"))
    if requested_comment_id and not any(
        _comment_identifier(comment) == str(requested_comment_id)
        for comment in comments
    ):
        # The durable request was deleted or cannot be recovered from the
        # complete paginated connection. Historical comments and reviews have
        # no authority for this review round. Ask for one fresh bounded review.
        return ClassificationResult(Classification.NEEDS_REVIEW)
    later_bot_messages = _bot_messages_after_request(comments, requested_comment_id)
    bot_messages = (
        later_bot_messages
        if requested_comment_id
        else [comment for comment in comments if _is_bot(comment)]
    )
    reviews, request_time = _reviews_for_request(
        payload, comments, requested_comment_id
    )

    for message in bot_messages:
        normalized = _body(message).lower()
        if any(marker in normalized for marker in _QUOTA_MARKERS):
            return ClassificationResult(
                Classification.QUOTA_EXHAUSTED,
                detail="Codex reported review quota exhaustion.",
            )
        if any(marker in normalized for marker in _ENVIRONMENT_MARKERS):
            return ClassificationResult(
                Classification.ENVIRONMENT_BLOCKED,
                detail="Codex reported that this repository lacks an environment.",
            )

    findings = _current_head_findings(
        payload, head_sha, bot_messages, reviews, request_time
    )
    if findings:
        reviewed_sha = next(
            (
                finding_sha
                for finding in findings
                if reviewed_sha_matches_head(
                    finding_sha := _reviewed_sha(_body(finding)), head_sha
                )
            ),
            None,
        )
        if not reviewed_sha:
            matching_review = next(
                (
                    review
                    for review in reviews
                    if _is_bot(review)
                    and _review_can_contribute_findings(review)
                    and reviewed_sha_matches_head(_review_sha(review), head_sha)
                ),
                None,
            )
            reviewed_sha = _review_sha(matching_review or {})
        return ClassificationResult(
            Classification.FINDINGS,
            reviewed_sha=reviewed_sha,
            findings=findings,
        )

    if (
        requested_head
        and requested_comment_id
        and requested_head == head_sha
    ):
        request = next(
            (
                comment
                for comment in comments
                if _comment_identifier(comment) == str(requested_comment_id)
            ),
            None,
        )
        if request:
            if _reaction_from_bot(request, "THUMBS_UP"):
                return ClassificationResult(Classification.CLEAN, reviewed_sha=head_sha)
            if _reaction_from_bot(request, "EYES"):
                return ClassificationResult(Classification.REVIEWING)

    for message in bot_messages:
        body = _body(message)
        reviewed_sha = _reviewed_sha(body)
        if reviewed_sha_matches_head(reviewed_sha, head_sha) and any(
            marker in body.lower() for marker in _CLEAN_MARKERS
        ):
            return ClassificationResult(Classification.CLEAN, reviewed_sha=reviewed_sha)

    if requested_head == head_sha:
        return ClassificationResult(Classification.REVIEWING)
    return ClassificationResult(Classification.NEEDS_REVIEW)
