"""Sanitized local state API for the native PR Autopilot Desktop plugin.

The plugin owns presentation only. The controller remains authoritative for all
pipeline and GitHub actions. This module exposes a bounded, read-only snapshot
of controller state and intentionally does not return filesystem paths,
credentials, task identifiers, raw findings, raw reviews, or raw logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from pr_autopilot import repository_storage_slug


router = APIRouter()

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SAFE_STATUS_VALUES = frozenset(
    {
        "archived",
        "blocked",
        "completed",
        "done",
        "failed",
        "ready",
        "running",
        "scheduled",
        "unknown",
    }
)
_PIPELINE_ROLES = (
    ("analyze", "Analyze"),
    ("fix", "Fix"),
    ("verify", "Verify"),
)
_INTENT_TTL = timedelta(minutes=5)
_INTENT_RETENTION = timedelta(hours=1)
_MAX_PULL_REQUESTS = 100
_MAX_REPOSITORIES = 200
_MAX_MERGE_HISTORY = 100
_PARENT_HANDOFF_MARKER = "\n\nVerified parent-stage handoff:\n"


@dataclass
class _RepositoryIntent:
    identifier: str
    repository: str
    disabled: bool
    expected_disabled: bool
    caller_binding: str
    idempotency_key: str
    created_at: datetime
    expires_at: datetime
    result: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True)
class _BlockedPipeline:
    repository: str
    number: int
    state_head_sha: str
    fix_repair_head_observed: bool
    expected_head: str
    head_ref: str
    restore_head: str
    retry_index: int
    workspace: str
    pipeline_digest: str
    task_ids: tuple[str, str, str]
    task_snapshot: tuple[tuple[Any, ...], tuple[Any, ...], tuple[Any, ...]]


@dataclass
class _PipelineResetIntent:
    identifier: str
    pipeline: _BlockedPipeline
    caller_binding: str
    idempotency_key: str
    created_at: datetime
    expires_at: datetime
    result: dict[str, Any] | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class _RepositoryIntentRequest(BaseModel):
    repository: str = Field(min_length=3, max_length=200)
    disabled: bool
    idempotency_key: str = Field(min_length=16, max_length=128)


class _ControllerPauseRequest(BaseModel):
    paused: bool


class _PipelineResetIntentRequest(BaseModel):
    repository: str = Field(min_length=3, max_length=200)
    number: int = Field(ge=1)
    idempotency_key: str = Field(min_length=16, max_length=128)


_intent_lock = threading.RLock()
_intents_by_identifier: dict[str, _RepositoryIntent] = {}
_intent_identifier_by_key: dict[tuple[str, str], str] = {}
_pipeline_reset_intents_by_identifier: dict[str, _PipelineResetIntent] = {}
_pipeline_reset_intent_identifier_by_key: dict[tuple[str, str], str] = {}


class _DashboardError(RuntimeError):
    """A client-safe failure that never carries a local exception or path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _caller_binding(request: Request) -> str:
    """Return a non-reversible identity binding for an authenticated request."""
    session = getattr(request.state, "session", None)
    session_user_id = getattr(session, "user_id", None)
    session_provider = getattr(session, "provider", None)
    session_access_token = getattr(session, "access_token", None)
    if (
        isinstance(session_user_id, str)
        and isinstance(session_provider, str)
        and isinstance(session_access_token, str)
        and session_access_token
    ):
        source = f"session:{session_provider}:{session_user_id}:{session_access_token}"
        return f"session:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"

    principal = getattr(request.state, "token_principal", None)
    principal_name = getattr(principal, "principal", None)
    principal_provider = getattr(principal, "provider", None)
    bearer = request.headers.get("authorization", "")
    if (
        isinstance(principal_name, str)
        and isinstance(principal_provider, str)
        and bearer
    ):
        source = f"token:{principal_provider}:{principal_name}:{bearer}"
        return f"token:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"

    try:
        auth_required = bool(getattr(request.app.state, "auth_required", False))
    except (AttributeError, RuntimeError):
        auth_required = True
    if not auth_required:
        try:
            from hermes_cli.web_server import _has_valid_session_token
        except Exception as error:
            raise _DashboardError(
                "caller_unavailable", "Authenticated caller identity is unavailable."
            ) from error
        if _has_valid_session_token(request):
            loopback_token = request.headers.get("X-Hermes-Session-Token", "")
            if not loopback_token:
                loopback_token = request.headers.get("authorization", "")
            if loopback_token:
                return f"loopback:{hashlib.sha256(loopback_token.encode('utf-8')).hexdigest()}"
    raise _DashboardError("caller_unavailable", "Authenticated caller identity is unavailable.")


def _controller_root() -> Path:
    """Resolve profile-scoped plugin data without exposing it to clients."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception as error:
        raise _DashboardError(
            "hermes_config_unavailable", "Hermes configuration is unavailable."
        ) from error

    if not isinstance(config, dict):
        raise _DashboardError("hermes_config_unavailable", "Hermes configuration is unavailable.")
    plugins = config.get("plugins", {})
    entries = plugins.get("entries", {}) if isinstance(plugins, dict) else {}
    entry = entries.get("pr-autopilot", {}) if isinstance(entries, dict) else {}
    settings = entry.get("settings", {}) if isinstance(entry, dict) else {}
    if not isinstance(settings, dict):
        raise _DashboardError("hermes_config_invalid", "Hermes configuration is invalid.")

    configured_root = settings.get("data_root")
    if configured_root is not None and (
        not isinstance(configured_root, str) or not configured_root.strip()
    ):
        raise _DashboardError("hermes_config_invalid", "Hermes configuration is invalid.")
    if configured_root:
        root = Path(configured_root).expanduser()
        if not root.is_absolute():
            raise _DashboardError("hermes_config_invalid", "Hermes configuration is invalid.")
    else:
        try:
            from hermes_constants import get_hermes_home
        except Exception as error:
            raise _DashboardError(
                "hermes_config_unavailable", "Hermes configuration is unavailable."
            ) from error
        root = get_hermes_home() / "plugin-data" / "pr-autopilot"
    root = root.resolve()
    plugin_root = Path(__file__).resolve().parent.parent
    try:
        root.relative_to(plugin_root)
    except ValueError:
        pass
    else:
        raise _DashboardError("hermes_config_invalid", "Hermes configuration is invalid.")
    if not root.is_dir():
        raise _DashboardError("controller_unavailable", "PR Autopilot controller is unavailable.")
    return root


def _controller_config(root: Path) -> tuple[dict[str, Any], frozenset[str]]:
    path = root / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _DashboardError(
            "controller_config_unavailable", "PR Autopilot configuration is unavailable."
        ) from error
    if not isinstance(raw, dict):
        raise _DashboardError("controller_config_invalid", "PR Autopilot configuration is invalid.")

    excluded = raw.get("excluded_repositories")
    disabled = raw.get("disabled_repositories", [])
    if (
        not isinstance(excluded, list)
        or not all(isinstance(value, str) for value in excluded)
        or not isinstance(disabled, list)
        or not all(isinstance(value, str) for value in disabled)
    ):
        raise _DashboardError("controller_config_invalid", "PR Autopilot configuration is invalid.")
    normalized = frozenset(
        _require_repository(value).casefold() for value in (*excluded, *disabled)
    )
    return raw, normalized


def _require_repository(value: Any) -> str:
    if not isinstance(value, str):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    repository = value.strip()
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    return repository


def _require_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    return value


def _short_sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA_PATTERN.fullmatch(value):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    return value[:12].lower()


def _observed_head_short(value: Any) -> str:
    """Render the controller's baseline state before GitHub supplies a head."""

    if value == "":
        return "Pending"
    return _short_sha(value)


def _bounded_text(value: Any, *, limit: int = 240) -> str:
    if not isinstance(value, str):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    return normalized


def _safe_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, limit=64)


def _state_connection(root: Path) -> sqlite3.Connection:
    path = _state_path(root)
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
    except sqlite3.Error as error:
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    connection.row_factory = sqlite3.Row
    return connection


def _state_path(root: Path) -> Path:
    path = root / "state" / "pr-autopilot.sqlite3"
    if not path.is_file():
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.")
    return path


def _require_columns(connection: sqlite3.Connection, table: str, required: set[str]) -> None:
    try:
        columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error as error:
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    if not required.issubset(columns):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")


def _pipeline_roles(
    connection: sqlite3.Connection, raw_pipeline: Any
) -> list[dict[str, Any]]:
    pipeline: dict[str, Any] = {}
    if raw_pipeline is not None:
        if not isinstance(raw_pipeline, str):
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        try:
            decoded = json.loads(raw_pipeline)
        except json.JSONDecodeError as error:
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.") from error
        if not isinstance(decoded, dict):
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        pipeline = decoded
    stages = []
    for key, label in _PIPELINE_ROLES:
        task_id = pipeline.get(key)
        bound = isinstance(task_id, str) and bool(task_id)
        status = "not_started"
        if bound:
            status = "unknown"
            task_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'desktop_worker_task'"
            ).fetchone()
            if task_table is not None:
                row = connection.execute(
                    "SELECT status FROM desktop_worker_task WHERE identifier = ?",
                    (task_id,),
                ).fetchone()
                if row is not None:
                    status = _observed_stage_status(row["status"]) or "unknown"
        stages.append({"role": label, "bound": bound, "status": status})
    return stages


def _observed_stage_status(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    return value if value in _SAFE_STATUS_VALUES else "unknown"


def _repository_url(repository: str, number: int) -> str:
    return f"https://github.com/{repository}/pull/{number}"


def _merge_history_status(row: sqlite3.Row) -> str:
    if row["invalidated_at"] is not None:
        return "invalidated"
    if row["confirmed_at"] is None:
        return "pending_confirmation"
    if row["recorded_at"] is None:
        return "pending_history"
    return "recorded"


def _control_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'controller_control'"
    ).fetchone()
    if table is None:
        return {"paused": False, "check_generation": 0, "last_cycle": None}
    _require_columns(
        connection,
        "controller_control",
        {"singleton", "paused", "check_generation", "updated_at"},
    )
    row = connection.execute(
        "SELECT paused, check_generation FROM controller_control WHERE singleton = 1"
    ).fetchone()
    if row is None or row["paused"] not in (0, 1):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    check_generation = row["check_generation"]
    if (
        isinstance(check_generation, bool)
        or not isinstance(check_generation, int)
        or check_generation < 0
    ):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    last_cycle = None
    cycle_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'controller_cycle'"
    ).fetchone()
    if cycle_table is not None:
        _require_columns(
            connection,
            "controller_cycle",
            {"started_at", "finished_at", "outcome", "event_count"},
        )
        cycle = connection.execute(
            """
            SELECT started_at, finished_at, outcome, event_count
            FROM controller_cycle ORDER BY sequence DESC LIMIT 1
            """
        ).fetchone()
        if cycle is not None:
            outcome = str(cycle["outcome"])
            event_count = cycle["event_count"]
            if outcome not in {"completed", "failed", "paused"} or (
                isinstance(event_count, bool)
                or not isinstance(event_count, int)
                or event_count < 0
            ):
                raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
            last_cycle = {
                "started_at": datetime.fromtimestamp(
                    int(cycle["started_at"]), timezone.utc
                ).isoformat(),
                "finished_at": datetime.fromtimestamp(
                    int(cycle["finished_at"]), timezone.utc
                ).isoformat(),
                "outcome": outcome,
                "event_count": event_count,
            }
    return {
        "paused": bool(row["paused"]),
        "check_generation": check_generation,
        "last_cycle": last_cycle,
    }


def _runtime_snapshot(
    connection: sqlite3.Connection, *, paused: bool
) -> dict[str, Any]:
    """Return safe controller-liveness state without exposing its owner identity."""

    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_lease'"
    ).fetchone()
    if table is None:
        return {
            "status": "inactive",
            "status_label": "Inactive",
            "lease_generation": None,
            "paused": paused,
        }
    _require_columns(
        connection,
        "runtime_lease",
        {"name", "generation", "expires_at"},
    )
    row = connection.execute(
        "SELECT generation, expires_at FROM runtime_lease WHERE name = ?",
        ("standalone-controller",),
    ).fetchone()
    if row is None or int(row["expires_at"]) <= int(time.time()):
        return {
            "status": "inactive",
            "status_label": "Inactive",
            "lease_generation": None,
            "paused": paused,
        }
    generation = row["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    return {
        "status": "paused" if paused else "running",
        "status_label": "Paused" if paused else "Running",
        "lease_generation": generation,
        "paused": paused,
    }


def _validate_requested_repository(value: str) -> str:
    repository = value.strip()
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise _DashboardError("invalid_repository", "Repository name is invalid.")
    return repository


def _repository_state(
    connection: sqlite3.Connection,
    repository: str,
    excluded_repositories: frozenset[str],
) -> tuple[str, bool, bool]:
    """Return a canonical repository, state, and whether its setting row exists."""
    key = repository.casefold()
    if key in excluded_repositories:
        raise _DashboardError(
            "repository_hard_excluded", "This repository is managed by static policy."
        )
    _require_columns(connection, "repository_settings", {"repository", "disabled"})
    _require_columns(connection, "pr_state", {"repository"})
    try:
        settings_rows = connection.execute(
            "SELECT repository, disabled FROM repository_settings WHERE lower(repository) = lower(?)",
            (repository,),
        ).fetchall()
        pr_rows = connection.execute(
            "SELECT DISTINCT repository FROM pr_state WHERE lower(repository) = lower(?)",
            (repository,),
        ).fetchall()
    except sqlite3.Error as error:
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error

    if len(settings_rows) > 1:
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    setting_repository: str | None = None
    disabled = False
    if settings_rows:
        setting_repository = _require_repository(settings_rows[0]["repository"])
        raw_disabled = settings_rows[0]["disabled"]
        if raw_disabled not in (0, 1):
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        disabled = bool(raw_disabled)

    state_repositories = {_require_repository(row["repository"]) for row in pr_rows}
    if setting_repository is None and not state_repositories:
        raise _DashboardError("repository_unknown", "This repository is not available for local control.")
    if len(state_repositories) > 1:
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    canonical = setting_repository or next(iter(state_repositories))
    if canonical.casefold() != key:
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    return canonical, disabled, setting_repository is not None


def _migrate_task_base_prompts(connection: sqlite3.Connection) -> None:
    """Add immutable base prompts before a dashboard retry reads legacy rows."""

    try:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(desktop_worker_task)")
        }
        if "prompt" not in columns:
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        if "base_prompt" not in columns:
            connection.execute("ALTER TABLE desktop_worker_task ADD COLUMN base_prompt TEXT")
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
    except sqlite3.Error as error:
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.") from error


def _blocked_pipeline(
    connection: sqlite3.Connection, repository: str, number: int
) -> _BlockedPipeline:
    """Return one resettable local pipeline or reject the state without writing."""

    _require_columns(
        connection,
        "pr_state",
        {
            "repository",
            "number",
            "head_sha",
            "active_task_id",
            "active_task_status",
            "pipeline_json",
        },
    )
    rows = connection.execute(
        """
        SELECT repository, number, head_sha, active_task_id, active_task_status, pipeline_json
        FROM pr_state
        WHERE lower(repository) = lower(?) AND number = ?
        """,
        (repository, number),
    ).fetchall()
    if len(rows) != 1:
        raise _DashboardError("pipeline_not_resettable", "No blocked pipeline is available to reset.")
    row = rows[0]
    canonical_repository = _require_repository(row["repository"])
    if canonical_repository.casefold() != repository.casefold():
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    if row["active_task_status"] != "blocked":
        raise _DashboardError("pipeline_not_resettable", "Only a blocked pipeline can be reset.")
    state_head_sha = row["head_sha"]
    if not isinstance(state_head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", state_head_sha):
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline does not have an exact head.")
    raw_pipeline = row["pipeline_json"]
    if not isinstance(raw_pipeline, str):
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is incomplete.")
    try:
        pipeline = json.loads(raw_pipeline)
    except json.JSONDecodeError as error:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is incomplete.") from error
    roles = tuple(role for role, _label in _PIPELINE_ROLES)
    if not isinstance(pipeline, dict) or set(pipeline) != set(roles):
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is incomplete.")
    task_ids = tuple(pipeline.get(role) for role in roles)
    if not all(isinstance(task_id, str) and task_id for task_id in task_ids) or len(set(task_ids)) != 3:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is incomplete.")
    if row["active_task_id"] != pipeline["verify"]:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is stale.")
    task_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'desktop_worker_task'"
    ).fetchone()
    if task_table is None:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is not locally managed.")
    _require_columns(
        connection,
        "desktop_worker_task",
        {
            "identifier",
            "repository",
            "number",
            "expected_head",
            "head_ref",
            "workspace",
            "role",
            "status",
            "runtime_attempt_id",
            "output_digest",
            "exit_code",
            "commit_sha",
            "terminal_reason",
        },
    )
    task_rows = connection.execute(
        """
        SELECT identifier, repository, number, expected_head, head_ref, workspace, role, status,
               runtime_attempt_id, output_digest, exit_code, commit_sha, terminal_reason
        FROM desktop_worker_task
        WHERE identifier IN (?, ?, ?)
        """,
        task_ids,
    ).fetchall()
    if len(task_rows) != 3:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is incomplete.")
    tasks_by_id = {str(task["identifier"]): task for task in task_rows}
    allowed_statuses = {"done", "blocked", "scheduled"}
    saw_blocked = False
    task_snapshot: list[tuple[Any, ...]] = []
    expected_head: str | None = None
    head_ref: str | None = None
    workspace: str | None = None
    for role, task_id in zip(roles, task_ids):
        task = tasks_by_id.get(task_id)
        if task is None:
            raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is incomplete.")
        if (
            _require_repository(task["repository"]).casefold() != canonical_repository.casefold()
            or _require_number(task["number"]) != number
            or task["role"] != role
        ):
            raise _DashboardError("pipeline_not_resettable", "The blocked pipeline is stale.")
        task_head = task["expected_head"]
        task_head_ref = task["head_ref"]
        task_workspace = task["workspace"]
        if not isinstance(task_head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", task_head):
            raise _DashboardError("pipeline_not_resettable", "The blocked pipeline has no exact starting head.")
        if not isinstance(task_workspace, str) or not task_workspace:
            raise _DashboardError("pipeline_not_resettable", "The blocked pipeline has no managed workspace.")
        if expected_head is None:
            expected_head = task_head.lower()
        elif task_head.lower() != expected_head:
            raise _DashboardError("pipeline_not_resettable", "The blocked pipeline has mixed starting heads.")
        if not isinstance(task_head_ref, str) or not task_head_ref or len(task_head_ref) > 255:
            raise _DashboardError("pipeline_not_resettable", "The blocked pipeline has no source branch.")
        if head_ref is None:
            head_ref = task_head_ref
        elif task_head_ref != head_ref:
            raise _DashboardError("pipeline_not_resettable", "The blocked pipeline has mixed source branches.")
        if workspace is None:
            workspace = task_workspace
        elif task_workspace != workspace:
            raise _DashboardError("pipeline_not_resettable", "The blocked pipeline has mixed workspaces.")
        if task["status"] not in allowed_statuses:
            raise _DashboardError("pipeline_not_resettable", "Running or unknown work cannot be reset.")
        saw_blocked = saw_blocked or task["status"] == "blocked"
        task_snapshot.append(
            (
                task_id,
                task["status"],
                task["runtime_attempt_id"],
                task["output_digest"],
                task["exit_code"],
                task["commit_sha"],
                task["terminal_reason"],
            )
        )
    if not saw_blocked:
        raise _DashboardError("pipeline_not_resettable", "Only a blocked pipeline can be reset.")
    statuses = tuple(str(snapshot[1]) for snapshot in task_snapshot)
    retry_index = statuses.index("blocked")
    if any(status != "done" for status in statuses[:retry_index]) or any(
        status not in {"blocked", "scheduled"} for status in statuses[retry_index:]
    ):
        raise _DashboardError(
            "pipeline_not_resettable", "The blocked pipeline stage order is incompatible."
        )
    normalized_state_head = state_head_sha.lower()
    fix_repair_head_observed = False
    if retry_index < 2:
        if normalized_state_head != expected_head:
            fix_commit = task_snapshot[1][5]
            if (
                retry_index == 1
                and isinstance(fix_commit, str)
                and re.fullmatch(r"[0-9a-fA-F]{40}", fix_commit)
                and fix_commit.lower() == normalized_state_head
            ):
                # A completed-looking Fix recorded the newer head, but its
                # pipeline is still blocked. Keep the snapshot readable so
                # the dashboard can prove whether that repair reached origin.
                # It must never be reset to the original head.
                fix_repair_head_observed = True
            else:
                raise _DashboardError(
                    "pipeline_not_resettable", "The blocked pipeline source head already changed."
                )
        restore_head = expected_head or ""
    else:
        fix_commit = task_snapshot[1][5]
        if (
            not isinstance(fix_commit, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", fix_commit)
            or fix_commit.lower() != normalized_state_head
            or normalized_state_head == expected_head
        ):
            raise _DashboardError(
                "pipeline_not_resettable", "The blocked Verify stage has no pushed Fix head."
            )
        restore_head = normalized_state_head
    return _BlockedPipeline(
        repository=canonical_repository,
        number=number,
        state_head_sha=normalized_state_head,
        fix_repair_head_observed=fix_repair_head_observed,
        expected_head=expected_head or "",
        head_ref=head_ref or "",
        restore_head=restore_head,
        retry_index=retry_index,
        workspace=workspace or "",
        pipeline_digest=hashlib.sha256(raw_pipeline.encode("utf-8")).hexdigest(),
        task_ids=task_ids,
        task_snapshot=(task_snapshot[0], task_snapshot[1], task_snapshot[2]),
    )


def _require_paused_controller(connection: sqlite3.Connection) -> None:
    """Require a manual pause before changing an existing worker pipeline."""

    _require_columns(connection, "controller_control", {"singleton", "paused", "cycle_active"})
    row = connection.execute(
        "SELECT paused, cycle_active FROM controller_control WHERE singleton = 1"
    ).fetchone()
    if row is None or row["paused"] not in (0, 1) or row["cycle_active"] not in (0, 1):
        raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
    if row["paused"] != 1:
        raise _DashboardError(
            "controller_not_paused", "Pause PR Autopilot before retrying a blocked pipeline."
        )
    if row["cycle_active"] != 0:
        raise _DashboardError(
            "controller_busy", "Wait for the active controller check before retrying a pipeline."
        )


def _intent_payload(intent: _RepositoryIntent) -> dict[str, Any]:
    return {
        "id": intent.identifier,
        "repository": intent.repository,
        "disabled": intent.disabled,
        "expires_at": intent.expires_at.isoformat(),
    }


def _cleanup_intents(now: datetime) -> None:
    for identifier, intent in tuple(_intents_by_identifier.items()):
        if now <= intent.expires_at + _INTENT_RETENTION:
            continue
        _intents_by_identifier.pop(identifier, None)
        _intent_identifier_by_key.pop((intent.caller_binding, intent.idempotency_key), None)
    for identifier, intent in tuple(_pipeline_reset_intents_by_identifier.items()):
        if now <= intent.expires_at + _INTENT_RETENTION:
            continue
        _pipeline_reset_intents_by_identifier.pop(identifier, None)
        _pipeline_reset_intent_identifier_by_key.pop(
            (intent.caller_binding, intent.idempotency_key), None
        )


def _create_repository_intent(
    request: _RepositoryIntentRequest, caller_binding: str
) -> tuple[_RepositoryIntent, bool]:
    repository = _validate_requested_repository(request.repository)
    key = request.idempotency_key.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{16,128}", key):
        raise _DashboardError("invalid_idempotency_key", "Idempotency key is invalid.")

    with _intent_lock:
        now = datetime.now(timezone.utc)
        _cleanup_intents(now)
        existing_identifier = _intent_identifier_by_key.get((caller_binding, key))
        if existing_identifier:
            existing = _intents_by_identifier.get(existing_identifier)
            if existing is not None:
                if existing.repository != repository or existing.disabled is not request.disabled:
                    raise _DashboardError(
                        "idempotency_conflict", "Idempotency key belongs to a different request."
                    )
                return existing, False

        root = _controller_root()
        _, excluded_repositories = _controller_config(root)
        connection = _state_connection(root)
        try:
            canonical_repository, expected_disabled, _has_setting = _repository_state(
                connection, repository, excluded_repositories
            )
        finally:
            connection.close()
        intent = _RepositoryIntent(
            identifier=uuid4().hex,
            repository=canonical_repository,
            disabled=request.disabled,
            expected_disabled=expected_disabled,
            caller_binding=caller_binding,
            idempotency_key=key,
            created_at=now,
            expires_at=now + _INTENT_TTL,
        )
        _intents_by_identifier[intent.identifier] = intent
        _intent_identifier_by_key[(caller_binding, key)] = intent.identifier
        return intent, True


def _apply_repository_intent(intent: _RepositoryIntent) -> dict[str, Any]:
    root = _controller_root()
    path = _state_path(root)
    try:
        connection = sqlite3.connect(path, timeout=5)
    except sqlite3.Error as error:
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        _, excluded_repositories = _controller_config(root)
        canonical_repository, current_disabled, had_setting = _repository_state(
            connection, intent.repository, excluded_repositories
        )
        if current_disabled != intent.expected_disabled:
            raise _DashboardError("intent_stale", "Repository state changed before confirmation.")
        changed = current_disabled != intent.disabled
        if changed:
            connection.execute(
                "DELETE FROM repository_settings WHERE lower(repository) = lower(?)",
                (canonical_repository,),
            )
            connection.execute(
                "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                (canonical_repository, int(intent.disabled)),
            )
            _require_columns(
                connection,
                "controller_control",
                {"singleton", "check_generation", "fence_generation", "updated_at"},
            )
            connection.execute(
                """
                UPDATE controller_control
                SET check_generation = check_generation + 1,
                    fence_generation = fence_generation + 1,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (int(time.time()),),
            )
        _, final_excluded_repositories = _controller_config(root)
        if intent.repository.casefold() in final_excluded_repositories:
            raise _DashboardError(
                "repository_hard_excluded", "This repository is managed by static policy."
            )
        connection.commit()
        _, committed_excluded_repositories = _controller_config(root)
        if intent.repository.casefold() in committed_excluded_repositories:
            if changed:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM repository_settings WHERE lower(repository) = lower(?)",
                    (canonical_repository,),
                )
                if had_setting:
                    connection.execute(
                        "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                        (canonical_repository, int(current_disabled)),
                    )
                connection.commit()
            raise _DashboardError(
                "repository_hard_excluded", "This repository is managed by static policy."
            )
    except _DashboardError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    finally:
        connection.close()
    return {
        "repository": canonical_repository,
        "disabled": intent.disabled,
        "changed": changed,
    }


def _pipeline_reset_intent_payload(intent: _PipelineResetIntent) -> dict[str, Any]:
    return {
        "id": intent.identifier,
        "repository": intent.pipeline.repository,
        "number": intent.pipeline.number,
        "expires_at": intent.expires_at.isoformat(),
    }


def _create_pipeline_reset_intent(
    request: _PipelineResetIntentRequest, caller_binding: str
) -> _PipelineResetIntent:
    repository = _validate_requested_repository(request.repository)
    number = _require_number(request.number)
    key = request.idempotency_key.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{16,128}", key):
        raise _DashboardError("invalid_idempotency_key", "Idempotency key is invalid.")
    with _intent_lock:
        now = datetime.now(timezone.utc)
        _cleanup_intents(now)
        existing_identifier = _pipeline_reset_intent_identifier_by_key.get((caller_binding, key))
        if existing_identifier:
            existing = _pipeline_reset_intents_by_identifier.get(existing_identifier)
            if existing is not None:
                if (
                    existing.pipeline.repository.casefold() != repository.casefold()
                    or existing.pipeline.number != number
                ):
                    raise _DashboardError(
                        "idempotency_conflict", "Idempotency key belongs to a different request."
                    )
                return existing
        root = _controller_root()
        connection = _state_connection(root)
        try:
            _require_paused_controller(connection)
            pipeline = _blocked_pipeline(connection, repository, number)
        finally:
            connection.close()
        if pipeline.fix_repair_head_observed or _blocked_fix_has_pushed_repair(root, pipeline):
            raise _DashboardError(
                "pipeline_not_resettable",
                "The blocked Fix requires fresh-head reconciliation.",
            )
        intent = _PipelineResetIntent(
            identifier=uuid4().hex,
            pipeline=pipeline,
            caller_binding=caller_binding,
            idempotency_key=key,
            created_at=now,
            expires_at=now + _INTENT_TTL,
        )
        _pipeline_reset_intents_by_identifier[intent.identifier] = intent
        _pipeline_reset_intent_identifier_by_key[(caller_binding, key)] = intent.identifier
        return intent


def _pipeline_worktree(root: Path, pipeline: _BlockedPipeline) -> tuple[str, Path, Path]:
    """Return the exact plugin-managed workspace path for one pipeline."""

    slug = repository_storage_slug(pipeline.repository)
    expected_relative = f"{slug}/pr-{pipeline.number}"
    if pipeline.workspace != expected_relative:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline workspace is unmanaged.")
    worktree_root = (root / "worktrees").resolve()
    worktree = (worktree_root / slug / f"pr-{pipeline.number}").resolve()
    try:
        worktree.relative_to(worktree_root)
    except ValueError as error:
        raise _DashboardError(
            "pipeline_not_resettable", "The blocked pipeline workspace is unmanaged."
        ) from error
    if not worktree.is_dir():
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline workspace is unavailable.")
    return slug, worktree_root, worktree


def _pipeline_git(worktree: Path, *arguments: str) -> str:
    """Run one bounded Git read or reset in a validated managed worktree."""

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
        raise _DashboardError(
            "pipeline_not_resettable", "The blocked pipeline workspace cannot be verified."
        ) from error
    if completed.returncode != 0:
        raise _DashboardError(
            "pipeline_not_resettable", "The blocked pipeline workspace cannot be verified."
        )
    return completed.stdout.strip()


def _blocked_fix_has_pushed_repair(root: Path, pipeline: _BlockedPipeline) -> bool:
    """Return whether a blocked Fix has a clean, remotely pushed repair head."""

    if pipeline.retry_index != 1:
        return False
    try:
        slug, _worktree_root, worktree = _pipeline_worktree(root, pipeline)
        git = lambda *arguments: _pipeline_git(worktree, *arguments)
        if git("rev-parse", "--is-inside-work-tree").lower() != "true":
            return False
        if Path(git("rev-parse", "--show-toplevel")).resolve() != worktree:
            return False
        common_dir = Path(git("rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = worktree / common_dir
        if common_dir.resolve() != (root / "repos" / slug / ".git").resolve():
            return False
        if git("status", "--porcelain", "--untracked-files=all"):
            return False
        local_head = git("rev-parse", "HEAD").lower()
        if local_head == pipeline.expected_head:
            return False
        if not re.fullmatch(r"[0-9a-f]{40}", local_head):
            return False
        git("merge-base", "--is-ancestor", pipeline.expected_head, local_head)
        git("check-ref-format", "--branch", pipeline.head_ref)
        remote_lines = [
            line.split()
            for line in git(
                "ls-remote", "--heads", "origin", f"refs/heads/{pipeline.head_ref}"
            ).splitlines()
            if line.strip()
        ]
        return (
            len(remote_lines) == 1
            and len(remote_lines[0]) == 2
            and remote_lines[0][0].lower() == local_head
            and remote_lines[0][1] == f"refs/heads/{pipeline.head_ref}"
        )
    except _DashboardError:
        return False


def _restore_pipeline_worktree(root: Path, pipeline: _BlockedPipeline) -> None:
    """Restore one clean plugin-managed worktree to the stage retry head."""

    if pipeline.fix_repair_head_observed:
        raise _DashboardError(
            "pipeline_not_resettable", "The blocked Fix requires fresh-head reconciliation."
        )
    slug, _worktree_root, worktree = _pipeline_worktree(root, pipeline)
    git = lambda *arguments: _pipeline_git(worktree, *arguments)

    if git("rev-parse", "--is-inside-work-tree").lower() != "true":
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline workspace is unmanaged.")
    if Path(git("rev-parse", "--show-toplevel")).resolve() != worktree:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline workspace is unmanaged.")
    common_dir = Path(git("rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = worktree / common_dir
    expected_common_dir = (root / "repos" / slug / ".git").resolve()
    if common_dir.resolve() != expected_common_dir:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline workspace is unmanaged.")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline workspace is dirty.")
    if git("rev-parse", "--verify", f"{pipeline.restore_head}^{{commit}}").lower() != pipeline.restore_head:
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline retry head is unavailable.")
    if git("rev-parse", "HEAD").lower() != pipeline.restore_head:
        if pipeline.retry_index == 1:
            raise _DashboardError(
                "pipeline_not_resettable",
                "The blocked Fix requires fresh-head reconciliation.",
            )
        raise _DashboardError(
            "pipeline_not_resettable", "The blocked pipeline workspace is not at its retry head."
        )
    git("reset", "--hard", pipeline.restore_head)
    if git("rev-parse", "HEAD").lower() != pipeline.restore_head or git(
        "status", "--porcelain", "--untracked-files=all"
    ):
        raise _DashboardError("pipeline_not_resettable", "The blocked pipeline workspace could not be restored.")


def _validate_pipeline_reset_intent(
    connection: sqlite3.Connection,
    intent: _PipelineResetIntent,
    *,
    caller_binding: str,
) -> _BlockedPipeline:
    """Validate the live reset controls and durable pipeline snapshot."""

    now = datetime.now(timezone.utc)
    live_intent = _pipeline_reset_intents_by_identifier.get(intent.identifier)
    if live_intent is not intent or not hmac.compare_digest(
        intent.caller_binding, caller_binding
    ):
        raise _DashboardError("intent_not_found", "Confirmation request was not found.")
    if now > intent.expires_at:
        raise _DashboardError("intent_expired", "Confirmation request expired.")
    _require_paused_controller(connection)
    pipeline = _blocked_pipeline(
        connection, intent.pipeline.repository, intent.pipeline.number
    )
    if pipeline != intent.pipeline:
        raise _DashboardError("intent_stale", "Pipeline state changed before confirmation.")
    return pipeline


def _apply_pipeline_reset_intent(
    intent: _PipelineResetIntent, *, caller_binding: str
) -> dict[str, Any]:
    root = _controller_root()
    try:
        connection = sqlite3.connect(_state_path(root), timeout=5)
    except sqlite3.Error as error:
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    connection.row_factory = sqlite3.Row
    try:
        # Validate under the write lock, then release it before Git touches the
        # worktree. A slow filesystem operation must not block lease renewal
        # or other controller writes.
        connection.execute("BEGIN IMMEDIATE")
        pipeline = _validate_pipeline_reset_intent(
            connection, intent, caller_binding=caller_binding
        )
        connection.commit()
        if pipeline.fix_repair_head_observed or _blocked_fix_has_pushed_repair(root, pipeline):
            raise _DashboardError(
                "pipeline_not_resettable",
                "The blocked Fix requires fresh-head reconciliation.",
            )
        _restore_pipeline_worktree(root, pipeline)

        # The durable state can change while the worktree is restored. Obtain
        # a fresh lock and require the same authenticated intent, controller
        # state, and exact pipeline snapshot before making any DB changes.
        connection.execute("BEGIN IMMEDIATE")
        pipeline = _validate_pipeline_reset_intent(
            connection, intent, caller_binding=caller_binding
        )
        _migrate_task_base_prompts(connection)
        _require_columns(
            connection,
            "desktop_worker_task",
            {
                "identifier",
                "base_prompt",
                "prompt",
                "status",
                "runtime_attempt_id",
                "output",
                "output_digest",
                "exit_code",
                "commit_sha",
                "terminal_reason",
                "updated_at",
            },
        )
        now = int(time.time())
        retry_task_ids = pipeline.task_ids[pipeline.retry_index :]
        retry_placeholders = ", ".join("?" for _task_id in retry_task_ids)
        invalid_prompt = connection.execute(
            f"""
            SELECT 1 FROM desktop_worker_task
            WHERE identifier IN ({retry_placeholders})
              AND (base_prompt IS NULL OR trim(base_prompt) = '')
            """,
            retry_task_ids,
        ).fetchone()
        if invalid_prompt is not None:
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        connection.execute(
            f"""
            UPDATE desktop_worker_task
            SET prompt = base_prompt, status = 'scheduled', output = NULL, output_digest = NULL, exit_code = NULL, commit_sha = NULL,
                terminal_reason = NULL, updated_at = ?
            WHERE identifier IN ({retry_placeholders})
            """,
            (now, *retry_task_ids),
        )
        _require_columns(
            connection,
            "desktop_worker_event",
            {"task_id", "kind", "detail", "created_at"},
        )
        for task_id in retry_task_ids:
            connection.execute(
                """
                INSERT INTO desktop_worker_event (task_id, kind, detail, created_at)
                VALUES (?, 'retry_scheduled', 'operator-confirmed exact-head retry', ?)
                """,
                (task_id, now),
            )
        updated = connection.execute(
            """
            UPDATE pr_state
            SET active_task_id = ?, active_task_status = 'scheduled'
            WHERE repository = ? AND number = ? AND head_sha = ?
              AND active_task_status = 'blocked'
            """,
            (pipeline.task_ids[2], pipeline.repository, pipeline.number, pipeline.state_head_sha),
        )
        if updated.rowcount != 1:
            raise _DashboardError("intent_stale", "Pipeline state changed before confirmation.")
        _require_columns(
            connection,
            "controller_control",
            {
                "singleton",
                "check_generation",
                "fence_generation",
                "cycle_active",
                "updated_at",
            },
        )
        control = connection.execute(
            """
            UPDATE controller_control
            SET check_generation = check_generation + 1,
                fence_generation = fence_generation + 1, updated_at = ?
            WHERE singleton = 1
            """,
            (now,),
        )
        if control.rowcount != 1:
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        row = connection.execute(
            "SELECT check_generation FROM controller_control WHERE singleton = 1"
        ).fetchone()
        if row is None or isinstance(row["check_generation"], bool) or not isinstance(
            row["check_generation"], int
        ):
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        connection.commit()
    except _DashboardError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    finally:
        connection.close()
    return {
        "repository": pipeline.repository,
        "number": pipeline.number,
        "retried": True,
        "check_generation": row["check_generation"],
    }


def _http_status(error: _DashboardError) -> int:
    if error.code == "caller_unavailable":
        return 401
    if error.code in {"invalid_repository", "invalid_idempotency_key"}:
        return 422
    if error.code in {"repository_unknown", "intent_not_found"}:
        return 404
    if error.code in {
        "repository_hard_excluded",
        "controller_busy",
        "controller_not_paused",
        "idempotency_conflict",
        "intent_expired",
        "intent_stale",
        "pipeline_not_resettable",
    }:
        return 409
    return 503


def _raise_dashboard_error(error: _DashboardError) -> None:
    raise HTTPException(
        status_code=_http_status(error),
        detail={"code": error.code, "message": error.message},
    ) from error


def _overview() -> dict[str, Any]:
    root = _controller_root()
    config, excluded_repositories = _controller_config(root)
    connection = _state_connection(root)
    try:
        _require_columns(
            connection,
            "pr_state",
            {
                "repository",
                "number",
                "updated_at",
                "head_sha",
                "requested_at",
                "review_rounds",
                "active_task_id",
                "active_task_status",
                "pending_finding_fingerprint",
                "pending_findings_json",
                "pipeline_json",
            },
        )
        _require_columns(connection, "repository_settings", {"repository", "disabled"})
        _require_columns(
            connection,
            "merge_history",
            {
                "repository",
                "number",
                "title",
                "head_sha",
                "merged_at",
                "confirmed_at",
                "invalidated_at",
                "recorded_at",
            },
        )

        controller = _control_snapshot(connection)
        runtime = _runtime_snapshot(connection, paused=controller["paused"])

        duplicate_repository = connection.execute(
            """
            SELECT source, lower(repository)
            FROM (
                SELECT 'repository_settings' AS source, repository FROM repository_settings
                UNION ALL
                SELECT 'pr_state' AS source, repository FROM pr_state
            )
            GROUP BY source, lower(repository)
            HAVING count(DISTINCT repository) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate_repository is not None:
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")

        settings_rows = connection.execute(
            """
            SELECT repository, disabled
            FROM repository_settings
            ORDER BY repository COLLATE NOCASE
            LIMIT ?
            """,
            (_MAX_REPOSITORIES + 1,),
        ).fetchall()
        repositories_truncated = len(settings_rows) > _MAX_REPOSITORIES
        settings_rows = settings_rows[:_MAX_REPOSITORIES]
        settings: dict[str, tuple[str, bool]] = {}
        for row in settings_rows:
            repository = _require_repository(row["repository"])
            disabled = row["disabled"]
            if disabled not in (0, 1):
                raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
            repository_key = repository.casefold()
            if repository_key in settings:
                raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
            settings[repository_key] = (repository, bool(disabled))

        repository_rows = connection.execute(
            """
            SELECT DISTINCT repository
            FROM pr_state
            ORDER BY repository COLLATE NOCASE
            LIMIT ?
            """,
            (_MAX_REPOSITORIES + 1,),
        ).fetchall()
        repositories_truncated = repositories_truncated or len(repository_rows) > _MAX_REPOSITORIES
        repository_rows = repository_rows[:_MAX_REPOSITORIES]
        known_repositories = dict(settings)
        for row in repository_rows:
            repository = _require_repository(row["repository"])
            repository_key = repository.casefold()
            if repository_key in known_repositories:
                # StateStore normalizes durable preference rows to lowercase.
                # Prefer the controller's PR-state spelling for presentation
                # while retaining the durable enabled/disabled value.
                known_repositories[repository_key] = (
                    repository,
                    known_repositories[repository_key][1],
                )
            elif len(known_repositories) < _MAX_REPOSITORIES:
                known_repositories[repository_key] = (repository, False)
            else:
                repositories_truncated = True

        pr_rows = connection.execute(
            """
            SELECT repository, number, head_sha, requested_at, review_rounds, active_task_id,
                   active_task_status, pending_finding_fingerprint,
                   pending_findings_json, pipeline_json
            FROM pr_state
            ORDER BY updated_at DESC, repository COLLATE NOCASE, number
            LIMIT ?
            """,
            (_MAX_PULL_REQUESTS + 1,),
        ).fetchall()
        pull_requests_truncated = len(pr_rows) > _MAX_PULL_REQUESTS
        pr_rows = pr_rows[:_MAX_PULL_REQUESTS]
        pull_requests: list[dict[str, Any]] = []
        for row in pr_rows:
            repository = _require_repository(row["repository"])
            number = _require_number(row["number"])
            review_rounds = row["review_rounds"]
            if isinstance(review_rounds, bool) or not isinstance(review_rounds, int) or review_rounds < 0:
                raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
            try:
                pipeline = _blocked_pipeline(connection, repository, number)
                resettable = not (
                    pipeline.fix_repair_head_observed
                    or _blocked_fix_has_pushed_repair(root, pipeline)
                )
            except _DashboardError:
                resettable = False
            pull_requests.append(
                {
                    "repository": repository,
                    "number": number,
                    "url": _repository_url(repository, number),
                    "head_short": _observed_head_short(row["head_sha"]),
                    "review_rounds": review_rounds,
                    "requested_at": _safe_timestamp(row["requested_at"]),
                    "observed_stage_status": _observed_stage_status(row["active_task_status"]),
                    "pipeline": _pipeline_roles(connection, row["pipeline_json"]),
                    "pending_findings": bool(
                        row["pending_finding_fingerprint"] or row["pending_findings_json"]
                    ),
                    "resettable": resettable,
                }
            )

        for repository_key in sorted(excluded_repositories):
            if repository_key not in known_repositories:
                if len(known_repositories) < _MAX_REPOSITORIES:
                    known_repositories[repository_key] = (repository_key, False)
                else:
                    repositories_truncated = True
        repositories = [
            {
                "repository": repository,
                "disabled": disabled,
                "hard_excluded": repository_key in excluded_repositories,
                "mutable": repository_key not in excluded_repositories,
            }
            for repository_key, (repository, disabled) in known_repositories.items()
        ]
        repositories.sort(key=lambda entry: (entry["repository"].casefold(), entry["repository"]))

        merge_rows = connection.execute(
            """
            SELECT repository, number, title, head_sha, merged_at, confirmed_at,
                   invalidated_at, recorded_at
            FROM merge_history
            ORDER BY merged_at DESC, repository COLLATE NOCASE, number DESC
            LIMIT ?
            """,
            (_MAX_MERGE_HISTORY + 1,),
        ).fetchall()
        merge_history_truncated = len(merge_rows) > _MAX_MERGE_HISTORY
        merge_rows = merge_rows[:_MAX_MERGE_HISTORY]
        merge_history = []
        for row in merge_rows:
            repository = _require_repository(row["repository"])
            number = _require_number(row["number"])
            merge_history.append(
                {
                    "repository": repository,
                    "number": number,
                    "title": _bounded_text(row["title"]),
                    "url": _repository_url(repository, number),
                    "head_short": _short_sha(row["head_sha"]),
                    "merged_at": _safe_timestamp(row["merged_at"]),
                    "status": _merge_history_status(row),
                }
            )
    except sqlite3.Error as error:
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    finally:
        connection.close()

    policy_revision = config.get("policy_revision")
    if isinstance(policy_revision, bool) or not isinstance(policy_revision, int) or policy_revision < 1:
        raise _DashboardError("controller_config_invalid", "PR Autopilot configuration is invalid.")

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "health": {"overall": "ok" if runtime["status"] == "running" else "degraded"},
        "policy": {"revision": policy_revision},
        "runtime": runtime,
        "controller": controller,
        "state_store": {"read_only": True},
        "repositories": repositories,
        "pull_requests": pull_requests,
        "merge_history": merge_history,
        "truncated": {
            "pull_requests": pull_requests_truncated,
            "repositories": repositories_truncated,
            "merge_history": merge_history_truncated,
        },
    }


@router.get("/overview")
def get_overview(http_request: Request) -> dict[str, Any]:
    """Return a bounded sanitized controller snapshot."""
    try:
        _caller_binding(http_request)
        return _overview()
    except _DashboardError as error:
        _raise_dashboard_error(error)


def _write_controller_control(*, paused: bool | None) -> dict[str, Any]:
    root = _controller_root()
    try:
        connection = sqlite3.connect(_state_path(root), timeout=5)
    except sqlite3.Error as error:
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_columns(
            connection,
            "controller_control",
            {
                "singleton",
                "paused",
                "check_generation",
                "fence_generation",
                "updated_at",
            },
        )
        now = int(time.time())
        if paused is None:
            updated = connection.execute(
                """
                UPDATE controller_control
                SET check_generation = check_generation + 1, updated_at = ?
                WHERE singleton = 1
                """,
                (now,),
            )
        else:
            updated = connection.execute(
                """
                UPDATE controller_control
                SET fence_generation = fence_generation
                        + CASE WHEN paused = 0 AND ? = 1 THEN 1 ELSE 0 END,
                    paused = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (int(paused), int(paused), now),
            )
        if updated.rowcount != 1:
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        row = connection.execute(
            "SELECT paused, check_generation FROM controller_control WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise _DashboardError("state_incompatible", "PR Autopilot state is incompatible.")
        connection.commit()
    except _DashboardError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        raise _DashboardError("state_unavailable", "PR Autopilot state is unavailable.") from error
    finally:
        connection.close()
    return {"paused": bool(row["paused"]), "check_generation": int(row["check_generation"])}


@router.post("/controller/check")
def request_controller_check(http_request: Request) -> dict[str, Any]:
    """Request one near-term deterministic controller check."""
    try:
        _caller_binding(http_request)
        result = _write_controller_control(paused=None)
        return {"check_generation": result["check_generation"]}
    except _DashboardError as error:
        _raise_dashboard_error(error)


@router.post("/controller/pause")
def set_controller_pause(
    http_request: Request, request: _ControllerPauseRequest
) -> dict[str, Any]:
    """Pause or resume controller checks without cancelling active workers."""
    try:
        _caller_binding(http_request)
        result = _write_controller_control(paused=request.paused)
        return {"paused": result["paused"]}
    except _DashboardError as error:
        _raise_dashboard_error(error)


@router.post("/pipeline-reset-intents", status_code=201)
def create_pipeline_reset_intent(
    http_request: Request, request: _PipelineResetIntentRequest
) -> dict[str, Any]:
    """Prepare one authenticated retry of a blocked exact-head local pipeline."""

    try:
        intent = _create_pipeline_reset_intent(request, _caller_binding(http_request))
        return {"intent": _pipeline_reset_intent_payload(intent)}
    except _DashboardError as error:
        _raise_dashboard_error(error)


@router.post("/pipeline-reset-intents/{identifier}/confirm")
def confirm_pipeline_reset_intent(http_request: Request, identifier: str) -> dict[str, Any]:
    """Schedule a confirmed retry only if the blocked pipeline is unchanged."""

    try:
        caller_binding = _caller_binding(http_request)
    except _DashboardError as error:
        _raise_dashboard_error(error)
    with _intent_lock:
        now = datetime.now(timezone.utc)
        _cleanup_intents(now)
        intent = _pipeline_reset_intents_by_identifier.get(identifier)
        if intent is None or not hmac.compare_digest(intent.caller_binding, caller_binding):
            _raise_dashboard_error(
                _DashboardError("intent_not_found", "Confirmation request was not found.")
            )
        if intent.result is not None:
            return intent.result
        if intent.failure_code is not None and intent.failure_message is not None:
            _raise_dashboard_error(_DashboardError(intent.failure_code, intent.failure_message))
        if now > intent.expires_at:
            error = _DashboardError("intent_expired", "Confirmation request expired.")
            intent.failure_code = error.code
            intent.failure_message = error.message
            _raise_dashboard_error(error)
        try:
            intent.result = _apply_pipeline_reset_intent(
                intent, caller_binding=caller_binding
            )
            return intent.result
        except _DashboardError as error:
            intent.failure_code = error.code
            intent.failure_message = error.message
            _raise_dashboard_error(error)


@router.post("/repository-intents", status_code=201)
def create_repository_intent(
    http_request: Request, request: _RepositoryIntentRequest
) -> dict[str, Any]:
    """Create a non-executing confirmation intent for one repository switch."""
    try:
        intent, _created = _create_repository_intent(request, _caller_binding(http_request))
        return {"intent": _intent_payload(intent)}
    except _DashboardError as error:
        _raise_dashboard_error(error)


@router.post("/repository-intents/{identifier}/confirm")
def confirm_repository_intent(http_request: Request, identifier: str) -> dict[str, Any]:
    """Apply the exact server-issued repository intent once after confirmation."""
    try:
        caller_binding = _caller_binding(http_request)
    except _DashboardError as error:
        _raise_dashboard_error(error)
    with _intent_lock:
        now = datetime.now(timezone.utc)
        _cleanup_intents(now)
        intent = _intents_by_identifier.get(identifier)
        if intent is None:
            _raise_dashboard_error(
                _DashboardError("intent_not_found", "Confirmation request was not found.")
            )
        if not hmac.compare_digest(intent.caller_binding, caller_binding):
            _raise_dashboard_error(
                _DashboardError("intent_not_found", "Confirmation request was not found.")
            )
        if intent.result is not None:
            return intent.result
        if intent.failure_code is not None and intent.failure_message is not None:
            _raise_dashboard_error(_DashboardError(intent.failure_code, intent.failure_message))
        if now > intent.expires_at:
            error = _DashboardError("intent_expired", "Confirmation request expired.")
            intent.failure_code = error.code
            intent.failure_message = error.message
            _raise_dashboard_error(error)
        try:
            intent.result = _apply_repository_intent(intent)
            return intent.result
        except _DashboardError as error:
            intent.failure_code = error.code
            intent.failure_message = error.message
            _raise_dashboard_error(error)
