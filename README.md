# Hermes Telegram Notify

`telegram-notify` is a standalone, Hermes-native lifecycle plugin that sends
small, bounded Telegram messages when Hermes starts a turn, finishes a turn, or
needs an approval decision. It is modeled on the operational safeguards in
[`codex-telegram-notify`](https://github.com/NousResearch/codex-telegram-notify),
but uses Hermes' public Python plugin API rather than Codex hook scripts.

## What it does

The plugin registers five observer hooks:

| Hermes hook | Notification |
|---|---|
| `pre_llm_call` | One **🚀 Hermes · Started** message per turn |
| `post_llm_call` | **✅ Hermes · Completed** plus the final assistant response |
| `on_session_end` | **⏸️ Interrupted** or **❌ Failed** fallback notification |
| `pre_approval_request` | **⚠️ Hermes · Approval required** for user-facing prompts; smart assessments are suppressed |
| `post_approval_response` | Optional decision/timeout message with emoji |

Start and completion notifications are suppressed only when Hermes reports
`platform="subagent"`. Parent-session metadata alone is not used to classify
an agent, and missing or other platform values retain the existing behavior.

Telegram failures, malformed payloads, missing credentials, and file-state
problems are fail-open: they are logged or ignored and never abort the Hermes
agent turn.

## Relationship to codex-telegram-notify

This project deliberately reuses the useful design principles of the Codex
plugin: the standard-library HTTPS client, atomic mode-0600 secret/state files,
exclusive state locking, bounded JSONL logs, safe truncation/redaction, chat-ID
discovery, and a test-message utility. It does **not** import or modify the
Codex plugin and has no runtime dependency on it.

## Compatibility and execution modes

- Targeted Hermes API: plugin API v1, tested against Hermes `0.20.2`.
- The lifecycle hooks are process-local and therefore work in the Hermes CLI,
  messaging Gateway, and Desktop sessions that use the Gateway backend.
- Desktop is not a separate hook API: its backend emits the same Gateway/agent
  lifecycle events. The plugin does not require Electron-specific code.
- Hermes fires `pre_llm_call` once in the per-turn prologue, before the
  tool-calling loop; it is not a per-HTTP-request hook. The state store still
  deduplicates by `(session_id, turn_id)` as a defensive guarantee.
- `post_llm_call` fires once after a successful, non-interrupted turn and
  carries `assistant_response`; this is the source of the Telegram final answer.
- `on_session_end` fires at the end of every `run_conversation()` turn and on
  relevant CLI/TUI interruption paths. It is not a final persistent-session
  teardown event. A long-lived Gateway conversation can consequently produce
  one completion notification per user turn. Shared state claims prevent it
  from duplicating the successful `post_llm_call` notification.
- Hermes approval hooks are observers. `pre_approval_request` can notify but
  cannot approve, deny, or block the request; this is the closest safe native
  equivalent to Codex `PermissionRequest`.
- Hermes' canonical `on_session_end` payload contains status flags and an exit
  reason, not final model text. Successful text comes only from the documented
  `post_llm_call.assistant_response` field. It is bounded and token-redacted;
  chain-of-thought, hidden reasoning, full prompts, and full context are never
  sent.

## Installation

From this repository, use the official Hermes local-plugin installer. The
installer expects a Git source, so commit the project first:

```bash
cd /home/max/Projects/hermes-telegram-notify
git init
git add .
git commit -m 'Initial Hermes Telegram notification plugin'
hermes plugins install file:///home/max/Projects/hermes-telegram-notify --enable
hermes plugins doctor telegram-notify
hermes plugins list
```

The installed copy is normally:

```text
$HERMES_HOME/plugins/telegram-notify/
```

For the default profile this is usually `~/.hermes/plugins/telegram-notify/`.
Do not develop directly in that directory.

Enable or disable it without uninstalling:

```bash
hermes plugins enable telegram-notify
hermes plugins disable telegram-notify
```

## Telegram bot setup

1. In Telegram, open `@BotFather` and create a bot with `/newbot`.
2. Keep the token private. Do not put it in Git, shell history, issue reports,
   or Telegram messages.
3. Send the bot a message (or add it to the target group and send a message).
4. Configure the token through a hidden prompt and save the chat ID:

```bash
hermes telegram-notify configure --chat-id '<CHAT_ID>'
```

The token is stored in the active profile's plugin directory as
`credentials.json` with mode `0600`; it is never stored in `config.json`.
For non-interactive automation, avoid command-line token arguments and use a
short-lived environment variable:

```bash
export MY_TELEGRAM_TOKEN='...'
hermes telegram-notify configure --token-env MY_TELEGRAM_TOKEN --chat-id '<CHAT_ID>'
unset MY_TELEGRAM_TOKEN
```

`TELEGRAM_BOT_TOKEN` is also accepted as a process-environment override. The
plugin reads only that key from the active Hermes `.env` when the process
environment does not already provide it; it does not copy the value elsewhere.

## Discovering a chat ID

The bot can only discover chats represented in recent `getUpdates` results.
After sending the bot a message, run:

```bash
hermes telegram-notify discover-chat-id
```

The command prints only chat IDs and a bounded human label. It never prints the
bot token or raw Telegram update payloads. Set the selected ID with:

```bash
hermes telegram-notify configure --chat-id '<CHAT_ID>'
```

The following environment variables override the stored chat ID (first
non-empty value wins):

```text
HERMES_TELEGRAM_CHAT_ID
TELEGRAM_CHAT_ID
CODEX_TELEGRAM_CHAT_ID       # compatibility with codex-telegram-notify
TELEGRAM_HOME_CHANNEL        # Hermes home-channel compatibility
```

## Configuration

Show current status without exposing secrets:

```bash
hermes telegram-notify status
```

The plugin writes non-secret settings to `$HERMES_HOME/telegram-notify/config.json`.
The supported keys are:

```json
{
  "enabled": true,
  "telegram_chat_id": "<CHAT_ID>",
  "notify_on_start": true,
  "notify_on_completion": true,
  "notify_on_approval": true,
  "notify_on_approval_response": false,
  "approval_debounce_seconds": 60,
  "max_message_chars": 3900,
  "final_response_max_chars": 3200,
  "telegram_timeout_seconds": 4,
  "state_retention_days": 7,
  "include_model": true,
  "include_cwd": true,
  "include_session": true
}
```

Use the management command for the common configuration path. To disable all
notifications without uninstalling:

```bash
HERMES_TELEGRAM_NOTIFY_ENABLED=0 hermes telegram-notify status
```

For a persistent setting, edit `config.json` with a local editor or use the
plugin's `configure --disable` command. Environment overrides are useful for
service-level toggles and are not written by the plugin.

Event-level environment overrides:

```text
HERMES_TELEGRAM_NOTIFY_START=0
HERMES_TELEGRAM_NOTIFY_COMPLETION=0
HERMES_TELEGRAM_NOTIFY_APPROVAL=0
HERMES_TELEGRAM_NOTIFY_APPROVAL_RESPONSE=1
HERMES_TELEGRAM_APPROVAL_DEBOUNCE=60
HERMES_TELEGRAM_TIMEOUT=4
```

## Test notification and status

Send one bounded test message:

```bash
hermes telegram-notify test
```

A custom short message is allowed, but the command truncates it:

```bash
hermes telegram-notify test --message 'Hermes notification test'
```

Inspect recent JSONL logs:

```bash
hermes telegram-notify logs --tail 50
```

The log is stored at `$HERMES_HOME/telegram-notify/telegram-notify.log`,
rotated at a bounded size, and written without token-bearing fields. Approval
diagnostics record a bounded surface category, outcome, timestamp, and opaque
correlation ID; commands, descriptions, and raw session/tool-call IDs are never
recorded. Hermes' own logs can also be viewed with:

```bash
hermes logs --level INFO
```

## Message safety and formatting

Messages identify Hermes explicitly and stay compact. Start messages contain
the project identity and the human-readable Hermes session title. When the
hook does not carry a title, the plugin reads it best-effort from the active
profile's `state.db`; a genuinely untitled session is shown as `New session`
rather than exposing a raw technical ID. Successful completion messages contain
the project identity, session title, and the bounded final assistant response.
Approval messages contain a sanitized command and optional reason.
Interrupted/failed messages contain the project, session title, and a short
reason. Turn IDs, model names, and elapsed timing are intentionally omitted
from Telegram messages. In a named profile, the profile name (for example,
`zorro`) is used as the project identity when Hermes does not provide an
explicit working directory. The plugin never sends full prompts,
conversation history, environment dumps, model reasoning, or unrestricted
command output. Common secret-bearing command arguments (`token`,
`password`, `secret`, `api-key`, authorization) and token-shaped values in the
final response are redacted.

## State, locking, and duplicate suppression

The plugin keeps profile-scoped state in `$HERMES_HOME/telegram-notify/state.json`
and uses an adjacent `state.lock` with an exclusive `flock` on Linux. Updates
are written through a same-directory temporary file, `fsync`, and `replace`.
Old records are pruned after seven days by default.

`pre_llm_call` is a once-per-Hermes-turn hook, unlike a naïve mapping to every
LLM HTTP request. The plugin claims a start record before sending and keys it by
`session_id + turn_id`; repeated callbacks for the same turn are suppressed.
If an exit-only payload lacks `turn_id`, it uses a stable task/message
fingerprint. Completion is claimed independently, so duplicate finalization
callbacks cannot send duplicate completion messages. User-facing approval
requests are fingerprinted and debounced (60 seconds by default); smart-mode
assessment hooks do not send or claim the debounce key.

## Failure behavior and limitations

- Missing token or chat ID means notifications are skipped safely.
- Telegram HTTP, timeout, DNS, malformed-response, and API failures are
  non-fatal and logged as sanitized error classes.
- Approval hooks cannot change Hermes' decision. `post_approval_response` is
  disabled by default because approval responses can be noisy; enable it with
  `notify_on_approval_response: true`.
- Interrupted and failed `on_session_end` notifications have no final response
  body because Hermes does not expose one on that lifecycle contract. Successful
  responses use `post_llm_call` instead.
- Chat discovery requires a recent update and may not work until the bot has
  received a message in the target chat.
- This plugin is outbound-only; it does not turn Telegram into a Hermes input
  channel.

## Uninstall

Disable first if desired, then use Hermes' plugin manager:

```bash
hermes plugins disable telegram-notify
hermes plugins uninstall telegram-notify
```

The plugin's profile-scoped runtime data can then be removed separately after
reviewing the status output:

```bash
rm -rf "$HERMES_HOME/telegram-notify"
```

If the plugin was installed from this source repository, removing the source
checkout is independent of the installed copy.

## Development

No third-party runtime dependency is required beyond the Hermes runtime.
Unit tests use mocked HTTPS and temporary `HERMES_HOME` directories:

```bash
cd /home/max/Projects/hermes-telegram-notify
python -m pytest -q
```

The repository's original Codex plugin is intentionally not imported or
modified.
