# Hermes PR Autopilot

Hermes PR Autopilot is a Windows desktop plugin for a guarded pull-request
review loop. A deterministic local controller requests Codex reviews, sends
current-head findings through Analyze, Fix, and Verify workers, and performs a
squash merge only after a clean verdict for the exact current commit.

The native Hermes dashboard shows runtime health, the review queue, repository
switches, and local merge records. A new installation starts paused.

> [!WARNING]
> This plugin can post `@codex review`, push a repair commit to a same-repository
> pull-request branch, and squash-merge the pull request. Test it with a
> disposable repository before you use it with important work.

## Supported environment

- Windows 10 or Windows 11
- A current Hermes Agent and Hermes Desktop build with native plugin support
- Python 3.11 or later and Node.js 20 or later for development tests
- Git and an authenticated GitHub CLI (`gh`)
- Codex code review enabled for each repository that the controller can manage

Linux and macOS are not declared as supported platforms in version 0.3.0.

## What the controller does

1. It discovers open, non-draft pull requests authored by the active `gh` user.
   Durable nonterminal pipelines remain in scope after discovery, even if the
   active `gh` account changes.
2. It skips static exclusions, locally paused repositories, pause labels, and
   fork-based pull requests.
3. It records a baseline before it takes historical action.
4. It requests `@codex review` and stores the exact request comment and head.
5. It accepts only current-head evidence from the Codex bot. Bot reactions and
   issue comments use the stored review request as their authority boundary.
6. It starts Analyze, Fix, and Verify workers for a new finding set.
7. It requests a new review after a verified repair is pushed.
8. It squash-merges with GitHub's exact-head guard only when the Codex verdict
   is clean and GitHub reports a clean merge state.

Pause the controller before you change the active `gh` account. Review or
remove durable pipelines before you resume it. The new account controls GitHub
access for those existing pipelines; authorship filtering applies only when the
controller discovers a new pull request.
9. It records the merge only after GitHub reports that the exact head is
   merged. A merge-queue submission stays pending until that confirmation.

The polling and merge decisions do not use a language model. Local Hermes model
invocation occurs only in the bounded worker stages. Each Codex review request
in steps 4 and 7 also starts model processing in the external Codex GitHub
integration.

## Install

Read [INSTALL.md](INSTALL.md) before you enable the backend. The installer asks
for a source Hermes profile. It does not guess a profile. It always installs the
backend in the disabled state. Enable it later with the separate Hermes command
only after you complete the safety checks in the installation guide.

```powershell
git clone https://github.com/AnikaWilliams/hermes-pr-autopilot.git
Set-Location hermes-pr-autopilot
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 `
  -SourceProfile <source-profile>
```

For an AI-assisted setup, copy the prompt from
[prompts/agent-assisted-install.md](prompts/agent-assisted-install.md). The
agent must follow [AGENT_INSTALL.md](AGENT_INSTALL.md).

## Operate the dashboard

After setup:

1. Enable the backend with `hermes plugins enable pr-autopilot`.
2. Restart the Hermes gateway or Hermes Desktop.
3. Open **Settings > Plugins** and enable the **PR Autopilot** dashboard.
4. Open **PR Autopilot** in the sidebar.
5. Review the repository list and policy. Use **Resume** only when the scope is
   correct and you approve the controller's automatic merge authority under
   that policy.

The dashboard controls are deliberate:

- **Check now** requests a near-term deterministic controller cycle.
- **Pause** stops new controller checks. It does not cancel an active worker.
- **Refresh** reads the latest bounded state snapshot.
- **Retry pipeline** resets a blocked exact-head pipeline after you pause the
  controller, correct the cause, and confirm the retry.
- A confirmed repository disable prevents a new pipeline, the next scheduled
  stage, and a merge. It does not cancel a worker that is already running.
- Stage labels are status indicators. Open the linked pull request to inspect
  the current head and review.

## Local data

By default, the controller stores its private configuration, controller SQLite
state, repository caches, and worktrees under the active Hermes home:

```text
<HERMES_HOME>\plugin-data\pr-autopilot
```

The usual default Windows path is
`%LOCALAPPDATA%\hermes\plugin-data\pr-autopilot`.

The Hermes plugin setting
`plugins.entries.pr-autopilot.settings.data_root` can replace this controller
root. The value must be an absolute path. If it is set, `config.json`,
`state\pr-autopilot.sqlite3`, `repos`, and `worktrees` are below that
path, not below `HERMES_HOME`.

The configured controller root must resolve outside the PR Autopilot plugin
checkout. The backend and dashboard reject an in-checkout override. Keep the
root outside every source repository so private policy, cached repositories,
worktrees, and runtime state cannot become tracked files.

The shared plugin worker runtime stores bounded worker output and audit events
in a separate profile-scoped database. The controller `data_root` setting
does not change this path. The database remains under the active Hermes home:

```text
<HERMES_HOME>\plugin-data\worker-runtime\worker-runtime.sqlite3
```

It applies limited redaction for known secret values from the controller
environment. This redaction does not guarantee that all captured output is
safe. For a named profile, custom `HERMES_HOME`, or controller `data_root`
override, include both resolved locations in a local data audit, backup, or
removal plan.

Controller messages use Hermes application logging; PR Autopilot does not
create a log file under its plugin-data directory. Hermes application logs are
under the active `HERMES_HOME\logs` directory. The usual default location is:

```text
%LOCALAPPDATA%\hermes\logs
```

Depending on the active Hermes surface, inspect `agent.log`, `errors.log`,
`gateway.log`, and `desktop.log`. Include this separate log directory in an
audit or removal plan.

`config.json` is created from [config.example.json](config.example.json) on the
first backend load. Keep the generated file outside source checkouts. Do not
put credentials in the public template or in a commit.

Recovery-provider access is disabled by default. It is not required for the
normal review loop.

## Development

```powershell
python -m pip install --requirement requirements-dev.txt
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
```

The release check runs the Python suite, the Desktop contract tests, Python
compilation, the tracked-file secret scan, and Hermes Plugin Doctor when the
`hermes` command is available.

## Documentation

- [Installation](INSTALL.md)
- [AI-agent installation rules](AGENT_INSTALL.md)
- [Architecture and safety gates](docs/architecture.md)
- [Configuration reference](docs/configuration.md)
- [Testing](docs/testing.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

Hermes references:

- [Native Desktop plugin SDK](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/desktop-plugin-sdk.md)
- [Hermes plugin management](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/plugins.md)

Codex reference:

- [Review GitHub pull requests with Codex](https://developers.openai.com/codex/integrations/github/)

## License

[MIT](LICENSE)
