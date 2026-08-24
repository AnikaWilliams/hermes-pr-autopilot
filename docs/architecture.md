# Architecture and safety gates

## Components

`__init__.py` is the Hermes backend entry point. It creates private
profile-scoped configuration, registers the bundled constrained worker runtime,
and starts one supervised controller service.

`standalone_controller.py` owns the timed service and the Analyze, Fix, and
Verify pipeline. `pr_autopilot.py` reads GitHub state, applies deterministic
policy, prepares worktrees, requests Codex reviews, and performs guarded
merges. `pr_reconciler.py` classifies Codex evidence and stores restart-safe
state in SQLite.

`dashboard/plugin_api.py` exposes a bounded local API. `desktop/plugin.js`
registers the native route, sidebar item, command-palette item, and dashboard.

The worker skills define narrow roles:

- Analyze is read-only.
- Fix can make the smallest supported edit, run focused checks, commit, and
  push to the supplied same-repository branch.
- Verify is read-only and must validate the repaired head independently.

## Data flow

```text
GitHub CLI -> deterministic controller -> exact-head SQLite state
                     |
                     +-> Analyze -> Fix -> Verify worker processes
                     |
                     +-> fresh @codex review -> exact-head clean gate
                     |
                     +-> guarded squash merge -> local merge audit

Hermes Desktop <-> authenticated local dashboard API <-> SQLite controls
```

## Exact-head gates

The controller stores the PR head for each review request. Codex evidence is
accepted only when it comes from the configured bot identities and identifies
the current head. A bot reaction is authoritative only on the stored request
comment.

Each worker gets an immutable expected head and an isolated worktree. Analyze
and Verify must not change the head. Fix must start from the expected head and
push the exact repaired head. A stale or force-pushed pipeline fails closed.

The final merge uses:

```text
gh pr merge --squash --match-head-commit <exact-head>
```

GitHub must also report a clean merge state.

## Scope gates

The controller uses the active GitHub CLI user to discover new pull requests.
It ignores drafts, forks, static exclusions, local repository pauses, and PRs
with a configured pause label. It limits open PR discovery and review rounds.
Durable nonterminal pipelines remain in scope if the active account changes.
Pause the controller and review those pipelines before an account change.

The first observation is a baseline. It does not act on historical state.

## Dashboard write gates

The overview is bounded and omits prompts, findings, task IDs, local paths, and
credentials. Repository changes use short-lived, caller-bound confirmation
intents. Pause and repository operations use confirmation dialogs.

The controller reloads durable repository switches on every cycle. A Desktop
restart is not required after a confirmed repository switch.

## Startup behavior

The backend and Desktop surface are opt-in. New controller state starts paused.
An existing database keeps its current pause value across updates.

Plugin Doctor can import and register the package without starting the
controller or using the network.
