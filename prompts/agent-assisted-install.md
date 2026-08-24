# Agent-assisted installation prompt

Copy the text below into an AI coding agent that can access the Windows machine
where Hermes is installed. Replace only values that you already know. Leave the
source profile placeholder in place when you want the agent to ask you.

```text
Install Hermes PR Autopilot from this public repository:
https://github.com/AnikaWilliams/hermes-pr-autopilot

Use AGENT_INSTALL.md in that repository as the installation runbook. Follow the
operator's request and local AGENTS.md files first. Use ASD-STE100 simplified
technical English in status reports.

Hermes source profile: <ASK ME BEFORE ANY WRITE>
Hermes base data directory: <USE THE INSTALLED DEFAULT, OR ASK IF AMBIGUOUS>
Existing prtriage, prfix, or prverify profiles: <DO NOT REUSE OR OVERWRITE
WITHOUT MY APPROVAL>

Start with read-only inspection and package tests. Do not read or print secret
values. Use `gh auth status --hostname github.com --active`; never use
`gh auth token`. Show me the exact write plan before you create profiles or
install the plugin.

If installation uses a custom Hermes base data directory, pass that exact
directory to the install script during read-only validation and installation.
Set it as `HERMES_HOME` before every profile list or profile inspection. Keep
it for every later Hermes verification and activation command. Do not inspect,
validate against, or fall back to the default Hermes home.

Install the plugin backend in the disabled state first. Ask for separate
approval before you enable the backend, restart Hermes, resume the controller,
create a test pull request, push a commit, or allow a merge.

If this is an update or reinstall, use the existing dashboard to pause the
controller and refresh its state before installation. Existing controller
state is retained. If you cannot verify the durable pause, stop. Do not
install, enable, or restart the updated backend.

If a worker profile already exists, do not rely on `hermes profile show` alone.
Use the installer's reuse validation to require an exact SHA-256 match between
each installed worker `SKILL.md` and its bundled file. Stop on a missing or
changed skill. Do not replace it without my approval.

For end-to-end validation, use only a disposable same-repository test pull
request that I approve. Verify the exact-head Codex review loop with sanitized
logs and screenshots. Do not expose tokens, private configuration, unrelated
repository names, session names, user paths, or personal data.

Before you resume that test, tell me that Fix can create and push a repair
commit. Obtain my separate approval for automatic repair pushes. Tell me that
a clean exact-head verdict causes an automatic squash-merge attempt. Obtain my
separate approval for that merge, or verify a repository-side rule that
prevents it. If the required push approval or merge protection is missing,
keep the controller paused and report that the full loop was not run.

At the end, report every change, the selected profile, test results, Plugin
Doctor result, backend/dashboard state, controller pause state, and any manual
step that remains. Do not claim success for a skipped or failed check.
```
