"""Contract tests for the native PR Autopilot Desktop plugin."""

from __future__ import annotations

from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import ModuleType
import unittest
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from pr_autopilot import repository_storage_slug
from pr_reconciler import StateStore
from standalone_controller import ControllerControlStore, StandaloneTaskClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API_PATH = PROJECT_ROOT / "dashboard" / "plugin_api.py"
HEAD = "a" * 40
REPOSITORY_SLUG = repository_storage_slug("AnikaWilliams/example")


class _RetryRuntime:
    def __init__(self) -> None:
        self.launches: list[dict[str, object]] = []
        self.statuses: dict[str, str] = {}
        self.outputs: dict[str, str] = {}

    def launch(self, owner: str, definition_id: str, **values: object) -> SimpleNamespace:
        self.launches.append({"owner": owner, "definition_id": definition_id, **values})
        self.statuses[str(values["attempt_id"])] = "running"
        return SimpleNamespace(status="running")

    def observe(self, _owner: str, attempt_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=self.statuses[attempt_id])

    def list_events(self, _owner: str, attempt_id: str, *, limit: int) -> list[SimpleNamespace]:
        del limit
        output = self.outputs.get(attempt_id)
        return [SimpleNamespace(kind="output", detail=output)] if output is not None else []

    def succeed(self, attempt_id: str, *, head: str, role: str, handoff: str) -> None:
        self.outputs[attempt_id] = (
            f"{handoff}\nPR_AUTOPILOT_RESULT:"
            f'{json.dumps({"commit_sha": head, "role": role}, sort_keys=True)}\n'
        )
        self.statuses[attempt_id] = "succeeded"


class _ManagedWorktreeGit:
    """Bounded Git command model for dashboard worktree recovery contracts."""

    def __init__(
        self,
        workspace: Path,
        common_dir: Path,
        *,
        expected_head: str,
        current_head: str,
        remote_head: str | None = None,
        dirty: bool = False,
        managed: bool = True,
    ) -> None:
        self.workspace = workspace
        self.common_dir = common_dir
        self.expected_head = expected_head
        self.current_head = current_head
        self.remote_head = remote_head
        self.dirty = dirty
        self.managed = managed
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        command = tuple(arguments[3:])
        self.calls.append(command)
        output = ""
        if command == ("rev-parse", "--is-inside-work-tree"):
            output = "true" if self.managed else "false"
        elif command == ("rev-parse", "--show-toplevel"):
            output = str(self.workspace)
        elif command == ("rev-parse", "--git-common-dir"):
            output = str(self.common_dir)
        elif command == ("status", "--porcelain", "--untracked-files=all"):
            output = "?? uncommitted.txt" if self.dirty else ""
        elif (
            len(command) == 3
            and command[:2] == ("rev-parse", "--verify")
            and command[2].endswith("^{commit}")
            and command[2][:-9] in {self.expected_head, self.current_head}
        ):
            output = command[2][:-9]
        elif command == ("reset", "--hard", self.expected_head):
            self.current_head = self.expected_head
        elif command == ("rev-parse", "HEAD"):
            output = self.current_head
        elif command == ("merge-base", "--is-ancestor", self.expected_head, self.current_head):
            output = ""
        elif command == ("check-ref-format", "--branch", "test-pr"):
            output = ""
        elif command == ("ls-remote", "--heads", "origin", "refs/heads/test-pr"):
            output = (
                f"{self.remote_head or self.current_head} refs/heads/test-pr\n"
            )
        else:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=output)


def load_plugin_api():
    """Load the dashboard module as the production plugin loader does."""
    if not PLUGIN_API_PATH.is_file():
        raise FileNotFoundError(f"plugin API is missing: {PLUGIN_API_PATH}")

    module_name = f"test_pr_autopilot_dashboard_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_API_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load plugin API module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


class PluginApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "state").mkdir()
        (self.root / "config.json").write_text(
            json.dumps(
                {
                    "excluded_repositories": ["AnikaWilliams/blocked"],
                    "policy_revision": 1,
                    "recovery_api_key_env": "MUST_NOT_APPEAR_IN_THE_DASHBOARD",
                }
            ),
            encoding="utf-8",
        )
        self.state_path = self.root / "state" / "pr-autopilot.sqlite3"
        StateStore(self.state_path)
        ControllerControlStore(self.state_path)
        self._seed_state()

        self.plugin_api = load_plugin_api()
        self.root_patch = patch.object(
            self.plugin_api,
            "_controller_root",
            return_value=self.root,
        )
        self.root_patch.start()
        self.application = FastAPI()
        self.application.state.auth_required = True

        @self.application.middleware("http")
        async def authenticate_test_request(request, call_next):
            token = request.headers.get("X-Hermes-Session-Token")
            if token in {"issuing-desktop-session", "different-desktop-session"}:
                request.state.session = SimpleNamespace(
                    access_token=token,
                    provider="test-provider",
                    user_id="test-user",
                )
            return await call_next(request)

        self.application.include_router(self.plugin_api.router, prefix="/api/plugins/pr-autopilot")
        self.client = TestClient(
            self.application,
            headers={"X-Hermes-Session-Token": "issuing-desktop-session"},
        )

    def tearDown(self) -> None:
        self.client.close()
        self.root_patch.stop()
        self.temporary_directory.cleanup()

    def _seed_state(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                """
                INSERT INTO pr_state (
                    repository, number, updated_at, head_sha, policy_revision,
                    requested_head, requested_at, review_rounds, active_task_id,
                    active_task_status, pending_finding_fingerprint, pipeline_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "AnikaWilliams/example",
                    18,
                    "2026-08-20T10:00:00Z",
                    HEAD,
                    1,
                    HEAD,
                    "2026-08-20T09:00:00Z",
                    2,
                    "t_internal_task_id",
                    "running",
                    "finding_fingerprint_must_not_leak",
                    json.dumps(
                        {
                            "analyze": "t_analysis_internal",
                            "fix": "t_fix_internal",
                            "verify": "t_verify_internal",
                        }
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                ("AnikaWilliams/example", 1),
            )
            connection.execute(
                "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                ("AnikaWilliams/blocked", 0),
            )
            connection.execute(
                """
                INSERT INTO merge_history (
                    repository, number, title, url, head_sha, merged_at, confirmed_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "AnikaWilliams/example",
                    17,
                    "A safe merged change",
                    "https://github.com/AnikaWilliams/example/pull/17",
                    HEAD,
                    "2026-08-19T10:00:00Z",
                    "2026-08-19T10:00:01Z",
                    "2026-08-19T10:00:02Z",
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _seed_blocked_pipeline_workers(self, statuses: tuple[str, str, str]) -> None:
        """Create the local rows that bind the displayed pipeline to its head."""

        identifiers = ("t_analysis_internal", "t_fix_internal", "t_verify_internal")
        workspace = self.root / "worktrees" / REPOSITORY_SLUG / "pr-18"
        workspace.mkdir(parents=True)
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                """
                CREATE TABLE desktop_worker_task (
                    identifier TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    expected_head TEXT NOT NULL,
                    head_ref TEXT NOT NULL,
                    finding_fingerprint TEXT NOT NULL,
                    role TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    parent_id TEXT,
                    definition_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    skill TEXT NOT NULL,
                    max_turns INTEGER NOT NULL,
                    workspace TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    runtime_attempt_id TEXT,
                    output TEXT,
                    output_digest TEXT,
                    exit_code INTEGER,
                    commit_sha TEXT,
                    terminal_reason TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE desktop_worker_event (
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO desktop_worker_task (
                    identifier, repository, number, expected_head, head_ref, finding_fingerprint,
                    role, position, parent_id, definition_id, profile, skill, max_turns, workspace,
                    prompt, status,
                    runtime_attempt_id, output, output_digest, exit_code, commit_sha,
                    terminal_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        identifier,
                        "AnikaWilliams/example",
                        18,
                        HEAD,
                        "test-pr",
                        "finding_fingerprint_must_not_leak",
                        role,
                        position,
                        parent_id,
                        f"pr-autopilot-{role}",
                        f"profile-{role}",
                        f"skill-{role}",
                        11 + position,
                        workspace.relative_to(self.root / "worktrees").as_posix(),
                        prompt,
                        status,
                        f"initial-{role}-attempt",
                        "old worker result",
                        "old-output-digest",
                        0 if status == "done" else 1,
                        (
                            HEAD if role == "analyze" else "b" * 40
                        ) if status == "done" else None,
                        None if status == "done" else "worker failed safely",
                        1,
                        1,
                    )
                    for identifier, role, position, parent_id, prompt, status in zip(
                        identifiers,
                        ("analyze", "fix", "verify"),
                        (1, 2, 3),
                        (None, identifiers[0], identifiers[1]),
                        (
                            f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                            (
                                f"Required head SHA: {HEAD}\nSource branch: test-pr"
                                "\n\nVerified parent-stage handoff:\nstale analyze evidence\n"
                            ),
                            (
                                f"Original reviewed SHA: {HEAD}\nSource branch: test-pr"
                                "\n\nVerified parent-stage handoff:\nstale fix evidence\n"
                            ),
                        ),
                        statuses,
                    )
                ],
            )
            connection.execute(
                """
                UPDATE pr_state
                SET active_task_id = ?, active_task_status = ?
                WHERE repository = ? AND number = ?
                """,
                ("t_verify_internal", "blocked", "AnikaWilliams/example", 18),
            )
            connection.commit()
        finally:
            connection.close()

    def _advance_managed_pipeline_worktree(self) -> tuple[_ManagedWorktreeGit, str, str]:
        """Make the Fix result differ from the original pipeline head."""

        workspace = self.root / "worktrees" / REPOSITORY_SLUG / "pr-18"
        repository = self.root / "repos" / REPOSITORY_SLUG
        common_dir = repository / ".git"
        common_dir.mkdir(parents=True)
        original_head = HEAD
        fixed_head = "b" * 40

        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute("UPDATE desktop_worker_task SET expected_head = ?", (original_head,))
            connection.execute(
                "UPDATE pr_state SET head_sha = ? WHERE repository = ? AND number = ?",
                (fixed_head, "AnikaWilliams/example", 18),
            )
            connection.commit()
        finally:
            connection.close()
        return (
            _ManagedWorktreeGit(
                workspace,
                common_dir,
                expected_head=fixed_head,
                current_head=fixed_head,
            ),
            original_head,
            fixed_head,
        )

    def test_overview_exposes_sanitized_controller_state(self) -> None:
        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        serialized = json.dumps(payload)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["state_store"], {"read_only": True})
        self.assertEqual(
            payload["runtime"],
            {
                "status": "inactive",
                "status_label": "Inactive",
                "lease_generation": None,
                "paused": True,
            },
        )
        self.assertEqual(payload["health"], {"overall": "degraded"})
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("MUST_NOT_APPEAR_IN_THE_DASHBOARD", serialized)
        self.assertNotIn("t_internal_task_id", serialized)
        self.assertNotIn("finding_fingerprint_must_not_leak", serialized)

        self.assertEqual(
            payload["pull_requests"],
            [
                {
                    "repository": "AnikaWilliams/example",
                    "number": 18,
                    "url": "https://github.com/AnikaWilliams/example/pull/18",
                    "head_short": HEAD[:12],
                    "review_rounds": 2,
                    "requested_at": "2026-08-20T09:00:00Z",
                    "observed_stage_status": "running",
                    "pipeline": [
                        {"role": "Analyze", "bound": True, "status": "unknown"},
                        {"role": "Fix", "bound": True, "status": "unknown"},
                        {"role": "Verify", "bound": True, "status": "unknown"},
                    ],
                    "pending_findings": True,
                    "resettable": False,
                }
            ],
        )

        repositories = {entry["repository"]: entry for entry in payload["repositories"]}
        self.assertEqual(
            repositories["AnikaWilliams/example"],
            {
                "repository": "AnikaWilliams/example",
                "disabled": True,
                "hard_excluded": False,
                "mutable": True,
            },
        )
        self.assertEqual(
            repositories["AnikaWilliams/blocked"],
            {
                "repository": "AnikaWilliams/blocked",
                "disabled": False,
                "hard_excluded": True,
                "mutable": False,
            },
        )
        self.assertEqual(
            payload["merge_history"],
            [
                {
                    "repository": "AnikaWilliams/example",
                    "number": 17,
                    "title": "A safe merged change",
                    "url": "https://github.com/AnikaWilliams/example/pull/17",
                    "head_short": HEAD[:12],
                    "merged_at": "2026-08-19T10:00:00Z",
                    "status": "recorded",
                }
            ],
        )

    def test_controller_controls_require_authentication_and_persist(self) -> None:
        check = self.client.post("/api/plugins/pr-autopilot/controller/check")
        resume = self.client.post(
            "/api/plugins/pr-autopilot/controller/pause", json={"paused": False}
        )
        pause = self.client.post(
            "/api/plugins/pr-autopilot/controller/pause", json={"paused": True}
        )
        repeated_pause = self.client.post(
            "/api/plugins/pr-autopilot/controller/pause", json={"paused": True}
        )

        self.assertEqual(check.status_code, 200)
        self.assertEqual(check.json(), {"check_generation": 1})
        self.assertEqual(resume.status_code, 200)
        self.assertEqual(resume.json(), {"paused": False})
        self.assertEqual(pause.status_code, 200)
        self.assertEqual(pause.json(), {"paused": True})
        self.assertEqual(repeated_pause.status_code, 200)
        overview = self.client.get("/api/plugins/pr-autopilot/overview").json()
        self.assertTrue(overview["runtime"]["paused"])
        connection = sqlite3.connect(self.state_path)
        try:
            fence_generation = connection.execute(
                "SELECT fence_generation FROM controller_control WHERE singleton = 1"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(fence_generation, 1)

        forged = TestClient(
            self.application,
            headers={"X-Hermes-Session-Token": "forged-desktop-session"},
        )
        try:
            rejected = forged.post("/api/plugins/pr-autopilot/controller/check")
        finally:
            forged.close()
        self.assertEqual(rejected.status_code, 401)

    def test_forged_sessions_cannot_pause_or_resume_the_controller(self) -> None:
        baseline = ControllerControlStore(self.state_path).snapshot()
        forged = TestClient(
            self.application,
            headers={"X-Hermes-Session-Token": "forged-desktop-session"},
        )
        try:
            for paused in (True, False):
                with self.subTest(paused=paused):
                    response = forged.post(
                        "/api/plugins/pr-autopilot/controller/pause",
                        json={"paused": paused},
                    )

                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(ControllerControlStore(self.state_path).snapshot(), baseline)
        finally:
            forged.close()

    def test_forged_or_missing_sessions_cannot_read_the_controller_overview(self) -> None:
        for headers in (
            {"X-Hermes-Session-Token": "forged-desktop-session"},
            {},
        ):
            with self.subTest(headers=headers):
                client = TestClient(self.application, headers=headers)
                try:
                    response = client.get("/api/plugins/pr-autopilot/overview")
                finally:
                    client.close()

                self.assertEqual(response.status_code, 401)
                serialized = json.dumps(response.json())
                self.assertNotIn("AnikaWilliams/example", serialized)
                self.assertNotIn(HEAD, serialized)
                self.assertNotIn("t_internal_task_id", serialized)

    def test_paused_pipeline_retry_resumes_verify_from_the_pushed_fix_head(self) -> None:
        self._seed_blocked_pipeline_workers(("done", "done", "blocked"))
        git, original_head, fixed_head = self._advance_managed_pipeline_worktree()
        controls = ControllerControlStore(self.state_path)
        controls.set_paused(True)
        generation = controls.snapshot().check_generation

        creation = self.client.post(
            "/api/plugins/pr-autopilot/pipeline-reset-intents",
            json={
                "repository": "AnikaWilliams/example",
                "number": 18,
                "idempotency_key": "reset-blocked-pipeline-contract-test",
            },
        )

        self.assertEqual(creation.status_code, 201)
        intent = creation.json()["intent"]
        self.assertEqual(
            {key: intent[key] for key in ("repository", "number")},
            {"repository": "AnikaWilliams/example", "number": 18},
        )
        self.assertIn("id", intent)
        self.assertIn("expires_at", intent)
        self.assertNotIn(HEAD, json.dumps(intent))

        with patch.object(self.plugin_api.subprocess, "run", side_effect=git.run):
            confirmation = self.client.post(
                f"/api/plugins/pr-autopilot/pipeline-reset-intents/{intent['id']}/confirm"
            )

        self.assertEqual(confirmation.status_code, 200)
        self.assertEqual(
            confirmation.json(),
            {
                "repository": "AnikaWilliams/example",
                "number": 18,
                "retried": True,
                "check_generation": generation + 1,
            },
        )
        connection = sqlite3.connect(self.state_path)
        try:
            state = connection.execute(
                """
                SELECT head_sha, requested_head, review_rounds, active_task_id, active_task_status,
                       pipeline_json, pending_finding_fingerprint, pending_findings_json
                FROM pr_state WHERE repository = ? AND number = ?
                """,
                ("AnikaWilliams/example", 18),
            ).fetchone()
            worker_rows = connection.execute(
                """
                SELECT status, runtime_attempt_id, output, output_digest, exit_code, commit_sha,
                       terminal_reason
                FROM desktop_worker_task ORDER BY role
                """
            ).fetchall()
            audit_count = connection.execute(
                "SELECT count(*) FROM desktop_worker_event"
            ).fetchone()[0]
            prompts = connection.execute(
                "SELECT role, base_prompt, prompt FROM desktop_worker_task ORDER BY role"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(state[:3], (fixed_head, HEAD, 2))
        self.assertEqual(state[3], "t_verify_internal")
        self.assertEqual(state[4], "scheduled")
        self.assertEqual(
            json.loads(state[5]),
            {
                "analyze": "t_analysis_internal",
                "fix": "t_fix_internal",
                "verify": "t_verify_internal",
            },
        )
        self.assertEqual(
            prompts,
            [
                (
                    "analyze",
                    f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                    f"Required starting SHA: {HEAD}\nSource branch: test-pr",
                ),
                (
                    "fix",
                    f"Required head SHA: {HEAD}\nSource branch: test-pr",
                    (
                        f"Required head SHA: {HEAD}\nSource branch: test-pr"
                        "\n\nVerified parent-stage handoff:\nstale analyze evidence\n"
                    ),
                ),
                (
                    "verify",
                    f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
                    f"Original reviewed SHA: {HEAD}\nSource branch: test-pr",
                ),
            ],
        )
        self.assertEqual(state[6], "finding_fingerprint_must_not_leak")
        self.assertIsNone(state[7])
        self.assertEqual(
            worker_rows,
            [
                (
                    "done",
                    "initial-analyze-attempt",
                    "old worker result",
                    "old-output-digest",
                    0,
                    HEAD,
                    None,
                ),
                (
                    "done",
                    "initial-fix-attempt",
                    "old worker result",
                    "old-output-digest",
                    0,
                    fixed_head,
                    None,
                ),
                ("scheduled", "initial-verify-attempt", None, None, None, None, None),
            ],
        )
        self.assertEqual(audit_count, 1)
        self.assertEqual(git.current_head, fixed_head)
        self.assertIn(("reset", "--hard", fixed_head), git.calls)
        self.assertNotIn(("reset", "--hard", original_head), git.calls)

        retry_runtime = _RetryRuntime()
        retry_client = StandaloneTaskClient(
            SimpleNamespace(
                state_path=self.state_path,
                worktree_root=self.root / "worktrees",
                analysis_max_turns=12,
                fix_max_turns=13,
                verification_max_turns=14,
            ),
            runtime=retry_runtime,
            owner="pr-autopilot",
            definition_ids={
                "analyze": "pr-autopilot-analyze",
                "fix": "pr-autopilot-fix",
                "verify": "pr-autopilot-verify",
            },
        )
        self.assertEqual(retry_client.status("t_verify_internal"), "running")
        self.assertEqual(len(retry_runtime.launches), 1)
        retry_launch = retry_runtime.launches[0]
        self.assertEqual(retry_launch["work_item_ref"], "t_verify_internal")
        self.assertNotEqual(retry_launch["attempt_id"], "initial-verify-attempt")
        self.assertNotEqual(retry_launch["attempt_id"], "t_verify_internal")
        retry_runtime.succeed(
            str(retry_launch["attempt_id"]),
            head=fixed_head,
            role="verify",
            handoff="fresh verify evidence",
        )
        self.assertEqual(retry_client.status("t_verify_internal"), "done")
        connection = sqlite3.connect(self.state_path)
        try:
            base_prompt, retry_prompt = connection.execute(
                "SELECT base_prompt, prompt FROM desktop_worker_task WHERE identifier = ?",
                ("t_verify_internal",),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            retry_prompt,
            f"{base_prompt}\n\nVerified parent-stage handoff:\nold worker result\n",
        )
        self.assertEqual(retry_prompt.count("Verified parent-stage handoff:"), 1)

    def test_pipeline_retry_rejects_an_expired_intent_without_git_or_state_changes(self) -> None:
        self._seed_blocked_pipeline_workers(("done", "done", "blocked"))
        self._advance_managed_pipeline_worktree()
        ControllerControlStore(self.state_path).set_paused(True)
        creation = self.client.post(
            "/api/plugins/pr-autopilot/pipeline-reset-intents",
            json={
                "repository": "AnikaWilliams/example",
                "number": 18,
                "idempotency_key": "expired-pipeline-retry-contract-test",
            },
        )
        self.assertEqual(creation.status_code, 201)
        intent_id = creation.json()["intent"]["id"]

        def persisted_snapshot() -> tuple[tuple[object, ...], list[tuple[object, ...]], int]:
            connection = sqlite3.connect(self.state_path)
            try:
                state = connection.execute(
                    """
                    SELECT head_sha, active_task_id, active_task_status, pipeline_json
                    FROM pr_state WHERE repository = ? AND number = ?
                    """,
                    ("AnikaWilliams/example", 18),
                ).fetchone()
                tasks = connection.execute(
                    """
                    SELECT identifier, status, runtime_attempt_id, output, output_digest,
                           exit_code, commit_sha, terminal_reason
                    FROM desktop_worker_task ORDER BY role
                    """
                ).fetchall()
                events = connection.execute(
                    "SELECT count(*) FROM desktop_worker_event"
                ).fetchone()[0]
            finally:
                connection.close()
            return state, tasks, events

        before_confirmation = persisted_snapshot()
        with self.plugin_api._intent_lock:
            intent = self.plugin_api._pipeline_reset_intents_by_identifier[intent_id]
            after_expiry = intent.expires_at + timedelta(seconds=1)
        with (
            patch.object(self.plugin_api, "datetime") as clock,
            patch.object(self.plugin_api.subprocess, "run") as git_run,
        ):
            clock.now.return_value = after_expiry
            confirmation = self.client.post(
                f"/api/plugins/pr-autopilot/pipeline-reset-intents/{intent_id}/confirm"
            )

        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "intent_expired")
        git_run.assert_not_called()
        self.assertEqual(persisted_snapshot(), before_confirmation)

    def test_pipeline_retry_releases_the_write_lock_and_revalidates_after_restore(self) -> None:
        self._seed_blocked_pipeline_workers(("done", "done", "blocked"))
        self._advance_managed_pipeline_worktree()
        ControllerControlStore(self.state_path).set_paused(True)
        creation = self.client.post(
            "/api/plugins/pr-autopilot/pipeline-reset-intents",
            json={
                "repository": "AnikaWilliams/example",
                "number": 18,
                "idempotency_key": "two-phase-reset-revalidation-test",
            },
        )
        self.assertEqual(creation.status_code, 201)
        intent_id = creation.json()["intent"]["id"]

        def persisted_snapshot() -> tuple[tuple[object, ...], list[tuple[object, ...]], int]:
            connection = sqlite3.connect(self.state_path)
            try:
                state = connection.execute(
                    """
                    SELECT head_sha, active_task_id, active_task_status, pipeline_json
                    FROM pr_state WHERE repository = ? AND number = ?
                    """,
                    ("AnikaWilliams/example", 18),
                ).fetchone()
                tasks = connection.execute(
                    """
                    SELECT identifier, status, runtime_attempt_id, output, output_digest,
                           exit_code, commit_sha, terminal_reason
                    FROM desktop_worker_task ORDER BY role
                    """
                ).fetchall()
                events = connection.execute(
                    "SELECT count(*) FROM desktop_worker_event"
                ).fetchone()[0]
            finally:
                connection.close()
            return state, tasks, events

        lease_store = StateStore(self.state_path)
        initial_lease = lease_store.acquire_runtime_lease(
            name="standalone-controller",
            owner_id="concurrent-controller",
            now=100,
            ttl_seconds=30,
        )
        self.assertIsNotNone(initial_lease)
        assert initial_lease is not None
        restore_entered = threading.Event()
        allow_restore = threading.Event()
        confirmation_responses: list[object] = []
        confirmation_errors: list[BaseException] = []
        lease_results: list[object] = []
        lease_errors: list[BaseException] = []
        lease_finished = threading.Event()

        def slow_restore(_root: Path, _pipeline: object) -> None:
            restore_entered.set()
            if not allow_restore.wait(timeout=5):
                raise RuntimeError("test did not release the worktree restoration")

        def confirm() -> None:
            client = TestClient(
                self.application,
                headers={"X-Hermes-Session-Token": "issuing-desktop-session"},
            )
            try:
                confirmation_responses.append(
                    client.post(
                        f"/api/plugins/pr-autopilot/pipeline-reset-intents/{intent_id}/confirm"
                    )
                )
            except BaseException as error:
                confirmation_errors.append(error)
            finally:
                client.close()

        def renew_lease() -> None:
            try:
                lease_results.append(
                    lease_store.acquire_runtime_lease(
                        name="standalone-controller",
                        owner_id="concurrent-controller",
                        now=101,
                        ttl_seconds=30,
                        renew_generation=initial_lease.generation,
                    )
                )
            except BaseException as error:
                lease_errors.append(error)
            finally:
                lease_finished.set()

        with patch.object(self.plugin_api, "_restore_pipeline_worktree", side_effect=slow_restore):
            confirmation_thread = threading.Thread(target=confirm)
            confirmation_thread.start()
            try:
                self.assertTrue(restore_entered.wait(timeout=3))
                lease_thread = threading.Thread(target=renew_lease)
                lease_thread.start()
                self.assertTrue(lease_finished.wait(timeout=3))
                self.assertEqual(lease_errors, [])
                self.assertEqual(len(lease_results), 1)
                self.assertIsNotNone(lease_results[0])

                connection = sqlite3.connect(self.state_path)
                try:
                    connection.execute(
                        "UPDATE desktop_worker_task SET output = ?, output_digest = ? WHERE identifier = ?",
                        ("concurrent worker transition", "concurrent-digest", "t_verify_internal"),
                    )
                    connection.commit()
                finally:
                    connection.close()
                changed_snapshot = persisted_snapshot()
            finally:
                allow_restore.set()
                confirmation_thread.join(timeout=5)
                if "lease_thread" in locals():
                    lease_thread.join(timeout=5)

        self.assertFalse(confirmation_thread.is_alive())
        self.assertEqual(confirmation_errors, [])
        self.assertEqual(len(confirmation_responses), 1)
        confirmation = confirmation_responses[0]
        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "intent_stale")
        self.assertEqual(persisted_snapshot(), changed_snapshot)

    def test_pipeline_retry_rejects_a_dirty_managed_worktree_without_state_changes(self) -> None:
        self._seed_blocked_pipeline_workers(("done", "done", "blocked"))
        git, _original_head, fixed_head = self._advance_managed_pipeline_worktree()
        ControllerControlStore(self.state_path).set_paused(True)
        creation = self.client.post(
            "/api/plugins/pr-autopilot/pipeline-reset-intents",
            json={
                "repository": "AnikaWilliams/example",
                "number": 18,
                "idempotency_key": "dirty-managed-pipeline-retry-test",
            },
        )
        self.assertEqual(creation.status_code, 201)
        git.dirty = True

        with patch.object(self.plugin_api.subprocess, "run", side_effect=git.run):
            confirmation = self.client.post(
                f"/api/plugins/pr-autopilot/pipeline-reset-intents/{creation.json()['intent']['id']}/confirm"
            )

        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "pipeline_not_resettable")
        self.assertEqual(git.current_head, fixed_head)
        self.assertNotIn(("reset", "--hard", HEAD), git.calls)
        connection = sqlite3.connect(self.state_path)
        try:
            state = connection.execute(
                "SELECT active_task_status FROM pr_state WHERE repository = ? AND number = ?",
                ("AnikaWilliams/example", 18),
            ).fetchone()
            statuses = connection.execute(
                "SELECT status FROM desktop_worker_task ORDER BY role"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(state, ("blocked",))
        self.assertEqual(statuses, [("done",), ("done",), ("blocked",)])

    def test_pipeline_retry_rejects_an_unmanaged_worktree_without_state_changes(self) -> None:
        self._seed_blocked_pipeline_workers(("done", "done", "blocked"))
        git, _original_head, fixed_head = self._advance_managed_pipeline_worktree()
        git.managed = False
        ControllerControlStore(self.state_path).set_paused(True)
        creation = self.client.post(
            "/api/plugins/pr-autopilot/pipeline-reset-intents",
            json={
                "repository": "AnikaWilliams/example",
                "number": 18,
                "idempotency_key": "unmanaged-worktree-retry-test",
            },
        )
        self.assertEqual(creation.status_code, 201)

        with patch.object(self.plugin_api.subprocess, "run", side_effect=git.run):
            confirmation = self.client.post(
                f"/api/plugins/pr-autopilot/pipeline-reset-intents/{creation.json()['intent']['id']}/confirm"
            )

        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "pipeline_not_resettable")
        self.assertEqual(git.current_head, fixed_head)
        connection = sqlite3.connect(self.state_path)
        try:
            state = connection.execute(
                "SELECT active_task_status FROM pr_state WHERE repository = ? AND number = ?",
                ("AnikaWilliams/example", 18),
            ).fetchone()
            statuses = connection.execute(
                "SELECT status FROM desktop_worker_task ORDER BY role"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(state, ("blocked",))
        self.assertEqual(statuses, [("done",), ("done",), ("blocked",)])

    def test_blocked_fix_with_a_recorded_pushed_repair_is_not_resettable(self) -> None:
        """A durable pushed Fix head must require fresh-head reconciliation."""
        fixed_head = "b" * 40
        self._seed_blocked_pipeline_workers(("done", "blocked", "scheduled"))
        git, original_head, _fixed_head = self._advance_managed_pipeline_worktree()
        git.expected_head = original_head
        git.current_head = fixed_head
        git.remote_head = fixed_head
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "UPDATE desktop_worker_task SET commit_sha = ? WHERE identifier = ?",
                (fixed_head, "t_fix_internal"),
            )
            connection.commit()
            before_state = connection.execute(
                "SELECT head_sha, active_task_status, pipeline_json FROM pr_state "
                "WHERE repository = ? AND number = ?",
                ("AnikaWilliams/example", 18),
            ).fetchone()
        finally:
            connection.close()
        ControllerControlStore(self.state_path).set_paused(True)

        with patch.object(self.plugin_api.subprocess, "run", side_effect=git.run):
            overview = self.client.get("/api/plugins/pr-autopilot/overview")
        self.assertEqual(overview.status_code, 200)
        pull_request = next(
            entry
            for entry in overview.json()["pull_requests"]
            if entry["repository"] == "AnikaWilliams/example" and entry["number"] == 18
        )
        self.assertFalse(pull_request["resettable"])

        with patch.object(self.plugin_api.subprocess, "run", side_effect=git.run):
            creation = self.client.post(
                "/api/plugins/pr-autopilot/pipeline-reset-intents",
                json={
                    "repository": "AnikaWilliams/example",
                    "number": 18,
                    "idempotency_key": "blocked-fix-pushed-head-reconciliation-test",
                },
            )

        self.assertEqual(creation.status_code, 409)
        self.assertEqual(creation.json()["detail"]["code"], "pipeline_not_resettable")
        self.assertIn("fresh-head reconciliation", creation.json()["detail"]["message"])
        self.assertEqual(git.calls, [])
        connection = sqlite3.connect(self.state_path)
        try:
            after_state = connection.execute(
                "SELECT head_sha, active_task_status, pipeline_json FROM pr_state "
                "WHERE repository = ? AND number = ?",
                ("AnikaWilliams/example", 18),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(after_state, before_state)

    def test_blocked_fix_with_an_unrecorded_pushed_repair_is_not_offered_for_reset(self) -> None:
        """A clean local and remote repair must not permit a stale-head retry."""
        self._seed_blocked_pipeline_workers(("done", "blocked", "scheduled"))
        git, original_head, fixed_head = self._advance_managed_pipeline_worktree()
        git.expected_head = original_head
        git.current_head = fixed_head
        git.remote_head = fixed_head
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "UPDATE pr_state SET head_sha = ? WHERE repository = ? AND number = ?",
                (original_head, "AnikaWilliams/example", 18),
            )
            connection.commit()
            before_state = connection.execute(
                "SELECT head_sha, active_task_status, pipeline_json FROM pr_state "
                "WHERE repository = ? AND number = ?",
                ("AnikaWilliams/example", 18),
            ).fetchone()
        finally:
            connection.close()
        ControllerControlStore(self.state_path).set_paused(True)

        with patch.object(self.plugin_api.subprocess, "run", side_effect=git.run):
            overview = self.client.get("/api/plugins/pr-autopilot/overview")
        self.assertEqual(overview.status_code, 200)
        pull_request = next(
            entry
            for entry in overview.json()["pull_requests"]
            if entry["repository"] == "AnikaWilliams/example" and entry["number"] == 18
        )
        self.assertFalse(pull_request["resettable"])

        with patch.object(self.plugin_api.subprocess, "run", side_effect=git.run):
            creation = self.client.post(
                "/api/plugins/pr-autopilot/pipeline-reset-intents",
                json={
                    "repository": "AnikaWilliams/example",
                    "number": 18,
                    "idempotency_key": "blocked-fix-unrecorded-pushed-head-test",
                },
            )

        self.assertEqual(creation.status_code, 409)
        self.assertEqual(creation.json()["detail"]["code"], "pipeline_not_resettable")
        self.assertIn("fresh-head reconciliation", creation.json()["detail"]["message"])
        self.assertIn(
            ("ls-remote", "--heads", "origin", "refs/heads/test-pr"), git.calls
        )
        self.assertNotIn(("reset", "--hard", original_head), git.calls)
        connection = sqlite3.connect(self.state_path)
        try:
            after_state = connection.execute(
                "SELECT head_sha, active_task_status, pipeline_json FROM pr_state "
                "WHERE repository = ? AND number = ?",
                ("AnikaWilliams/example", 18),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(after_state, before_state)

    def test_pipeline_retry_rejects_a_reset_while_a_controller_cycle_is_active(self) -> None:
        self._seed_blocked_pipeline_workers(("done", "done", "blocked"))
        git, _original_head, fixed_head = self._advance_managed_pipeline_worktree()
        ControllerControlStore(self.state_path).set_paused(True)
        creation = self.client.post(
            "/api/plugins/pr-autopilot/pipeline-reset-intents",
            json={
                "repository": "AnikaWilliams/example",
                "number": 18,
                "idempotency_key": "active-cycle-pipeline-retry-test",
            },
        )
        self.assertEqual(creation.status_code, 201)

        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute("UPDATE controller_control SET cycle_active = 1 WHERE singleton = 1")
            connection.commit()
        finally:
            connection.close()

        with patch.object(self.plugin_api.subprocess, "run", side_effect=git.run):
            confirmation = self.client.post(
                f"/api/plugins/pr-autopilot/pipeline-reset-intents/{creation.json()['intent']['id']}/confirm"
            )

        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "controller_busy")
        self.assertEqual(git.current_head, fixed_head)
        connection = sqlite3.connect(self.state_path)
        try:
            state = connection.execute(
                "SELECT active_task_status FROM pr_state WHERE repository = ? AND number = ?",
                ("AnikaWilliams/example", 18),
            ).fetchone()
            statuses = connection.execute(
                "SELECT status FROM desktop_worker_task ORDER BY role"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(state, ("blocked",))
        self.assertEqual(statuses, [("done",), ("done",), ("blocked",)])

    def test_pipeline_reset_rejects_a_worker_that_started_after_its_intent(self) -> None:
        self._seed_blocked_pipeline_workers(("done", "blocked", "blocked"))
        ControllerControlStore(self.state_path).set_paused(True)
        creation = self.client.post(
            "/api/plugins/pr-autopilot/pipeline-reset-intents",
            json={
                "repository": "AnikaWilliams/example",
                "number": 18,
                "idempotency_key": "reject-running-pipeline-reset-test",
            },
        )
        self.assertEqual(creation.status_code, 201)
        intent_id = creation.json()["intent"]["id"]

        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "UPDATE desktop_worker_task SET status = 'running' WHERE identifier = ?",
                ("t_fix_internal",),
            )
            connection.commit()
        finally:
            connection.close()

        confirmation = self.client.post(
            f"/api/plugins/pr-autopilot/pipeline-reset-intents/{intent_id}/confirm"
        )

        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "pipeline_not_resettable")
        connection = sqlite3.connect(self.state_path)
        try:
            state = connection.execute(
                """
                SELECT active_task_id, active_task_status, pipeline_json,
                       pending_finding_fingerprint
                FROM pr_state WHERE repository = ? AND number = ?
                """,
                ("AnikaWilliams/example", 18),
            ).fetchone()
            worker_statuses = connection.execute(
                "SELECT status FROM desktop_worker_task ORDER BY role"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(state[0], "t_verify_internal")
        self.assertEqual(state[1], "blocked")
        self.assertIsNotNone(state[2])
        self.assertEqual(state[3], "finding_fingerprint_must_not_leak")
        self.assertEqual(worker_statuses, [("done",), ("running",), ("blocked",)])

    def test_pipeline_reset_creation_and_confirmation_require_authentication(self) -> None:
        self._seed_blocked_pipeline_workers(("done", "blocked", "blocked"))
        ControllerControlStore(self.state_path).set_paused(True)

        def persisted_snapshot() -> tuple[tuple[object, ...], list[tuple[object, ...]], int]:
            connection = sqlite3.connect(self.state_path)
            try:
                state = connection.execute(
                    """
                    SELECT head_sha, active_task_id, active_task_status, pipeline_json
                    FROM pr_state WHERE repository = ? AND number = ?
                    """,
                    ("AnikaWilliams/example", 18),
                ).fetchone()
                workers = connection.execute(
                    """
                    SELECT identifier, status, runtime_attempt_id, output, commit_sha
                    FROM desktop_worker_task ORDER BY role
                    """
                ).fetchall()
                events = connection.execute(
                    "SELECT count(*) FROM desktop_worker_event"
                ).fetchone()[0]
            finally:
                connection.close()
            return state, workers, events

        baseline = persisted_snapshot()
        forged = TestClient(
            self.application,
            headers={"X-Hermes-Session-Token": "forged-desktop-session"},
        )
        outsider = TestClient(
            self.application,
            headers={"X-Hermes-Session-Token": "different-desktop-session"},
        )
        try:
            rejected_creation = forged.post(
                "/api/plugins/pr-autopilot/pipeline-reset-intents",
                json={
                    "repository": "AnikaWilliams/example",
                    "number": 18,
                    "idempotency_key": "forged-pipeline-reset-creation-test",
                },
            )
            self.assertEqual(rejected_creation.status_code, 401)
            self.assertEqual(persisted_snapshot(), baseline)

            creation = self.client.post(
                "/api/plugins/pr-autopilot/pipeline-reset-intents",
                json={
                    "repository": "AnikaWilliams/example",
                    "number": 18,
                    "idempotency_key": "authenticated-reset-for-forged-confirm-test",
                },
            )
            self.assertEqual(creation.status_code, 201)
            with patch.object(self.plugin_api.subprocess, "run") as git_run:
                other_session_confirmation = outsider.post(
                    "/api/plugins/pr-autopilot/pipeline-reset-intents/"
                    f"{creation.json()['intent']['id']}/confirm"
                )
            self.assertEqual(other_session_confirmation.status_code, 404)
            self.assertEqual(
                other_session_confirmation.json()["detail"]["code"], "intent_not_found"
            )
            git_run.assert_not_called()
            self.assertEqual(persisted_snapshot(), baseline)

            with patch.object(self.plugin_api.subprocess, "run") as git_run:
                rejected_confirmation = forged.post(
                    "/api/plugins/pr-autopilot/pipeline-reset-intents/"
                    f"{creation.json()['intent']['id']}/confirm"
                )
            self.assertEqual(rejected_confirmation.status_code, 401)
            git_run.assert_not_called()
            self.assertEqual(persisted_snapshot(), baseline)
        finally:
            outsider.close()
            forged.close()

    def test_confirmed_repository_intent_changes_one_canonical_row_once(self) -> None:
        creation = self.client.post(
            "/api/plugins/pr-autopilot/repository-intents",
            json={
                "repository": "AnikaWilliams/example",
                "disabled": False,
                "idempotency_key": "enable-example-for-plugin-contract-test",
            },
        )

        self.assertEqual(creation.status_code, 201)
        intent = creation.json()["intent"]
        self.assertEqual(intent["repository"], "AnikaWilliams/example")
        self.assertFalse(intent["disabled"])
        self.assertIn("expires_at", intent)
        self.assertTrue(self._repository_disabled("AnikaWilliams/example"))

        repeated_creation = self.client.post(
            "/api/plugins/pr-autopilot/repository-intents",
            json={
                "repository": "AnikaWilliams/example",
                "disabled": False,
                "idempotency_key": "enable-example-for-plugin-contract-test",
            },
        )
        self.assertEqual(repeated_creation.status_code, 201)
        self.assertEqual(repeated_creation.json()["intent"]["id"], intent["id"])
        self.assertTrue(self._repository_disabled("AnikaWilliams/example"))

        confirmation = self.client.post(
            f"/api/plugins/pr-autopilot/repository-intents/{intent['id']}/confirm"
        )

        self.assertEqual(confirmation.status_code, 200)
        self.assertEqual(
            confirmation.json(),
            {
                "repository": "AnikaWilliams/example",
                "disabled": False,
                "changed": True,
            },
        )
        self.assertFalse(self._repository_disabled("AnikaWilliams/example"))
        self.assertEqual(self._repository_row_count("AnikaWilliams/example"), 1)
        control = ControllerControlStore(self.state_path).snapshot()
        self.assertEqual(control.check_generation, 1)
        self.assertEqual(control.fence_generation, 1)

        replay = self.client.post(
            f"/api/plugins/pr-autopilot/repository-intents/{intent['id']}/confirm"
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), confirmation.json())
        self.assertEqual(self._repository_row_count("AnikaWilliams/example"), 1)
        replayed_control = ControllerControlStore(self.state_path).snapshot()
        self.assertEqual(replayed_control.check_generation, 1)
        self.assertEqual(replayed_control.fence_generation, 1)

    def test_expired_repository_intent_changes_no_repository_or_controller_state(self) -> None:
        """Expired Enable and Disable confirmations are fail-closed before writes."""
        for disabled, initially_disabled in ((False, True), (True, False)):
            with self.subTest(disabled=disabled):
                connection = sqlite3.connect(self.state_path)
                try:
                    connection.execute(
                        "UPDATE repository_settings SET disabled = ? WHERE repository = ?",
                        (int(initially_disabled), "AnikaWilliams/example"),
                    )
                    connection.commit()
                    before_pr_states = connection.execute(
                        "SELECT * FROM pr_state ORDER BY repository, number"
                    ).fetchall()
                finally:
                    connection.close()
                before_repository = self._repository_disabled("AnikaWilliams/example")
                before_control = ControllerControlStore(self.state_path).snapshot()
                creation = self.client.post(
                    "/api/plugins/pr-autopilot/repository-intents",
                    json={
                        "repository": "AnikaWilliams/example",
                        "disabled": disabled,
                        "idempotency_key": f"expired-repository-intent-{disabled}",
                    },
                )
                self.assertEqual(creation.status_code, 201)
                intent_id = creation.json()["intent"]["id"]
                with self.plugin_api._intent_lock:
                    intent = self.plugin_api._intents_by_identifier[intent_id]
                    after_expiry = intent.expires_at + timedelta(seconds=1)

                with patch.object(self.plugin_api, "datetime") as clock:
                    clock.now.return_value = after_expiry
                    confirmation = self.client.post(
                        f"/api/plugins/pr-autopilot/repository-intents/{intent_id}/confirm"
                    )

                self.assertEqual(confirmation.status_code, 409)
                self.assertEqual(confirmation.json()["detail"]["code"], "intent_expired")
                self.assertEqual(
                    self._repository_disabled("AnikaWilliams/example"), before_repository
                )
                self.assertEqual(ControllerControlStore(self.state_path).snapshot(), before_control)
                connection = sqlite3.connect(self.state_path)
                try:
                    after_pr_states = connection.execute(
                        "SELECT * FROM pr_state ORDER BY repository, number"
                    ).fetchall()
                finally:
                    connection.close()
                self.assertEqual(after_pr_states, before_pr_states)

    def test_confirmation_is_bound_to_the_issuing_desktop_session(self) -> None:
        creation = self.client.post(
            "/api/plugins/pr-autopilot/repository-intents",
            json={
                "repository": "AnikaWilliams/example",
                "disabled": False,
                "idempotency_key": "session-bound-plugin-contract-test",
            },
        )
        self.assertEqual(creation.status_code, 201)
        intent_id = creation.json()["intent"]["id"]

        outsider = TestClient(
            self.application,
            headers={"X-Hermes-Session-Token": "different-desktop-session"},
        )
        try:
            rejected = outsider.post(
                f"/api/plugins/pr-autopilot/repository-intents/{intent_id}/confirm"
            )
        finally:
            outsider.close()

        self.assertEqual(rejected.status_code, 404)
        self.assertEqual(rejected.json()["detail"]["code"], "intent_not_found")
        self.assertTrue(self._repository_disabled("AnikaWilliams/example"))

        confirmation = self.client.post(
            f"/api/plugins/pr-autopilot/repository-intents/{intent_id}/confirm"
        )
        self.assertEqual(confirmation.status_code, 200)
        self.assertFalse(self._repository_disabled("AnikaWilliams/example"))

    def test_session_binding_uses_the_session_credential(self) -> None:
        first = Request({"type": "http", "headers": []})
        first.state.session = SimpleNamespace(
            access_token="first-access-token",
            provider="test-provider",
            user_id="same-user",
        )
        second = Request({"type": "http", "headers": []})
        second.state.session = SimpleNamespace(
            access_token="second-access-token",
            provider="test-provider",
            user_id="same-user",
        )

        self.assertNotEqual(
            self.plugin_api._caller_binding(first),
            self.plugin_api._caller_binding(second),
        )

    def test_rejects_a_forged_session_header_without_host_authentication(self) -> None:
        forged = TestClient(
            self.application,
            headers={"X-Hermes-Session-Token": "forged-desktop-session"},
        )
        try:
            response = forged.post(
                "/api/plugins/pr-autopilot/repository-intents",
                json={
                    "repository": "AnikaWilliams/example",
                    "disabled": False,
                    "idempotency_key": "forged-session-plugin-contract-test",
                },
            )
        finally:
            forged.close()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "caller_unavailable")
        self.assertTrue(self._repository_disabled("AnikaWilliams/example"))

    def test_stale_repository_intent_does_not_overwrite_newer_state(self) -> None:
        creation = self.client.post(
            "/api/plugins/pr-autopilot/repository-intents",
            json={
                "repository": "AnikaWilliams/example",
                "disabled": False,
                "idempotency_key": "stale-example-for-plugin-contract-test",
            },
        )
        self.assertEqual(creation.status_code, 201)
        intent_id = creation.json()["intent"]["id"]

        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "UPDATE repository_settings SET disabled = 0 WHERE repository = ?",
                ("AnikaWilliams/example",),
            )
            connection.commit()
        finally:
            connection.close()

        confirmation = self.client.post(
            f"/api/plugins/pr-autopilot/repository-intents/{intent_id}/confirm"
        )
        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "intent_stale")
        self.assertFalse(self._repository_disabled("AnikaWilliams/example"))
        self.assertEqual(self._repository_row_count("AnikaWilliams/example"), 1)

        replay = self.client.post(
            f"/api/plugins/pr-autopilot/repository-intents/{intent_id}/confirm"
        )
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["detail"]["code"], "intent_stale")
        self.assertFalse(self._repository_disabled("AnikaWilliams/example"))

    def test_hard_excluded_repository_cannot_create_an_intent(self) -> None:
        response = self.client.post(
            "/api/plugins/pr-autopilot/repository-intents",
            json={
                "repository": "AnikaWilliams/blocked",
                "disabled": True,
                "idempotency_key": "blocked-repository-plugin-contract-test",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "repository_hard_excluded")
        self.assertFalse(self._repository_disabled("AnikaWilliams/blocked"))

    def test_configured_disabled_repository_is_immutable_in_the_dashboard(self) -> None:
        config_path = self.root / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["disabled_repositories"] = ["AnikaWilliams/example"]
        config_path.write_text(json.dumps(config), encoding="utf-8")

        overview = self.client.get("/api/plugins/pr-autopilot/overview")
        repositories = {
            entry["repository"]: entry for entry in overview.json()["repositories"]
        }
        self.assertEqual(overview.status_code, 200)
        self.assertTrue(repositories["AnikaWilliams/example"]["disabled"])
        self.assertTrue(repositories["AnikaWilliams/example"]["hard_excluded"])
        self.assertFalse(repositories["AnikaWilliams/example"]["mutable"])

        response = self.client.post(
            "/api/plugins/pr-autopilot/repository-intents",
            json={
                "repository": "AnikaWilliams/example",
                "disabled": False,
                "idempotency_key": "configured-disabled-policy-contract-test",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "repository_hard_excluded")
        self.assertTrue(self._repository_disabled("AnikaWilliams/example"))

    def test_confirmation_rechecks_static_exclusion_before_commit(self) -> None:
        creation = self.client.post(
            "/api/plugins/pr-autopilot/repository-intents",
            json={
                "repository": "AnikaWilliams/example",
                "disabled": False,
                "idempotency_key": "final-static-exclusion-recheck-test",
            },
        )
        self.assertEqual(creation.status_code, 201)
        intent_id = creation.json()["intent"]["id"]

        mutated = False
        real_connect = self.plugin_api.sqlite3.connect
        config_path = self.root / "config.json"

        class ConnectionProxy:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self._connection = connection

            @property
            def row_factory(self):
                return self._connection.row_factory

            @row_factory.setter
            def row_factory(self, value) -> None:
                self._connection.row_factory = value

            def execute(self, statement: str, *parameters):
                nonlocal mutated
                if statement.startswith("INSERT INTO repository_settings") and not mutated:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    config["excluded_repositories"].append("AnikaWilliams/example")
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                    mutated = True
                return self._connection.execute(statement, *parameters)

            def __getattr__(self, name: str):
                return getattr(self._connection, name)

        with patch.object(
            self.plugin_api.sqlite3,
            "connect",
            side_effect=lambda *args, **kwargs: ConnectionProxy(real_connect(*args, **kwargs)),
        ):
            confirmation = self.client.post(
                f"/api/plugins/pr-autopilot/repository-intents/{intent_id}/confirm"
            )

        self.assertTrue(mutated)
        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "repository_hard_excluded")
        self.assertTrue(self._repository_disabled("AnikaWilliams/example"))

    def test_confirmation_restores_state_when_static_exclusion_changes_during_commit(self) -> None:
        creation = self.client.post(
            "/api/plugins/pr-autopilot/repository-intents",
            json={
                "repository": "AnikaWilliams/example",
                "disabled": False,
                "idempotency_key": "post-commit-static-exclusion-recheck-test",
            },
        )
        self.assertEqual(creation.status_code, 201)
        intent_id = creation.json()["intent"]["id"]

        mutated = False
        real_connect = self.plugin_api.sqlite3.connect
        config_path = self.root / "config.json"

        class ConnectionProxy:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self._connection = connection

            @property
            def row_factory(self):
                return self._connection.row_factory

            @row_factory.setter
            def row_factory(self, value) -> None:
                self._connection.row_factory = value

            def commit(self) -> None:
                nonlocal mutated
                if not mutated:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    config["excluded_repositories"].append("AnikaWilliams/example")
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                    mutated = True
                self._connection.commit()

            def __getattr__(self, name: str):
                return getattr(self._connection, name)

        with patch.object(
            self.plugin_api.sqlite3,
            "connect",
            side_effect=lambda *args, **kwargs: ConnectionProxy(real_connect(*args, **kwargs)),
        ):
            confirmation = self.client.post(
                f"/api/plugins/pr-autopilot/repository-intents/{intent_id}/confirm"
            )

        self.assertTrue(mutated)
        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(confirmation.json()["detail"]["code"], "repository_hard_excluded")
        self.assertTrue(self._repository_disabled("AnikaWilliams/example"))

    def test_overview_fails_closed_when_state_is_missing(self) -> None:
        self.state_path.unlink()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "state_unavailable")
        self.assertNotIn(str(self.root), json.dumps(response.json()))

    def test_overview_fails_closed_for_duplicate_casefolded_repository_rows(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                ("anikawilliams/example", 0),
            )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "state_incompatible")

    def test_overview_accepts_controller_normalized_setting_casing(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "DELETE FROM repository_settings WHERE lower(repository) = lower(?)",
                ("AnikaWilliams/blocked",),
            )
            connection.execute(
                "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                ("anikawilliams/blocked", 1),
            )
            connection.execute(
                """
                INSERT INTO pr_state (repository, number, updated_at, head_sha)
                VALUES (?, ?, ?, ?)
                """,
                ("AnikaWilliams/blocked", 991, "2026-08-20T10:00:00Z", HEAD),
            )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(
            "AnikaWilliams/blocked",
            {entry["repository"] for entry in payload["pull_requests"]},
        )
        controls = {entry["repository"]: entry for entry in payload["repositories"]}
        self.assertEqual(
            controls["AnikaWilliams/blocked"],
            {
                "repository": "AnikaWilliams/blocked",
                "disabled": True,
                "hard_excluded": True,
                "mutable": False,
            },
        )

    def test_overview_accepts_a_baseline_row_without_a_head(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                """
                INSERT INTO pr_state (repository, number, updated_at, head_sha)
                VALUES (?, ?, ?, ?)
                """,
                ("AnikaWilliams/baseline", 992, "2026-08-20T10:00:00Z", ""),
            )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 200)
        baseline = next(
            entry
            for entry in response.json()["pull_requests"]
            if entry["repository"] == "AnikaWilliams/baseline"
        )
        self.assertEqual(baseline["head_short"], "Pending")

    def test_overview_preserves_an_archived_pipeline_status(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                """
                UPDATE pr_state SET active_task_status = ?
                WHERE repository = ? AND number = ?
                """,
                ("archived", "AnikaWilliams/example", 18),
            )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 200)
        pull_request = next(
            entry
            for entry in response.json()["pull_requests"]
            if entry["repository"] == "AnikaWilliams/example" and entry["number"] == 18
        )
        self.assertEqual(pull_request["observed_stage_status"], "archived")

    def test_overview_rejects_duplicate_casefolded_pr_state_rows(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                """
                INSERT INTO pr_state (repository, number, updated_at, head_sha)
                VALUES (?, ?, ?, ?)
                """,
                ("anikawilliams/example", 991, "2026-08-20T10:00:00Z", HEAD),
            )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "state_incompatible")

    def test_overview_bounds_pull_requests_and_repository_controls(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            for index in range(101):
                connection.execute(
                    """
                    INSERT INTO pr_state (repository, number, updated_at, head_sha)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        f"AnikaWilliams/pull-request-{index}",
                        1_000 + index,
                        "2026-08-20T10:00:00Z",
                        HEAD,
                    ),
                )
            for index in range(201):
                connection.execute(
                    "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                    (f"AnikaWilliams/repository-setting-{index}", 0),
                )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertLessEqual(len(payload["pull_requests"]), 100)
        self.assertLessEqual(len(payload["repositories"]), 200)
        self.assertEqual(
            payload["truncated"],
            {"pull_requests": True, "repositories": True, "merge_history": False},
        )

    def test_overview_rejects_duplicate_repository_rows_beyond_the_output_cap(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            for index in range(201):
                connection.execute(
                    "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                    (f"AnikaWilliams/a-bound-{index:03d}", 0),
                )
            connection.execute(
                "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                ("AnikaWilliams/z-duplicate", 0),
            )
            connection.execute(
                "INSERT INTO repository_settings (repository, disabled) VALUES (?, ?)",
                ("anikawilliams/z-duplicate", 1),
            )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "state_incompatible")

    def test_repository_controls_include_known_repositories_beyond_the_pr_card_cap(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            for index in range(101):
                connection.execute(
                    """
                    INSERT INTO pr_state (repository, number, updated_at, head_sha)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        f"AnikaWilliams/pull-request-{index:03d}",
                        2_000 + index,
                        "2026-08-20T10:00:00Z",
                        HEAD,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get("/api/plugins/pr-autopilot/overview")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        repositories = {entry["repository"] for entry in payload["repositories"]}
        self.assertIn("AnikaWilliams/pull-request-100", repositories)
        self.assertFalse(payload["truncated"]["repositories"])

    def _repository_disabled(self, repository: str) -> bool:
        connection = sqlite3.connect(self.state_path)
        try:
            row = connection.execute(
                "SELECT disabled FROM repository_settings WHERE lower(repository) = lower(?)",
                (repository,),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        return bool(row[0])

    def _repository_row_count(self, repository: str) -> int:
        connection = sqlite3.connect(self.state_path)
        try:
            row = connection.execute(
                "SELECT count(*) FROM repository_settings WHERE lower(repository) = lower(?)",
                (repository,),
            ).fetchone()
        finally:
            connection.close()
        return int(row[0])


class ControllerRootTests(unittest.TestCase):
    def test_controller_root_uses_the_active_profile_plugin_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile_home = Path(temporary_directory) / "hermes" / "profiles" / "prfix"
            expected = profile_home / "plugin-data" / "pr-autopilot"
            expected.mkdir(parents=True)
            config_module = ModuleType("hermes_cli.config")
            config_module.load_config_readonly = lambda: {
                "plugins": {"entries": {"pr-autopilot": {"settings": {}}}}
            }
            plugin_api = load_plugin_api()

            with (
                patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}),
                patch.dict(sys.modules, {"hermes_cli.config": config_module}),
            ):
                resolved = plugin_api._controller_root()

            self.assertEqual(resolved, expected.resolve())

    def test_controller_root_rejects_data_inside_the_plugin_checkout(self) -> None:
        """A configured state root must never use files in the plugin checkout."""
        plugin_api = load_plugin_api()
        config_module = ModuleType("hermes_cli.config")
        config_module.load_config_readonly = lambda: {
            "plugins": {
                "entries": {
                    "pr-autopilot": {
                        "settings": {
                            "data_root": str(Path(plugin_api.__file__).resolve().parent)
                        }
                    }
                }
            }
        }

        with patch.dict(sys.modules, {"hermes_cli.config": config_module}):
            with self.assertRaises(plugin_api._DashboardError) as raised:
                plugin_api._controller_root()

        self.assertEqual(raised.exception.code, "hermes_config_invalid")


if __name__ == "__main__":
    unittest.main()
