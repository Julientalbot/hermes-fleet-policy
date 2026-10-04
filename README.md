# hermes-fleet-policy

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for administrators who pin
settings through Hermes' **managed scope** (`/etc/hermes/config.yaml` or `$HERMES_MANAGED_DIR`)
and keep their agents' recurring work cheap. Three parts:

- **policy observer** (`status`, `crons`): what is really sent to the provider, per role;
- **cron cost** (`crons --cost`): runs, empty runs and $/day per cron job, measured;
- **watch** (`watch`): wake the agent only when a read tool returns new items.

The managed scope tells you what *should* be sent. This plugin records what *is* sent: for each
role (main conversation, delegated sub-agents, cron runs, auxiliary tasks) it stores the model,
the reasoning effort and the service tier of the last real provider request, plus the tier the
provider says it served. It shows them next to the managed-scope state, the effective config and
the cron jobs:

```
hermes fleet-policy status [--json]
hermes fleet-policy crons  [--json] [--cost [--days N]]
hermes fleet-policy watch  --config <watch.json> [--dry-run]
```

The observer only observes. It never changes a request, never blocks one, and never stores message
content or credentials.

## How it works

| Surface (public plugin API) | Role recorded | What it sees |
|---|---|---|
| `pre_api_request` hook | `main`, `delegation`, `cron`, `unknown` | every main-loop provider attempt (the request body, which Hermes caps in size) |
| `pre_auxiliary_call` hook | `auxiliary:<task>` | every auxiliary attempt (compression, title generation, vision, web extract, MoA, ...) |
| `llm_execution` middleware | same roles as `pre_api_request` | the full request **and** the provider response, so it also records `response_service_tier` |
| `register_cli_command` | | `hermes fleet-policy ...` |
| `register_skill` | | `fleet-policy:fleet-policy-admin` (admin runbook), `fleet-policy:recurring-tasks` (how to set up a watch) |

The role comes from the agent `platform` Hermes passes to the hook: `subagent` maps to
`delegation`, `cron` to `cron`, and any other value (CLI `""`, `telegram`, `discord`, ...) to
`main`. If the key is missing, the role is `unknown`.

The middleware calls `next_call()` exactly once and returns its result unchanged; errors from the
provider pass through untouched. Every hook is fail-open. Turn the middleware off with
`plugins.entries.fleet-policy.settings.observe_execution: false` (hooks only).

Data lives in `$HERMES_HOME/fleet-policy/observed.json`, one entry per role. Writes are atomic
(temp file + `fsync` + `os.replace`) and serialized by a `flock`. Each value is a short scalar:
model id, effort, tier, timestamp.

## Install

Requires Hermes **0.21.5 or later** with plugin API 1. The plugin has no dependencies of its own.
Tested against Hermes 0.21.5 (image v2026.9.24) and `main` at 97cc4d0.

Hermes plugins are **opt-in**: installing one does nothing until its name is listed in
`plugins.enabled`.

### A. Plugin folder (no pip; recommended for containers)

`fleet-policy-plugin-<version>.tar.gz` holds the plugin folder alone (top directory
`fleet-policy/`, with `plugin.yaml` and `__init__.py`). Extract it into `$HERMES_HOME/plugins/`:

```bash
mkdir -p "$HERMES_HOME/plugins"
tar -xzf fleet-policy-plugin-0.2.0.tar.gz -C "$HERMES_HOME/plugins"   # -> plugins/fleet-policy/
hermes plugins enable fleet-policy        # appends to plugins.enabled in config.yaml
hermes plugins list --plain | grep fleet-policy
# enabled      user     0.2.0    fleet-policy
```

From a source checkout, copying the `fleet_policy/` directory to
`$HERMES_HOME/plugins/fleet-policy/` gives the same result.

### B. pip (entry point `hermes_agent.plugins`)

```bash
/path/to/hermes/venv/bin/python -m pip install ./hermes_fleet_policy-0.2.0-py3-none-any.whl
hermes plugins enable fleet-policy
hermes plugins list --plain | grep fleet-policy
# enabled      entrypoint 0.2.0    fleet-policy
```

### Enable from the managed scope (fleet-wide, users cannot turn it off)

```yaml
# /etc/hermes/config.yaml  (or $HERMES_MANAGED_DIR/config.yaml)
plugins:
  enabled: [fleet-policy]
```

Managed values replace user values leaf by leaf, and a list counts as one leaf. That means a
managed `plugins.enabled` **replaces** the user's whole list. List every plugin the fleet needs
there. `hermes plugins disable fleet-policy` then reports the key as managed and does nothing.

### Docker image (`nousresearch/hermes-agent`)

In the image, `HERMES_HOME=/opt/data` (a volume), the venv `/opt/hermes/.venv` is sealed
(root-owned, read-only), and services run as UID 10000. Use the folder install on the data
volume:

```bash
C=<container>
docker cp fleet-policy-plugin-0.2.0.tar.gz "$C":/tmp/fleet-policy-plugin.tar.gz
docker exec -u 10000 "$C" sh -c '
  set -e
  mkdir -p /opt/data/plugins
  rm -rf /opt/data/plugins/fleet-policy
  tar -xzf /tmp/fleet-policy-plugin.tar.gz -C /opt/data/plugins
  rm -f /tmp/fleet-policy-plugin.tar.gz'
docker exec -u 10000 "$C" /opt/hermes/.venv/bin/hermes plugins enable fleet-policy
docker exec -u 10000 "$C" /opt/hermes/.venv/bin/hermes plugins list --plain | grep fleet-policy
#   enabled      user     0.2.0    fleet-policy
docker restart "$C"    # the gateway and cron ticker load plugins at start
docker exec -u 10000 "$C" /opt/hermes/.venv/bin/hermes fleet-policy status --json
```

`-u 10000` keeps every file under `/opt/data` owned by the `hermes` user. If you run the commands
as root, the image's `/opt/hermes/bin/hermes` shim drops to that user for `hermes ...` calls, but
`tar` and `cp` do not. Fleet-wide activation without touching each `config.yaml`: put
`plugins.enabled: [fleet-policy, ...]` in the managed `config.yaml` instead of running
`plugins enable`.

## Output contract (`status --json`)

Keys and their order are fixed; downstream readers parse this exact shape.

```json
{
  "plugin": "fleet-policy",
  "version": "0.2.0",
  "managed": {"dir": "/etc/hermes", "keys": 3, "sha256": "3f14…", "error": null},
  "effective": {
    "model.default": "grok-4.6", "model.provider": "xai",
    "agent.reasoning_effort": "high", "agent.service_tier": "priority",
    "delegation.reasoning_effort": "", "delegation.model": "",
    "compression.threshold_tokens": 120000, "x_search.model": "grok-4.5"
  },
  "observed": {
    "main": {"model": "grok-4.6", "reasoning_effort": "high", "service_tier": "priority",
             "response_service_tier": "priority", "ts": "2026-10-04T14:41:05Z"},
    "auxiliary:title_generation": {"model": "grok-4.6", "reasoning_effort": "none",
             "service_tier": null, "response_service_tier": null, "ts": "2026-10-04T14:41:05Z"}
  },
  "crons": [
    {"id": "job-pinned", "name": "pinned report", "enabled": true, "provider": "openrouter",
     "model": "other-model", "reasoning_effort": "xhigh", "last_status": "error", "failure_streak": 3}
  ]
}
```

- `managed.dir`: the active managed directory, or `null` if there is none.
- `managed.keys`: the number of dotted keys Hermes reports as pinned.
- `managed.sha256`: digest of the raw managed `config.yaml`, or `null` if the file is absent.
- `managed.error`: set when the managed `config.yaml` exists but Hermes cannot apply it
  (unreadable, not UTF-8, empty, YAML parse error, not a mapping). Hermes itself only logs these
  cases and silently falls back to user settings; `keys` is then `0`.
- `effective`: values from `hermes_cli.config.load_config()` (defaults + `config.yaml` + managed
  overlay).
- `observed.<role>`: the roles are `main`, `delegation`, `cron`, `auxiliary:<task>` and
  `unknown`. `reasoning_effort` is what was on the wire (`reasoning.effort`, `reasoning_effort`,
  `output_config.effort`, `extra_body.reasoning.effort`), `"budget:<n>"` for Anthropic manual
  thinking, or `"none"` when reasoning is explicitly disabled. `service_tier` is the tier that was
  requested; `response_service_tier` is the tier the provider reports it served (non-streaming
  only, see Limitations).
- `crons`: every job from `cron.jobs.list_jobs(include_disabled=True)`; a paused job counts as
  `enabled: false`.

`crons --json` returns `{"plugin", "version", "crons", "audit"}`. The `audit` object lists jobs
with a pinned model or provider, a pinned effort, a failing state, or a model outside
`plugins.entries.fleet-policy.settings.allowed_cron_models`.

## Cron cost (`crons --cost`)

`hermes fleet-policy crons --cost --json` appends a `cost` object (the four keys above keep their
order). It is measured, read-only, from what Hermes stores in `$HERMES_HOME`:

- `cron/executions.db` (`executions`): one row per run attempt;
- `state.db` (`sessions`): each agent turn of a cron run is a session `cron_<job_id>_<timestamp>`,
  with compression/delegation children linked by `parent_session_id`. Cost is
  `actual_cost_usd`, else `estimated_cost_usd`, plus the auxiliary rows of
  `session_model_usage` (`task != ''`); the final assistant message tells whether the turn was
  silent.

```json
"cost": {
  "window_days": 7, "since": "2026-09-27T12:00:00Z", "until": "2026-10-04T12:00:00Z",
  "sources": {"executions": true, "state_db": true, "executions_coverage_from": null},
  "definition": "run = terminal execution in the window; empty run = ...",
  "jobs": [
    {"id": "a1b2c3", "name": "inbox check", "enabled": true, "schedule": "every 15m",
     "gate": null, "span_days": 7.0, "runs": 672, "runs_failed": 3, "agent_turns": 669,
     "empty_runs": 641, "empty_share": 0.954, "runs_per_day": 96.0, "tokens": 41000000,
     "cost_usd": 112.4, "cost_per_day_usd": 16.06, "cost_per_run_usd": 0.167,
     "flags": ["frequent_without_gate", "mostly_empty"]}
  ],
  "total_cost_per_day_usd": 16.06,
  "flagged": ["a1b2c3"]
}
```

Definitions:

- **run**: a terminal execution (completed, failed, unknown) claimed in the window. Agent turns
  without a ledger row (older Hermes, pruned ledger) count as runs too.
- **empty run**: a completed run that told the user nothing: the agent was not started (script
  gate `{"wakeAgent": false}`, empty script output, monitor `no_change`), or its final reply was
  empty or a silence marker (`[SILENT]`, `SILENT`, `NO_REPLY`…, matched by Hermes' own function).
  `null` for `no_agent` jobs (no LLM to measure) and when `state.db` is unreadable.
- **gate**: what can keep the agent asleep: `no_agent`, `monitor` (`monitor_script`/`monitor_url`),
  `script` (pre-run script; it gates only if it prints `{"wakeAgent": false}`), or `null` (the
  agent runs on every fire).
- **per day**: divided by the covered span: the window, shortened to the job's `created_at` and to
  `executions_coverage_from` when Hermes has pruned its ledger (it keeps the newest 1000 terminal
  rows across all jobs).
- **flags**: `frequent_without_gate` (no gate, more than 24 runs/day), `mostly_empty` (more than
  80 % empty runs, at least 5 runs).

## Watch: wake the agent only on new items

`hermes fleet-policy watch --config <file.json>` calls **one MCP tool** (streamable HTTP, with the
`mcp` SDK that Hermes ships) and compares the ids of the items it returns with the ids already
seen. It is the pre-run `script` of a cron job, so it plugs into Hermes' stock wake gate:

- nothing new: the last stdout line is `{"wakeAgent": false}` and the agent does not start (no
  LLM call, nothing delivered);
- new items: one short line per item, which Hermes injects into the agent's prompt as
  `## Script Output`. The job's prompt judges them and writes the message (or `[SILENT]`).

The code knows no service and holds no business rule: a watch is a tool, its arguments and the
name of the id field.

### Config

```json
{
  "server_url": "http://127.0.0.1:8787/mcp",
  "tool": "COMPOSIO_MULTI_EXECUTE_TOOL",
  "arguments": {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS",
                           "arguments": {"query": "newer_than:2d", "max_results": 25,
                                         "include_payload": false}}],
                "sync_response_to_workbench": false},
  "id_field": "messageId",
  "show_fields": ["sender", "subject"],
  "max_new": 20,
  "state": "inbox.state.json"
}
```

| Key | Meaning |
|---|---|
| `server_url` | streamable-HTTP MCP endpoint. Required unless an administrator pinned `plugins.entries.fleet-policy.settings.watch_server_url` (managed scope): then omit it — the pinned endpoint is used and any other is refused (exit 2) |
| `tool`, `arguments` | the tool call (required / default `{}`). Use a **read** tool. |
| `id_field` | field that identifies an item (required) |
| `show_fields` | fields printed for each new item; `a.b` follows a path, a bare name is searched in the item (default `[]`) |
| `max_new` | lines printed per run; the rest is summarized as a count but still marked as seen (default 20) |
| `state` | seen-ids file; relative to the config's folder (default `<config stem>.state.json`) |
| `max_seen` | bound on stored ids (default 5000) |
| `headers` | optional HTTP headers; `${VAR}` is expanded from the environment, so no secret sits in the file |
| `timeout_s` | call timeout (default 60) |

Composio wraps every app behind `COMPOSIO_MULTI_EXECUTE_TOOL` (the app tool is the
`tool_slug`); any other MCP server is called directly, e.g.
`{"tool": "list_issues", "arguments": {"state": "open"}, "id_field": "number"}`.

### Semantics

- **Ids, not bytes.** Items are the outermost JSON objects that carry a scalar `id_field`, at any
  depth of the result, including inside JSON documents embedded as strings (MCP relays often
  return the provider's answer that way). Hermes' `monitor_script` compares the exact output
  hash: a raw API answer changes on every call (timestamps, request ids) and a deleted item
  would wake the agent too. `watch` reports only ids never seen.
- **First run** (no state file): the current ids are recorded as the baseline and nothing is
  reported, otherwise the whole history would arrive as "new". Deleting the state file starts a
  new baseline.
- **Errors**: a transport failure, an MCP `isError` result, or `successful: false` / a non-empty
  `error` in the response envelope (outside the items) exits with code 1, a message on stderr,
  and an unchanged state. Hermes then runs the agent with a `## Script Error` block, so a broken
  watch is reported, never silent, and no item is marked as seen. Exit code 2 = invalid config.
  A corrupt state file is an error too (not a silent new baseline).
- **State**: written atomically (temp file, `fsync`, `os.replace`) and bounded by `max_seen`;
  past the bound the oldest ids that the tool no longer returns are dropped first.
- **`--dry-run`**: prints what would be reported (or, without state, the baseline items) and
  never writes the state.

### Wiring it to a cron job

Hermes runs cron scripts from `$HERMES_HOME/scripts/` without arguments (`.sh` through bash), so
each watch gets a one-line script:

```bash
# $HERMES_HOME/scripts/watch-inbox.sh  (Docker image: HERMES_HOME=/opt/data)
exec /opt/hermes/.venv/bin/python /opt/data/plugins/fleet-policy watch --config /opt/data/watches/inbox.json "$@"
```

```bash
bash /opt/data/scripts/watch-inbox.sh --dry-run          # check ids and lines
hermes cron create "every 15m" \
  "You receive new emails, one per line. Keep those that need the user's attention and write one
   short line for each. If none does, reply exactly [SILENT]." \
  --script watch-inbox.sh --name inbox-watch
```

`"$@"` only serves manual runs such as `--dry-run`; cron passes nothing. With
`python <plugin-dir>` the command works even where Hermes does not wire plugin CLI commands
(`plugins.isolation: host`). The bundled skill `fleet-policy:recurring-tasks` tells the agent to
build watches this way instead of a full agent run on a fixed interval.

### Limits of watch

- New ids only: an item edited in place keeps its id and is not reported; deletions are ignored.
- The agent judges only what `show_fields` prints.
- One tool call per run, no pagination: the window of the call (e.g. `newer_than:2d`,
  `max_results`) must be wider than the interval between runs, else items can be missed.
- Latency is the cron interval. Event-driven delivery (webhooks) is out of scope.
- The interpreter must import `mcp` (the Hermes venv does; the Docker image ships it).
- `watch` does not enforce read-only: point it at a read tool, and prefer an MCP session that is
  restricted to read-only tools (Composio: `readOnlyHint`).

## Configuration

```yaml
plugins:
  enabled: [fleet-policy]
  entries:
    fleet-policy:
      settings:
        observe_execution: true          # llm_execution pass-through (served tier). false = hooks only
        allowed_cron_models: [grok-4.6]  # optional allow-list for `crons` audit
```

## Limitations

- **Not observed.** These calls skip both the main loop and the auxiliary client, so no plugin
  hook fires for them:
  - `x_search`: a direct `requests.post` to xAI `/responses`. Its configured model still appears
    under `effective."x_search.model"`.
  - TTS synthesis.
  - STT (transcription). Hermes has a `pre_transcription` hook, but it fires only for the
    command-based provider and does not carry the effort or the tier.
  - Realtime / voice mode.
  - Image and video generation providers.

  The one exception is the Gemini TTS audio-tag rewrite: it runs through the auxiliary client, so
  it appears as `auxiliary:tts_audio_tags`.
- **`response_service_tier`.** It needs the `llm_execution` middleware *and* a non-streaming
  response. When Hermes streams (its default), it rebuilds the response from SSE chunks and drops
  the `service_tier` field, so the value is `null`. `service_tier` (the requested tier) is recorded
  either way. Auxiliary calls go through no middleware, so their `response_service_tier` is always
  `null`.
- **Truncated payloads.** Hermes caps the `pre_api_request` payload. On very large requests the
  plugin reads model/effort/tier from the JSON prefix; the middleware, when enabled, then records
  the full request anyway.
- **`plugins.isolation: host`** (Hermes `main`, not 0.21.5) runs the plugin out of process. Hermes
  then skips `register_cli_command`, and the `llm_execution` middleware is not carried across the
  process boundary. The hooks still record. Read the status with the venv's Python instead:
  `/opt/hermes/.venv/bin/python /opt/data/plugins/fleet-policy status --json` (folder install) or
  `python -m fleet_policy status --json` (pip install). The output is the same.
- **Only the last call per role** is kept: no history, no counters.
- **`crons --cost` is as good as Hermes' accounting.** It reads the billed cost when the provider
  reports one, else Hermes' estimate; models without a price in Hermes count as `0`. A run is
  matched to its agent turn by time (the session starts inside the run). Read-only: it never
  repairs or migrates the databases.
- **Long-running processes** (gateway, cron ticker) load plugins once at start. Restart them after
  installing or enabling.

## Development

```bash
python -m pytest -q                                   # unit tests, no Hermes needed
FLEET_POLICY_HERMES=/path/to/venv/bin/hermes \
  /path/to/venv/bin/python -m pytest -q tests/test_e2e.py
# several targets: FLEET_POLICY_HERMES=/a/bin/hermes:/b/bin/hermes
# pip-installed plugin instead of folder copy: FLEET_POLICY_INSTALL=entrypoint
python tests/e2e_harness.py /path/to/venv/bin/hermes   # same scenario, prints every JSON
```

The end-to-end tests start `tests/fake_provider.py`, a local OpenAI-compatible stub on 127.0.0.1,
inside a throwaway `HERMES_HOME`. They then run real `hermes -z` turns, a delegated sub-agent, and
a real `hermes cron run`, and check `status --json` after each one.

`tests/test_e2e_watch.py` adds `tests/mcp_stub.py`, a real streamable-HTTP MCP server, and a cron
job with `script=watch-x.sh`; four real `hermes cron tick` runs check that the baseline and an
unchanged tick send **no** request to the LLM stub, that a new id wakes the agent once with the
item's line in its prompt, and that a source failure reaches the agent as a Script Error with
the state unchanged. It needs `mcp>=2` and `uvicorn` in the Hermes venv (skipped otherwise).

## Building the release artifacts

```bash
python scripts/build_dist.py [OUT_DIR]   # default ./dist
```

This produces `fleet-policy-plugin-<v>.tar.gz` (the plugin folder, for `$HERMES_HOME/plugins/`),
`hermes_fleet_policy-<v>.tar.gz` and `-py3-none-any.whl` (pip), and `SHA256SUMS`. It uses
`uv build` when available, `python -m build` otherwise.

## License

MIT — see [LICENSE](LICENSE).
