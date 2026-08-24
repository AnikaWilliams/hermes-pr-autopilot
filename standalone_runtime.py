"""Local, non-live Analyze-only PR Autopilot runtime boundary.

This module deliberately neither imports nor invokes Hermes, Kanban, GitHub, or
process-launch APIs. Its caller supplies an already-observed exact PR head.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

from pr_reconciler import StateStore


class RuntimeBlocked(RuntimeError):
    """The durable standalone state is not safe to admit as new work."""


@dataclass(frozen=True)
class ObservedPullRequest:
    repository: str
    number: int
    head_sha: str


@dataclass(frozen=True)
class AnalyzeAttempt:
    identifier: str
    repository: str
    number: int
    expected_head: str
    role: str
    status: str
    terminal_reason: str | None = None


class AnalyzeOnlyRuntime:
    """Create and observe one local Analyze attempt under a controller lease."""

    def __init__(
        self,
        store: StateStore,
        *,
        owner_id: str,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.clock = clock or (lambda: int(time.time()))
        self._lease_generation: int | None = None

    def acquire_controller_lease(self, *, now: int | None = None, ttl_seconds: int) -> int:
        lease = self.store.acquire_runtime_lease(
            name="standalone-controller",
            owner_id=self.owner_id,
            now=self.clock() if now is None else now,
            ttl_seconds=ttl_seconds,
            renew_generation=self._lease_generation,
        )
        if lease is None:
            raise RuntimeBlocked("standalone controller lease is owned by another runtime")
        self._lease_generation = lease.generation
        return lease.generation

    def release_controller_lease(self) -> bool:
        """Release this runtime's fenced lease without touching any attempt."""

        if self._lease_generation is None:
            return False
        released = self.store.release_runtime_lease(
            name="standalone-controller",
            owner_id=self.owner_id,
            generation=self._lease_generation,
        )
        if released:
            self._lease_generation = None
        return released

    def admit(self, observed: ObservedPullRequest) -> AnalyzeAttempt:
        if self._lease_generation is None:
            raise RuntimeBlocked("standalone controller lease is required before admitting work")
        try:
            record = self.store.create_or_load_analyze_attempt(
                repository=observed.repository,
                number=observed.number,
                expected_head=observed.head_sha,
                owner_id=self.owner_id,
                lease_generation=self._lease_generation,
                now=self.clock(),
            )
        except ValueError as error:
            raise RuntimeBlocked(str(error)) from error
        return AnalyzeAttempt(**record)

    def observe(self, attempt_id: str) -> AnalyzeAttempt:
        record = self.store.load_analyze_attempt(attempt_id)
        if record is None:
            raise KeyError(f"standalone Analyze attempt not found: {attempt_id}")
        return AnalyzeAttempt(**record)

    def interrupt(self, attempt_id: str, *, reason: str) -> AnalyzeAttempt:
        """Persist shutdown/unknown outcomes as blocked, never as success.

        A stale backend callback cannot terminalize an attempt after a new
        runtime has acquired the fenced controller lease.
        """
        if self._lease_generation is None:
            raise RuntimeBlocked("standalone controller lease is required before interruption")
        try:
            record = self.store.block_analyze_attempt(
                attempt_id,
                reason=reason,
                owner_id=self.owner_id,
                lease_generation=self._lease_generation,
                now=self.clock(),
            )
        except ValueError as error:
            raise RuntimeBlocked(str(error)) from error
        return AnalyzeAttempt(**record)
