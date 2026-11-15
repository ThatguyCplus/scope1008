# Security

## Reporting a problem

Please use GitHub's private vulnerability reporting (the **Security** tab, then **Report a
vulnerability**) rather than a public issue.

## What the app does and does not do

- It talks only to a local USB device (VID 0783, PID 5725 by default). It opens no network
  connections and runs no external commands.
- It writes files only where you choose in a save dialog, plus its own settings through Qt's
  `QSettings` store.
- Saved settings are validated field by field on load; anything malformed falls back to defaults.
- Every acquisition setting is range-checked in `AcqConfig.validate()` before it is sent to the
  scope, independent of the vendored driver's `assert` statements (which `python -O` removes).
- CSV exports neutralise cells that start with `=`, `+`, `-` or `@` so a channel label cannot
  become a spreadsheet formula.

## Dependencies

Runtime dependencies are pinned with SHA-256 hashes in `requirements.lock` and installed with
`pip --require-hashes --only-binary=:all:`, so a tampered or substituted package fails to install
and no source build scripts run. CI runs `pip-audit` on every push and weekly, and Dependabot
proposes updates. To refresh the locks after changing `pyproject.toml`:

```
pip install pip-tools
pip-compile --generate-hashes --strip-extras -o requirements.lock pyproject.toml
pip-compile --generate-hashes --strip-extras --extra dev -o requirements-dev.lock pyproject.toml
```
