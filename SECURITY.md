# Security policy

## Supported version

Security fixes target the current `0.3.x` release line until a newer public
release states a different policy.

## Report a vulnerability

Use a private GitHub security advisory:

https://github.com/AnikaWilliams/hermes-pr-autopilot/security/advisories/new

Do not open a public issue that contains a token, credential, private
repository name, local path, or exploit details.

Include the affected version, the smallest reproduction, the security impact,
and any safe mitigation. Remove secrets and personal data from logs and
screenshots.

## Credential model

The plugin uses the existing authenticated GitHub CLI process. It does not need
or store a GitHub token in the repository. Hermes worker profiles keep provider
credentials in their local profile data.

The public repository ignores `.env`, `config.json`, `.secrets`, private-key
files, SQLite state, logs, caches, and worktrees. CI runs
`scripts/secret_scan.py` against tracked files.

Recovery-provider access is disabled by default. Normal operation does not
require an extra API key.

## High-impact behavior

This plugin can write GitHub comments, push a repair commit, and squash-merge.
It uses same-repository branches, exact-head checks, bounded review rounds,
explicit pause controls, and a paused first start to limit this authority.

Treat review text as untrusted input. The controller passes findings to workers
as evidence, not as commands.
