---
name: fleet-policy-admin
description: Pin org-wide Hermes settings and verify what is sent.
version: 0.2.1
license: MIT
metadata:
  hermes:
    tags: [managed-scope, fleet, policy, cron]
---

# Administer a managed Hermes policy

Use when an administrator pins Hermes settings for a team or a fleet (managed scope) and needs to
check that the policy is applied and that real provider requests follow it.

## Facts to rely on

- The managed layer is `config.yaml` (and `.env`) in `$HERMES_MANAGED_DIR`, else `/etc/hermes`.
  It deep-merges per leaf over the user's `config.yaml`; managed keys cannot be changed with
  `hermes config set`.
- A malformed or empty managed `config.yaml` is IGNORED (fail-open) and `hermes doctor` still
  shows a green "Managed scope active: 0 config key(s)". Always check `managed.error`.
- An empty mapping (`delegation: {}`) changes nothing but is still reported as a pinned key.
- Cron jobs with their own `model`/`provider`/`reasoning_effort` bypass the managed scope
  (`job pin > cron.model > model.default`). So do `/model` switches inside a session.

## Procedure

1. Read the state: `terminal` → `hermes fleet-policy status --json`.
   - `managed.error` not null → the policy is NOT applied. Fix the file (valid YAML, non-empty
     mapping), then re-run. `managed.sha256` lets you compare against the file you shipped.
   - `managed.keys` = number of pinned dotted keys.
   - `effective` = what Hermes resolves after the overlay.
2. Check real traffic: `observed` holds the last provider request per role (`main`,
   `delegation`, `cron`, `auxiliary:<task>`). Compare `model`, `reasoning_effort` and
   `service_tier` with `effective`. `response_service_tier` is the tier the provider says it
   served (OpenAI / xAI only; null otherwise). An empty `observed` only means no request ran
   since the plugin was enabled.
3. Audit cron: `terminal` → `hermes fleet-policy crons --json`. For each job in
   `audit.pinned_model` that should follow the policy, ask the user, then run
   `hermes cron edit <id> --unpin`. For `audit.pinned_effort`, run
   `hermes cron edit <id> --reasoning-effort ""`.
4. Single values: `hermes config get <dotted.key> --json` returns the effective (managed) value.

## Pitfalls

- Never edit the managed file from inside the agent unless the user asked; it is policy.
- `observed` never contains message content or keys; do not try to add them.
- Voice/TTS/transcription and tools that call a provider directly (for example `x_search`) do
  not go through the observed hooks; check them with `effective` only.
