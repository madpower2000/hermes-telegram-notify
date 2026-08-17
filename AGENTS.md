# Hermes Telegram Notify Development Guide

This repository is a standalone Hermes Agent native plugin. Keep the project
independently installable: the plugin root must retain `plugin.yaml` and
`__init__.py`, and the runtime implementation lives in
`hermes_telegram_notify/`.

## Safety rules

- Never print, commit, test-fixture, or send the Telegram bot token.
- Do not read arbitrary Hermes configuration into Telegram messages.
- Hook callbacks are observers and must remain fail-open: Telegram/network
  failures must never abort the Hermes turn.
- Use `get_hermes_home()` for persistent profile-scoped files.
- Use `scripts/run_tests.sh` from the Hermes checkout for integration tests;
  local unit tests may be run with the Hermes virtualenv and pytest.

## Verification

Run the unit suite from the repository root:

```bash
python -m pytest -q
```

After edits, load the installed plugin through `hermes plugins doctor` and
exercise hooks through Hermes' `PluginManager.invoke_hook` rather than only
calling formatter functions.
