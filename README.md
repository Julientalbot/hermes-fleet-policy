# hermes-fleet-policy

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for administrators who pin
settings through Hermes' **managed scope** (`/etc/hermes/config.yaml` or `$HERMES_MANAGED_DIR`).

The managed scope tells you what *should* be sent. This plugin records what *is* sent: for each
role (main conversation, delegated sub-agents, cron runs, auxiliary tasks) it stores the model,
the reasoning effort and the service tier of the last real provider request, plus the tier the
provider says it served. It shows them next to the managed-scope state, the effective config and
the cron jobs:

```
hermes fleet-policy status [--json]
hermes fleet-policy crons  [--json]
```

It only observes. It never changes a request, never blocks one, and never stores message content
or credentials.

## How it works

| Surface (public plugin API) | Role recorded | What it sees |
|---|---|---|
| `pre_api_request` hook | `main`, `delegation`, `cron`, `unknown` | every main-loop provider attempt (the request body, which Hermes caps in size) |
| `pre_auxiliary_call` hook | `auxiliary:<task>` | every auxiliary attempt (compression, title generation, vision, web extract, MoA, ...) |
| `llm_execution` middleware | same roles as `pre_api_request` | the full request **and** the provider response, so it also records `response_service_tier` |
| `register_cli_command` | | `hermes fleet-policy ...` |
| `register_skill` | | `fleet-policy-admin`, a short admin runbook |

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
tar -xzf fleet-policy-plugin-0.1.0.tar.gz -C "$HERMES_HOME/plugins"   # -> plugins/fleet-policy/
hermes plugins enable fleet-policy        # appends to plugins.enabled in config.yaml
hermes plugins list --plain | grep fleet-policy
# enabled      user     0.1.0    fleet-policy
```

From a source checkout, copying the `fleet_policy/` directory to
`$HERMES_HOME/plugins/fleet-policy/` gives the same result.

### B. pip (entry point `hermes_agent.plugins`)

```bash
/path/to/hermes/venv/bin/python -m pip install ./hermes_fleet_policy-0.1.0-py3-none-any.whl
hermes plugins enable fleet-policy
hermes plugins list --plain | grep fleet-policy
# enabled      entrypoint 0.1.0    fleet-policy
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
docker cp fleet-policy-plugin-0.1.0.tar.gz "$C":/tmp/fleet-policy-plugin.tar.gz
docker exec -u 10000 "$C" sh -c '
  set -e
  mkdir -p /opt/data/plugins
  rm -rf /opt/data/plugins/fleet-policy
  tar -xzf /tmp/fleet-policy-plugin.tar.gz -C /opt/data/plugins
  rm -f /tmp/fleet-policy-plugin.tar.gz'
docker exec -u 10000 "$C" /opt/hermes/.venv/bin/hermes plugins enable fleet-policy
docker exec -u 10000 "$C" /opt/hermes/.venv/bin/hermes plugins list --plain | grep fleet-policy
#   enabled      user     0.1.0    fleet-policy
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
  "version": "0.1.0",
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

## Building the release artifacts

```bash
python scripts/build_dist.py [OUT_DIR]   # default ./dist
```

This produces `fleet-policy-plugin-<v>.tar.gz` (the plugin folder, for `$HERMES_HOME/plugins/`),
`hermes_fleet_policy-<v>.tar.gz` and `-py3-none-any.whl` (pip), and `SHA256SUMS`. It uses
`uv build` when available, `python -m build` otherwise.

## License

MIT — see [LICENSE](LICENSE).
