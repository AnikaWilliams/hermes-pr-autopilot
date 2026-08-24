# Contributing

Thank you for improving Hermes PR Autopilot.

## Before a change

Open an issue for a large behavior or policy change. Keep pull requests narrow.
Preserve these invariants:

- deterministic polling and merge decisions
- exact-head review, worker, push, and merge gates
- same-repository branches only
- no automatic retry of a failed worker stage, except guarded same-stage
  recovery for a classified transient provider outage after an allowlisted,
  authenticated endpoint probe
- profile-scoped runtime state
- no secrets or machine-specific paths in tracked files
- explicit confirmation for dashboard writes
- paused first start

Use native Hermes Desktop plugin components and keep stage status separate from
actions.

## Development setup

```powershell
git clone https://github.com/AnikaWilliams/hermes-pr-autopilot.git
Set-Location hermes-pr-autopilot
python -m pip install --requirement requirements-dev.txt
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
```

Use Windows for the full supported test path. Hermes Plugin Doctor must pass
before release.

## Pull request checklist

- Add a focused regression for behavior changes.
- Run `scripts/test.ps1`.
- Run `python scripts/secret_scan.py`.
- State whether Plugin Doctor ran.
- Describe changes to GitHub writes, worker authority, or repository scope.
- Do not include runtime databases, logs, worktrees, local configuration,
  credentials, or screenshots with private data.
- Do not weaken exact-head or confirmation checks to make a test pass.

Use clear technical English and concise commit messages.
