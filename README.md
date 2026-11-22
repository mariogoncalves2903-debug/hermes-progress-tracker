# Hermes Progress Tracker

A [Hermes Agent](https://hermes-agent.nousresearch.com/) desktop plugin that shows real progress bars for agent work: goal-level tasks, permission/question blocks, findings, token/context usage, and work-vs-wait timing.

## Features

- Per-session task bar: create with goal-level steps, advance, skip, mark failed/fixed, add steps later.
- Blocks timeline: permission requests, human-input questions, and tool rows.
- Findings log with capped history.
- Token/context tiles from model calls.
- Work vs waited-on-you timing. Wait only counts open permission/question blocks — tool calls are work, not waiting.
- Session-scoped: each conversation has its own bar; session end dismisses only that session's bar.
- Desktop pane plus an above-the-composer band.

## Requirements

Hermes >= 0.22 (see `requires_hermes` in `plugin.yaml`).

## Install

1. Copy this directory to `$HERMES_HOME/plugins/progress-tracker`.
2. Run `hermes plugins enable progress-tracker`.
3. Restart the gateway.
4. In Capabilities -> Plugins, enable **Progress Tracker**, then run "Reload desktop plugins".

## Tool

The agent drives the bar through the `progress_tracker` tool:

| op | args | effect |
|---|---|---|
| `title` | `title`, `tasks` | create a bar with goal-level steps |
| `tasks` | `tasks` | replace the task list |
| `add` | `tasks` | append steps |
| `next` | - | advance to the next step |
| `active` | `task` | set a named step active |
| `skip` | `task` | skip a named step |
| `failed` | `text` | mark the active step failed |
| `fixed` | - | clear failure state |
| `found` | `text` | log a finding |
| `dismiss` | - | dismiss the bar |

Use goal-level steps from the user's request, never raw tool names.

## Configuration

See `plugin.yaml`: `enabled_tools`, `enabled_findings`, `max_findings`, `max_turns_retained`.

## Tests

```bash
cd tests
python -m unittest discover -s . -v
```

## License

MIT — see [LICENSE](LICENSE).
