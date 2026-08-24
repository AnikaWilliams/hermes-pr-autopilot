"""Strictly local contracts for the standalone Analyze-only runtime."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pr_reconciler import PRState, StateStore
from standalone_runtime import AnalyzeOnlyRuntime, ObservedPullRequest, RuntimeBlocked


HEAD = "a" * 40


class StandaloneAnalyzeRuntimeTests(unittest.TestCase):
    def test_creates_and_observes_one_exact_head_analyze_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            runtime = AnalyzeOnlyRuntime(store, owner_id="desktop-test", clock=lambda: 100)
            runtime.acquire_controller_lease(now=100, ttl_seconds=60)

            attempt = runtime.admit(
                ObservedPullRequest(
                    repository="AnikaWilliams/example",
                    number=42,
                    head_sha=HEAD,
                )
            )

            observed = runtime.observe(attempt.identifier)

            self.assertEqual(observed, attempt)
            self.assertEqual(attempt.repository, "AnikaWilliams/example")
            self.assertEqual(attempt.number, 42)
            self.assertEqual(attempt.expected_head, HEAD)
            self.assertEqual(attempt.role, "analyze")
            self.assertEqual(attempt.status, "ready")
            self.assertEqual(store.standalone_schema_tables(), {
                "pipeline",
                "pipeline_stage",
                "runtime_lease",
                "worker_attempt",
            })

    def test_expired_controller_lease_cannot_admit_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            runtime = AnalyzeOnlyRuntime(store, owner_id="desktop-test", clock=lambda: 161)
            runtime.acquire_controller_lease(now=100, ttl_seconds=60)

            with self.assertRaises(RuntimeBlocked):
                runtime.admit(ObservedPullRequest("AnikaWilliams/example", 42, HEAD))

    def test_unexpired_controller_lease_rejects_a_competing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            first = AnalyzeOnlyRuntime(store, owner_id="desktop-one")
            first.acquire_controller_lease(now=100, ttl_seconds=60)

            competing = AnalyzeOnlyRuntime(store, owner_id="desktop-two")

            with self.assertRaises(RuntimeBlocked):
                competing.acquire_controller_lease(now=101, ttl_seconds=60)

    def test_released_controller_lease_is_fenced_before_new_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            first = AnalyzeOnlyRuntime(store, owner_id="desktop-one")
            first.acquire_controller_lease(now=100, ttl_seconds=60)

            first.release_controller_lease()

            second = AnalyzeOnlyRuntime(store, owner_id="desktop-two")
            generation = second.acquire_controller_lease(now=101, ttl_seconds=60)
            self.assertEqual(generation, 2)
            with self.assertRaises(RuntimeBlocked):
                first.admit(ObservedPullRequest("AnikaWilliams/example", 42, HEAD))

    def test_legacy_nonterminal_pipeline_is_quarantined_and_never_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "state.sqlite3"
            store = StateStore(path)
            store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at="2026-08-21T00:00:00Z",
                    head_sha=HEAD,
                    active_task_id="legacy-kanban-task",
                    active_task_status="running",
                    pipeline_json='{"analyze":"legacy-kanban-task"}',
                )
            )

            migrated = StateStore(path)
            quarantined = migrated.pipeline_for(
                repository="AnikaWilliams/example", number=42, expected_head=HEAD
            )

            self.assertEqual(quarantined.status, "blocked_migration")
            self.assertEqual(quarantined.block_reason, "legacy_nonterminal_quarantined")
            self.assertEqual(quarantined.legacy_task_id, "legacy-kanban-task")
            runtime = AnalyzeOnlyRuntime(migrated, owner_id="desktop-test", clock=lambda: 100)
            runtime.acquire_controller_lease(now=100, ttl_seconds=60)
            with self.assertRaises(RuntimeBlocked):
                runtime.admit(ObservedPullRequest("AnikaWilliams/example", 42, HEAD))

    def test_legacy_pipeline_head_is_canonicalized_before_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "state.sqlite3"
            store = StateStore(path)
            store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at="2026-08-21T00:00:00Z",
                    head_sha=HEAD.upper(),
                    active_task_id="legacy-kanban-task",
                    active_task_status="running",
                    pipeline_json='{"analyze":"legacy-kanban-task"}',
                )
            )

            migrated = StateStore(path)
            quarantined = migrated.pipeline_for(
                repository="AnikaWilliams/example", number=42, expected_head=HEAD
            )

            self.assertIsNotNone(quarantined)
            self.assertEqual(quarantined.status, "blocked_migration")
            runtime = AnalyzeOnlyRuntime(migrated, owner_id="desktop-test", clock=lambda: 100)
            runtime.acquire_controller_lease(now=100, ttl_seconds=60)
            with self.assertRaises(RuntimeBlocked):
                runtime.admit(ObservedPullRequest("AnikaWilliams/example", 42, HEAD))

    def test_stale_runtime_cannot_interrupt_after_lease_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            first = AnalyzeOnlyRuntime(store, owner_id="desktop-one", clock=lambda: 100)
            first.acquire_controller_lease(now=100, ttl_seconds=60)
            attempt = first.admit(ObservedPullRequest("AnikaWilliams/example", 42, HEAD))
            first.release_controller_lease()

            replacement = AnalyzeOnlyRuntime(store, owner_id="desktop-two", clock=lambda: 101)
            replacement.acquire_controller_lease(now=101, ttl_seconds=60)

            with self.assertRaises(RuntimeBlocked):
                first.interrupt(attempt.identifier, reason="stale_shutdown")
            self.assertEqual(replacement.observe(attempt.identifier).status, "ready")

    def test_expired_lease_reused_by_same_owner_fences_old_runtime_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            stale = AnalyzeOnlyRuntime(store, owner_id="desktop-shared", clock=lambda: 100)
            self.assertEqual(stale.acquire_controller_lease(now=100, ttl_seconds=60), 1)
            attempt = stale.admit(ObservedPullRequest("AnikaWilliams/example", 42, HEAD))

            replacement = AnalyzeOnlyRuntime(store, owner_id="desktop-shared", clock=lambda: 161)
            self.assertEqual(replacement.acquire_controller_lease(now=161, ttl_seconds=60), 2)

            with self.assertRaises(RuntimeBlocked):
                stale.interrupt(attempt.identifier, reason="stale_shutdown")
            self.assertEqual(replacement.observe(attempt.identifier).status, "ready")

    def test_default_clock_recovers_an_expired_controller_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            stale = store.acquire_runtime_lease(
                name="standalone-controller",
                owner_id="stale-desktop",
                now=100,
                ttl_seconds=60,
            )
            self.assertIsNotNone(stale)

            runtime = AnalyzeOnlyRuntime(store, owner_id="replacement-desktop")

            self.assertEqual(runtime.acquire_controller_lease(ttl_seconds=60), 2)

    def test_same_commit_head_case_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            runtime = AnalyzeOnlyRuntime(store, owner_id="desktop-test", clock=lambda: 100)
            runtime.acquire_controller_lease(now=100, ttl_seconds=60)

            first = runtime.admit(ObservedPullRequest("AnikaWilliams/example", 42, HEAD))
            second = runtime.admit(ObservedPullRequest("AnikaWilliams/example", 42, HEAD.upper()))

            self.assertEqual(first.identifier, second.identifier)
            self.assertEqual(second.expected_head, HEAD)

    def test_same_owner_cannot_acquire_an_unexpired_lease_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            first = AnalyzeOnlyRuntime(store, owner_id="desktop-shared", clock=lambda: 100)
            first.acquire_controller_lease(now=100, ttl_seconds=60)
            replacement = AnalyzeOnlyRuntime(store, owner_id="desktop-shared", clock=lambda: 101)

            with self.assertRaises(RuntimeBlocked):
                replacement.acquire_controller_lease(now=101, ttl_seconds=60)

    def test_interruption_blocks_analyze_attempt_without_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state.sqlite3")
            runtime = AnalyzeOnlyRuntime(store, owner_id="desktop-test", clock=lambda: 100)
            runtime.acquire_controller_lease(now=100, ttl_seconds=60)
            attempt = runtime.admit(
                ObservedPullRequest("AnikaWilliams/example", 42, HEAD)
            )

            interrupted = runtime.interrupt(attempt.identifier, reason="backend_shutdown")

            self.assertEqual(interrupted.status, "blocked")
            self.assertEqual(interrupted.terminal_reason, "backend_shutdown")
            observed = runtime.observe(attempt.identifier)
            self.assertEqual(observed.status, "blocked")
            self.assertEqual(observed.terminal_reason, "backend_shutdown")


if __name__ == "__main__":
    unittest.main()
