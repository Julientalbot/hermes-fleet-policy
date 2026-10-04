---
name: recurring-tasks
description: Watch jobs that wake the model only on new items.
version: 0.2.0
author: Julien Talbot
license: MIT
metadata:
  hermes:
    tags: [cron, watch, monitor, cost, recurring]
---

# Recurring tasks that stay cheap

## When to Use

The user asks "tell me when…", "let me know if…", "watch…", "keep an eye on…", or for any job
that checks something on a schedule. Builds on the stock cron gates (`script` +
`{"wakeAgent": false}`, `monitor_url`, `no_agent`, `[SILENT]`); see the catalog skill
`hermes-cron` for lifecycle and troubleshooting.

**Rule: observing is free, judging is not.** A cheap script observes every tick; the model is
woken only when there is something new to judge. Never create a full agent run on a fixed
interval to watch something (e.g. "every 15 min: check my inbox and tell me if…"): most runs
find nothing and still cost a full turn.

## Pick the shape

| The user wants | Shape |
| --- | --- |
| News from a tool (mail, calendar, CRM, sheet, tickets…) | `watch` (below) |
| A web page changed | stock `monitor_url` on the job — no script |
| A report at fixed times (briefing, weekly review) | plain agent job, no gate: it must run anyway |
| A fixed alert, no judgement (disk full, URL down) | `no_agent: true` + script |

## `watch`: new items of one read tool

1. **Find a READ tool** that lists the items, and the **field that identifies one item** (`id`,
   `messageId`, `eventId`, row number…). Never a tool that sends, writes or deletes. Call it once
   yourself to see the response and pick the field.
2. **Write the config** `$HERMES_HOME/watches/<name>.json`:

   ```json
   {"server_url": "<MCP endpoint of that tool, streamable HTTP>",
    "tool": "<tool name>", "arguments": {},
    "id_field": "<id field>", "show_fields": ["<2-3 short fields>"], "max_new": 20,
    "state": "<name>.state.json"}
   ```

   `server_url` is the `url` of the MCP server in `config.yaml` (`mcp_servers.<name>.url`). If it
   needs a header, put `"headers": {"Authorization": "Bearer ${ENV_VAR}"}` — never a literal secret.
3. **Write a one-line script** `$HERMES_HOME/scripts/watch-<name>.sh` (cron scripts take no
   arguments, so the config path is in the line):

   ```sh
   exec <hermes venv python> <fleet-policy plugin dir> watch --config <HERMES_HOME>/watches/<name>.json "$@"
   ```

   Use absolute paths; `chmod +x` is not needed (`.sh` runs through bash). `hermes fleet-policy watch --config …` also works where `hermes` is on PATH.
4. **Dry-run it**: `bash $HERMES_HOME/scripts/watch-<name>.sh --dry-run` (prints what would be
   reported, writes nothing). Check that ids are found and the lines read
   well. A `warning: no '<field>' found` means a wrong field or arguments.
5. **Create the job** with `script="watch-<name>.sh"` and a prompt that judges and writes:
   "You receive new items from <source>, one per line. Keep only those that <user's criterion>.
   For each, write one short line for the user. If none qualifies, reply exactly [SILENT]."
6. **Run once for real** (`hermes cron run <id>` or the `run` action): the first run only records
   the current items as the baseline and stays silent. Tell the user that.

What happens then: no new id → the script prints `{"wakeAgent": false}` and the model is not
started. New ids → one line per item reaches the prompt. A failing source exits non-zero: the job
reports the error instead of going quiet, and nothing is marked as seen.

### Example: Composio (one MCP endpoint for many apps)

Composio exposes apps through `COMPOSIO_MULTI_EXECUTE_TOOL`; the app tool is the `tool_slug`:

```json
{"server_url": "http://composio-relay:8787/mcp",
 "tool": "COMPOSIO_MULTI_EXECUTE_TOOL",
 "arguments": {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS",
                          "arguments": {"query": "newer_than:2d", "max_results": 25,
                                        "include_payload": false}}],
               "thought": "watch", "sync_response_to_workbench": false},
 "id_field": "messageId", "show_fields": ["sender", "subject"],
 "state": "inbox.state.json"}
```

Prefer a Composio session restricted to read-only tools (`readOnlyHint`) for watches.

### Example: any other MCP server

```json
{"server_url": "http://127.0.0.1:9000/mcp", "tool": "list_issues",
 "arguments": {"state": "open", "labels": ["urgent"]},
 "id_field": "number", "show_fields": ["title", "author.login"],
 "state": "urgent-issues.state.json"}
```

## Check the cost afterwards

`hermes fleet-policy crons --cost` shows, per job over 7 days: runs, share of empty runs, $/day
and the gate. A job flagged `frequent_without_gate` or `mostly_empty` should be converted to a
gated shape; propose it to the user rather than changing it silently.

## Limits

- New ids only: an edited item with the same id is not news, a deleted one is ignored.
- The model judges only what `show_fields` shows: pick fields that let it decide.
- Interval = latency. Every 15 min is fine for most people; real time needs a webhook trigger.
