# Configuration reference

On the first backend load, the plugin copies `config.example.json` to a private
profile-scoped `config.json`. Edit the private copy. Do not edit the public
template with machine-specific values.

## Worker settings

| Field | Purpose |
| --- | --- |
| `analyzer_profile` | Hermes profile for read-only analysis. |
| `analyzer_skill` | Skill installed for the Analyze worker. |
| `worker_profile` | Hermes profile for repair work. |
| `worker_skill` | Skill installed for the Fix worker. |
| `verifier_profile` | Hermes profile for independent verification. |
| `verifier_skill` | Skill installed for the Verify worker. |
| `analysis_task_timeout` | Analyze duration from 1 second through 1 day, in `s`, `m`, `h`, or `d`. |
| `task_timeout` | Fix duration from 1 second through 1 day, in `s`, `m`, `h`, or `d`. |
| `fix_progress_extension` | Required compatibility duration in `s`, `m`, `h`, or `d`; the Desktop standalone runtime does not apply it. |
| `fix_runtime_cap` | Required compatibility duration in `s`, `m`, `h`, or `d`; the Desktop standalone runtime does not apply it. |
| `verification_task_timeout` | Verify duration from 1 second through 1 day, in `s`, `m`, `h`, or `d`. |
| `analysis_max_turns` | Maximum Hermes turns for Analyze. |
| `fix_max_turns` | Maximum Hermes turns for Fix. |
| `verification_max_turns` | Maximum Hermes turns for Verify. |

The setup script installs the default profile and skill names. If you change a
name, update both the profile and the private configuration.

`task_timeout` is the fixed Fix worker limit in this standalone Desktop plugin.
A verified mid-stage push does not extend that process. The two Fix extension
fields remain required for controller configuration compatibility, but changing
them does not change the installed worker runtime.

Active worker timeouts must resolve to 1 through 86,400 seconds. For example,
`24h` and `1d` are valid; `48h` and `2d` prevent plugin registration.

## Policy settings

| Field | Purpose |
| --- | --- |
| `policy_revision` | Forces a fresh current-head review after a change only when no worker pipeline is active. |
| `pause_labels` | PR labels that block new pipelines, scheduled stage launches, and merges after a controller eligibility check observes them; they do not stop a worker that is already running. |
| `excluded_repositories` | Static, non-dashboard exclusions in `owner/repo` form. |
| `disabled_repositories` | Static repository disables reapplied on every controller cycle. The dashboard cannot override them. |
| `max_open_prs` | Maximum authored open PRs used for new PR discovery. Active durable pipelines are also reconciled beyond this limit. |
| `max_review_rounds` | Maximum review requests before a bounded stop. |
| `task_max_retries` | Must remain `0`; failed stages require diagnosis. |

Repository names are case-insensitive. Static exclusions and static disables
cannot be reversed in the dashboard. Use the dashboard for repositories that
are not listed in either configuration field.

The controller applies current pull-request label eligibility before it
observes an existing Analyze, Fix, and Verify pipeline. A pause label added
after pipeline creation does not cancel a worker that is already running. It
becomes effective when a subsequent controller eligibility check observes it.
After that observation, it prevents the next scheduled stage from starting
until the label is removed. A scheduled stage can start if the label is added
after the controller reads its current pull-request snapshot but before that
stage starts. Pause the controller when you need an immediate local stop.
Diagnose a blocked stage before you use the guarded dashboard retry.

If `policy_revision` changes while a pipeline is active, the controller records
the new revision and lets that pipeline continue. It does not invalidate the
pipeline's existing review evidence. Pause the controller and resolve the
active pipeline before a policy change when you require new-policy evidence.

## Recovery settings

Recovery-provider probing is disabled in the public template:

```json
{
  "recovery_endpoint_hosts": [],
  "recovery_model_id": "disabled",
  "recovery_api_key_env": "PR_AUTOPILOT_RECOVERY_API_KEY",
  "recovery_api_key_file": ".secrets/recovery-api-key"
}
```

Keep the public defaults unless you want automatic recovery from a recognized
provider outage. To enable recovery:

1. Add only the exact approved provider host names to
   `recovery_endpoint_hosts`. Do not add schemes, paths, wildcards, user
   information, queries, or fragments.
2. Set `recovery_model_id` to the model identifier used only by the readiness
   probe. This setting does not select or constrain the worker profile's model.
3. Put the provider key in the environment variable named by
   `recovery_api_key_env`. If that variable is empty, the controller reads the
   private file named by `recovery_api_key_file`.

The standalone runtime marks a failed worker as transient only when its bounded
output contains a recognized timeout, connection, rate-limit, or provider 5xx
marker and an `Endpoint: https://...` value on an allow-listed host. Other
blocked workers remain terminal.

Before retry, the controller sends an authenticated `GET` request to the
endpoint's `/models` path. It rejects redirects and requires a `200` JSON
response that lists `recovery_model_id`. A successful probe shows only that
the listed model is ready at that endpoint. The worker profile can use a
different model. The controller then validates the managed worktree, restores
the exact expected head when safe, and reschedules only the same blocked stage.
A dirty, unmanaged, missing, or advanced worktree remains blocked for manual
review.

Never put a key in JSON, Markdown, a command argument, a screenshot, or Git
history. Keep the key in the named environment variable or ignored private
file. Use a provider key with the smallest practical scope.

## Plugin data root

Hermes supplies a profile-scoped data directory. Advanced users can set an
absolute `plugins.entries.pr-autopilot.settings.data_root` value in Hermes
configuration. Relative paths and paths inside the PR Autopilot plugin
checkout are rejected. Keep the data root outside all source repositories.
