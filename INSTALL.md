# Installation

This procedure installs Hermes PR Autopilot without storing credentials in the
repository. The plugin is Windows-only in version 0.3.0.

## 1. Prepare dependencies

Install and verify these commands:

```powershell
hermes --version
git --version
gh --version
gh auth status --hostname github.com --active
```

Do not use `gh auth token` during setup. The plugin uses the existing GitHub CLI
credential without copying it into plugin data.

Enable Codex code review for each target repository before you start the
controller. Follow the official
[Codex GitHub integration guide](https://developers.openai.com/codex/integrations/github/).
The controller uses the documented `@codex review` request.

## 2. Select a Hermes source profile

The three worker profiles need the model and provider settings of one existing
Hermes profile. Choose that profile explicitly.

If you use a custom Hermes base data directory, set it before you list or show
profiles. Keep the value for all later Hermes commands in this PowerShell
session:

```powershell
$env:HERMES_HOME = (Resolve-Path -LiteralPath '<base-path>').Path
```

```powershell
hermes profile list
hermes profile show <source-profile>
```

The installer will create:

- `prtriage` for read-only finding analysis
- `prfix` for the bounded repair and push
- `prverify` for independent verification

The installer uses `hermes profile create --clone-from`. This local Hermes
operation can copy the selected profile's provider configuration and `.env`
file into each worker profile. It does not copy those files into this Git
repository.

If a worker profile already exists, the installer stops. Inspect the profile,
then pass `-ReuseExistingWorkerProfiles` only when you want to use it. Reuse
also requires each installed worker `SKILL.md` to have the same SHA-256 digest
as its bundled file. Validation rejects a missing or changed worker skill.
A normal approved reuse run can install a missing bundled skill after an
interrupted installation. A changed installed skill remains fatal.

## 3. Validate without changes

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 `
  -SourceProfile <source-profile> `
  -ValidateOnly `
  -ReuseExistingWorkerProfiles
```

Remove `-ReuseExistingWorkerProfiles` only when none of the three worker
profiles exists. Keep the flag when one or more approved worker profiles
already exist. The installer validates each existing worker skill and creates
the missing profiles.

If you use a custom Hermes base data directory, include it during validation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 `
  -SourceProfile <source-profile> `
  -HermesHome <base-path> `
  -ValidateOnly `
  -ReuseExistingWorkerProfiles
```

Use the base directory, not one named profile directory. Use this same path
during installation and for all later Hermes commands.

The validation checks the required commands, GitHub CLI authentication, the
requested repository, source profile, and bundled skills. It does not install
or modify anything.

If validation reports that an approved existing profile has no `SKILL.md`,
inspect the profile and confirm that the missing file is from an interrupted
installation. Ask the operator to approve installation of the bundled skill.
Then run the normal installation in Section 4 with
`-ReuseExistingWorkerProfiles`. The installer uses its guarded write path to
install the missing skill. Do not use this recovery path to replace a changed
installed skill.

## 4. Install in the disabled state

For an update or reinstall, pause the existing controller in its dashboard
before you run the installer. Select **Refresh** and confirm that the paused
state is durable. If you cannot verify the pause, stop. Do not install, enable,
or restart the updated backend.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 `
  -SourceProfile <source-profile> `
  -ReuseExistingWorkerProfiles
```

Keep `-ReuseExistingWorkerProfiles` when one or more existing worker profiles
were approved for reuse. This includes an approved profile with a missing
skill that the operator approved for repair. Remove the flag only when no
worker profile exists.

This command creates the missing worker profiles, installs their scoped skills,
and installs the plugin backend in the disabled state.

You can use `-HermesHome <path>` for a custom base Hermes data directory. Use
the base directory, not one named profile directory.

The installer restores the previous process environment when it returns. If
you used `-HermesHome`, set the same base path for every later Hermes command
in this PowerShell session:

```powershell
$env:HERMES_HOME = (Resolve-Path -LiteralPath '<base-path>').Path
```

Do this before the enable and verification commands below. A new shell must
set the value again unless the operator configured it persistently.

## 5. Review the safety boundary

Before activation, confirm all of these statements:

- The active `gh` account is the intended GitHub identity.
- Codex code review is enabled for every repository that is in scope.
- The account can push to each same-repository PR branch and can squash-merge.
- You will review `pause_labels` in the generated private `config.json` before
  you resume the controller.
- You understand that a repository is active unless it is excluded, disabled
  in the dashboard, or has a pause label.

A fresh installation with no existing controller database starts paused. An
update or reinstall retains its prior controller state. Do not activate it
unless you verified the existing controller was paused before installation.

## 6. Enable the backend and dashboard

```powershell
hermes plugins enable pr-autopilot
```

Restart the Hermes gateway or Hermes Desktop. Do not terminate an active
Desktop session without the operator's permission.

The first paused backend load creates the private `config.json`. Before you
resume the controller, review and edit `pause_labels` there if the defaults do
not match your policy. Add sensitive or unsupported repositories to
`excluded_repositories` in the same private file. Restart the backend after a
configuration edit. Do not edit the public template with private policy.

In Hermes Desktop:

1. Open **Settings > Plugins**.
2. Enable **PR Autopilot**.
3. Open **PR Autopilot** in the sidebar.
4. Confirm that the runtime is paused.
5. Review the repository switches.
6. Keep the controller paused until Section 7 establishes approval for
   automatic repair pushes. Section 7 must also establish merge approval or a
   repository-side rule that prevents an automatic merge.

After an approved resume, the first observation of an existing pull request
creates a baseline and takes no historical action. Use **Check now** after the
baseline if you want another immediate cycle.

## 7. Verify

```powershell
hermes plugins show pr-autopilot
hermes profile show prtriage
hermes profile show prfix
hermes profile show prverify
```

Then use a disposable same-repository pull request:

1. While the controller is paused, add a small, deliberate defect on a test
   branch.
2. Open a pull request with that branch in the same repository.
3. Approve automatic repair pushes for this disposable pull request. If you do
   not approve them, stop and keep the controller paused.
4. Separately approve the automatic squash-merge attempt, or establish a
   repository-side rule that prevents the merge. If neither condition is true,
   stop and keep the controller paused.
5. Confirm the repository scope in the dashboard, select **Resume**, and
   confirm the dialog.
6. Confirm that PR Autopilot observes the exact head.
7. Confirm that Codex reacts to the stored `@codex review` comment.
8. Observe Analyze, Fix, and Verify in the dashboard.
9. Confirm the pushed repair and a new Codex review on the new exact head.
10. Confirm a merge only after the clean current-head verdict.

Keep the test repository disposable until this full loop passes in your
environment.

## AI-assisted installation

Use [AGENT_INSTALL.md](AGENT_INSTALL.md) as the agent's runbook. A copy-ready
request is in [prompts/agent-assisted-install.md](prompts/agent-assisted-install.md).
