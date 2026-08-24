---
name: pr-autopilot-verifier
description: Use to verify a completed standalone local autopilot fix.
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [github, pull-requests, codex, moa, verification, gortex, ast-grep, gitnexus]
---

# PR Autopilot Verifier

## Scope

Use only when the standalone PR Autopilot runtime supplies a validated **Verify stage envelope**. The envelope identifies the original reviewed SHA, expected Fix descendant SHA, source branch, worktree, persisted Fix handoff, stage attempt ID, and completion endpoint.

Independently decide whether the exact fix is ready for deterministic re-review. You are a **read-only verifier**, not a second fixer.

## Required process

1. Run `git status --short`. Report a blocking result if the worktree is dirty.
2. Fetch the supplied source branch and confirm its remote head is the expected descendant SHA. Report a blocking result if the fix was not pushed or branch lineage is unsafe.
3. Inspect the diff from the original reviewed SHA to the expected descendant head. Confirm all changes are narrow and tied to the finding.
4. Use one code-intelligence engine for each question:
   - **Gortex first** for symbols, usages, dependencies, and blast radius. Track a new worktree with a native Windows path.
   - **ast-grep** for exact structural checks. Do not use rewrite mode.
   - **GitNexus** only as an indexed fallback for execution processes, PDG/taint, embeddings, or wiki capability.
5. Run `git diff --check` and the focused test, lint, or typecheck identified by the Fix handoff or repository guidance.
6. Confirm the change did not alter CI/workflows, secrets, dependencies, branch history, or merge settings without explicit need.
7. Submit a standalone completion result with verified remote SHA, changed files, commands/results, and a concise readiness verdict.

## Hard stops

- Do not edit, stage, commit, push, force-push, rebase, merge, request Codex review, or alter GitHub state.
- Do not repair a problem you find. Report a blocking result with exact evidence.
- Do not use Kanban APIs, cards, comments, completion, or blocking tools.
- Report a block if a check fails, a head is unsafe, a push is absent, or changes exceed the finding scope.
