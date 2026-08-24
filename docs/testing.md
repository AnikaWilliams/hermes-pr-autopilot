# Testing

## Full local release check

```powershell
python -m pip install --requirement requirements-dev.txt
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
```

The script uses a unique path under ignored `test-output` for each run. It does
not use the active Hermes profile for Plugin Doctor.

The check includes:

- Python controller, reconciler, worker, dashboard API, and package tests
- native Desktop plugin contract tests with Node.js
- Python bytecode compilation
- tracked-file credential and private-path scan
- Hermes Plugin Doctor when `hermes` is on `PATH`

If Plugin Doctor is skipped, the result is not a complete Hermes package check.

## Focused commands

```powershell
$env:HERMES_TEST_SHIMS = '1'
python -m pytest tests -q --basetemp test-output/pytest-manual
Remove-Item Env:HERMES_TEST_SHIMS
node --test tests/desktop_plugin_contract.test.mjs
python scripts/secret_scan.py
hermes plugins doctor . --ci
```

`HERMES_TEST_SHIMS` enables minimal imports only for the isolated Python test
process. Remove it before Plugin Doctor so Doctor validates the real Hermes
runtime.

Use a new `--basetemp` value after a worker-process test on Windows. A child
process can keep the previous temporary directory open briefly.

## End-to-end acceptance

Run end-to-end validation only in an operator-approved disposable repository.
Use a same-repository branch because fork pushing is deliberately blocked.

Evidence must include:

1. initial PR head SHA
2. stored Codex review request and bot response
3. Analyze, Fix, and Verify status
4. focused regression result
5. pushed repaired head SHA
6. new clean Codex verdict for that repaired head
7. exact-head merge result, when merge testing is approved
8. sanitized logs and dashboard screenshots

Do not publish a screenshot that shows unrelated sessions, private repository
names, local user paths, tokens, or private configuration.
