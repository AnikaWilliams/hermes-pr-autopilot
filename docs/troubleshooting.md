# Troubleshooting

## The dashboard is not in the sidebar

Confirm both plugin layers:

```powershell
hermes plugins show pr-autopilot
hermes plugins enable pr-autopilot
```

Restart the Hermes gateway or Desktop after backend changes. Then open
**Settings > Plugins** and enable the PR Autopilot Desktop surface.

## The dashboard says the runtime is inactive

The backend can be disabled, unloaded, or blocked by invalid configuration.
Run:

```powershell
hermes plugins doctor . --ci
hermes plugins show pr-autopilot
```

Inspect Hermes logs for `pr_autopilot.plugin`. Do not paste credentials or full
private paths into a public issue.

## The controller stays paused

This is the safe default for a new database. Open the dashboard, review
repository scope, select **Resume**, and confirm the dialog.

## Check now does not start a review

The first observation of a PR creates a baseline. Request another check after
the baseline. Also confirm that the PR is not a draft, fork, excluded
repository, disabled repository, or marked with a pause label.

Confirm that Codex code review is enabled for the repository and that the
active GitHub CLI account is the PR author.

## A repository switch does not change

The server creates a caller-bound confirmation intent before it writes the
setting. Complete the confirmation dialog promptly. If the intent expired,
refresh and try again.

A static `excluded_repositories` entry is not mutable in the dashboard.

## A worker is blocked

Do not retry a failed card blindly. Common safe stops include:

- the PR head changed
- the worktree was dirty
- a fork branch cannot be pushed
- the finding repeated without progress
- Codex quota was exhausted
- Codex reported that the repository environment was missing
- focused verification failed
- the worker exceeded a bounded runtime

Read the bounded stage output and exact-head evidence first. The public policy
keeps automatic retries at zero. The dashboard does not expose raw logs or task
identifiers. To read the bounded output stored for one pull request, run this
command locally. Replace the repository and pull-request number. If you use a
custom `HERMES_HOME` or data root, change `$State` to that profile's
`state\pr-autopilot.sqlite3` file.

```powershell
$Repository = "owner/repository"
$PullRequest = 123
$State = Join-Path $env:LOCALAPPDATA "hermes\plugin-data\pr-autopilot\state\pr-autopilot.sqlite3"

@'
import json
import sqlite3
import sys
from pathlib import Path

state, repository, number = sys.argv[1:]
database_uri = Path(state).resolve().as_uri() + "?mode=ro"
with sqlite3.connect(database_uri, uri=True) as connection:
    connection.execute("PRAGMA query_only = ON")
    state_rows = connection.execute(
        """
        SELECT head_sha, pipeline_json
        FROM pr_state
        WHERE lower(repository) = lower(?) AND number = ?
        """,
        (repository, int(number)),
    ).fetchall()
    if len(state_rows) != 1:
        raise SystemExit("No unique local PR state matched that pull request.")
    head_sha, pipeline_json = state_rows[0]
    try:
        pipeline = json.loads(pipeline_json)
    except (TypeError, json.JSONDecodeError):
        raise SystemExit("The pull request has no active local pipeline.")
    roles = ("analyze", "fix", "verify")
    if not isinstance(pipeline, dict) or set(pipeline) != set(roles):
        raise SystemExit("The active local pipeline is incomplete.")
    task_ids = tuple(pipeline[role] for role in roles)
    if not all(isinstance(task_id, str) and task_id for task_id in task_ids):
        raise SystemExit("The active local pipeline has invalid task identifiers.")
    rows = connection.execute(
        """
        SELECT role, expected_head, status, terminal_reason, output
        FROM desktop_worker_task
        WHERE lower(repository) = lower(?) AND number = ?
          AND identifier IN (?, ?, ?)
        ORDER BY CASE role WHEN 'analyze' THEN 1 WHEN 'fix' THEN 2 ELSE 3 END
        """,
        (repository, int(number), *task_ids),
    ).fetchall()

if len(rows) != 3:
    raise SystemExit("The active local pipeline stages are incomplete.")

print(f"Active pipeline state head: {head_sha}")
for role, expected_head, status, reason, output in rows:
    print(f"\n[{role}] expected_head={expected_head} status={status} reason={reason or '-'}")
    print(output or "(no captured output)")
'@ | python - $State $Repository $PullRequest
```

This read-only query follows the active task identifiers in `pr_state` and
prints their state head and expected heads. It does not mix retained tasks from
older pipelines into the result. The worker runtime
replaces exact values that it knows from controller environment variables with
names that contain `SECRET`, `TOKEN`, `PASSWORD`, or `API_KEY`. It cannot redact
a credential that is available only inside a worker profile or any other value
that the controller does not know. Treat the preview as sensitive. Keep it
local, inspect it for credentials and private data, and remove those values
before you share an excerpt.

If the pull request is still at the pipeline's exact head:

1. Pause PR Autopilot.
2. Fix the blocking cause and make the worktree clean.
3. Select **Retry pipeline** for the blocked pull request.
4. Confirm the exact-head retry.
5. Resume PR Autopilot.

The controller retries the same local Analyze, Fix, and Verify pipeline with a
new runtime attempt. It keeps the retry event in the local audit log. The
button is disabled while the controller is running, and confirmation fails if
any task changed or started after the request.

Do not use **Retry pipeline** after the pull request head changes. On the next
controller check, PR Autopilot retires the blocked old-head pipeline and asks
Codex to review the new exact head. It does not relaunch the old tasks.

## GitHub merge does not occur

The controller waits unless Codex is clean for the current head and GitHub
reports a clean merge state. Branch protection, required checks, conflicts, or
a changed head can block the merge. Do not bypass repository protection.

## Plugin Doctor reports an import error

Run Plugin Doctor from this repository with the same Hermes installation that
will load the plugin:

```powershell
hermes plugins doctor . --ci
```

The package includes its constrained worker runtime. It does not require a
private Hermes core patch.
