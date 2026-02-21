# Mobius

Mobius is a custom router/orchestrator service that exposes an OpenAI-compatible API for Open WebUI and coordinates specialist behavior internally.

## MVP Features

- OpenAI-compatible endpoints:
  - `GET /v1/models`
  - `POST /v1/chat/completions` (streaming and non-streaming)
- LLM-based specialist routing with one coherent final response
- Image payload passthrough through `chat/completions`
- Configurable specialist prompts loaded from markdown files
- Optional stateful coaching pipeline (check-in, memory) with automatic writes
- One-way markdown projection from PostgreSQL state into human-readable files
- Response footer summarizing state writes and projection targets
- Restart-safe persistence and diagnostics endpoints

## Configuration

Main config file: `config.yaml`

Startup is strict: the config file must exist and match the schema. Missing files,
unknown keys, or missing specialist domain entries fail fast at startup.

API keys are referenced from environment variables:

- `${ENV:OPENAI_API_KEY}`
- `${ENV:GEMINI_API_KEY}`
- `${ENV:MOBIUS_API_KEY}`

When Gemini is configured with the OpenAI-compatible endpoint
(`https://generativelanguage.googleapis.com/v1beta/openai/`), requests are sent
through OpenAI-compatible transport and do not require Vertex/Google SDK libs.

Use:

- `.env` for local development secrets (copy from `.env.example`)
- `/etc/mobius/mobius.env` for systemd deployments
- Keep runtime behavior flags in `config.yaml` (YAML is the single source of truth).

For local macOS testing, use `config.local.yaml` so data and logs stay under `./data`.

### Stateful Pipeline (Phase 1)

Stateful features are disabled by default and can be enabled with:

```yaml
state:
  enabled: true
  database:
    dsn: ${ENV:MOBIUS_STATE_DSN}
  projection:
    mode: one_way
    output_directory: /var/lib/mobius/state
  user_scope:
    policy: by_user
    anonymous_user_key: anonymous
  decision:
    enabled: true
    model: ""                # empty => fallback to models.orchestrator
    include_fallbacks: false
    facts_only: true         # persist only user-grounded facts
    strict_grounding: true   # require exact user evidence for writes
    max_json_retries: 1      # auto-retry when output is invalid JSON/schema
    on_failure: footer_warning
  checkin:
    enabled: true
  memory:
    enabled: true
    semantic_merge:
      enabled: true
      embedding_model: text-embedding-3-small
      verification_model: "" # empty => fallback to models.orchestrator
      max_json_retries: 1
```

Phase 1 storage contract:

- Source of truth: PostgreSQL (`users`, `turn_events`, `tracks`, `checkin_events`, `memory_cards`, `write_operations`, projection state, etc.)
- Projection: one-way markdown export under `state/users/<user_key>/...`
- Retry safety: idempotency keys per request+channel
- Decision engine: model-driven JSON contract with schema validation and retry
- Memory dedupe: semantic merge using embeddings + verifier model decision
- Failure visibility: if decision model fails, response footer can show state-warning

Single prompt can trigger multiple writes (check-in + memory) when justified.

State write policy (facts-only):

- `memory`: durable long-term facts/preferences/recurring patterns
- `check-in`: ongoing goal/habit/system with progress/barrier/coaching signal
- conservative default: when uncertain, do not write memory
- strict grounding: write blocks must include an exact user-text evidence quote

### Specialist Routing Model

`models.orchestrator` is used as the specialist routing orchestrator model.

Default:

```yaml
models:
  orchestrator: gpt-5-nano-2025-08-07
```

On each turn, the orchestrator chooses exactly one specialist domain from:
`general`, `health`, `parenting`, `relationships`, `homelab`, `personal_development`.

Routing continuity is sticky across conversation history:

- every turn is classified by the orchestrator model
- if previous user messages exist in the same session, Mobius sends recent routed
  domain history (last 3 domains) to the classifier as continuity context
- classifier is instructed to keep the current domain unless the user clearly asks
  to switch (or the topic clearly shifts)
- continuity does not parse `Answered by ...`; that prefix is UI-only

For non-general routes, by default the assistant response starts with:

`*Answered by <display_name> (the <specialist> specialist) using <model> model.*`

This is configurable via `api.attribution`:

```yaml
api:
  attribution:
    enabled: true
    include_model: true
    include_general: false
    template: "Answered by {display_name} (the {domain_label} specialist){model_suffix}."
```

Template placeholders:

- `{display_name}`: specialist display name (from `specialists.by_domain.<domain>.display_name`)
- `{domain}`: normalized domain key (for example `personal_development`)
- `{domain_label}`: human label form (for example `personal development`)
- `{model}`: model used for the response turn
- `{model_suffix}`: either ` using <model> model` or empty (depends on `include_model`)

### Current Timestamp Context

Mobius can inject the current date/time into the system context on every
request, so the active specialist always sees an authoritative timestamp.

Default:

```yaml
runtime:
  inject_current_timestamp: true
  timezone: Europe/Ljubljana
  include_timestamp_in_routing: false
```

- `inject_current_timestamp`: include timestamp in orchestrated response prompt
- `timezone`: IANA timezone used to format the timestamp
- `include_timestamp_in_routing`: also include timestamp in routing classifier context

## Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# edit .env and set keys
export MOBIUS_CONFIG="$(pwd)/config.local.yaml"
mobius
# show installed CLI version
mobius --version
```

### CLI Commands

Common Mobius CLI commands:

```bash
mobius onboarding                 # interactive setup (keep/overwrite/cancel existing values)
mobius version                    # print version
mobius paths                      # print config/env/prompts/log paths
mobius diagnostics                # print curl checks and detected local IP
mobius status                     # systemd status (LXC/server)
mobius start                      # systemd start   (LXC/server)
mobius stop                       # systemd stop    (LXC/server)
mobius restart                    # systemd restart (LXC/server)
mobius update                     # run in-LXC updater (same flow as one-liner)
mobius db bootstrap-local         # bootstrap local PostgreSQL + pgvector for state
mobius logs --follow              # service logs (default source)
mobius logs --file --follow       # file logs from configured log path
```

### Local Debug Modes

Configure debug verbosity in YAML:

- Levels: `ERROR`, `WARNING`, `INFO`, `DEBUG`, `TRACE`
- Outputs: `console`, `file`, `both`
- Daily file rotation: `logging.daily_rotation: true`

Tail daily-rotating log file:

```bash
tail -f data/logs/mobius.log
```

### Local Behavior Tests

You can validate routing behavior locally before pushing/deploying:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q \
  tests/test_specialist_router.py \
  tests/test_orchestrator_routing_behavior.py \
  tests/test_state_decision_engine.py
```

To print each routing test query and selected specialist:

```bash
python -m pytest -s -q tests/test_specialist_router.py
```

To run a live OpenWebUI-like routing probe (real model calls, no stubs):

```bash
MOBIUS_LIVE_TESTS=1 MOBIUS_CONFIG=config.local.yaml \
python -m pytest -s -q tests/test_live_openwebui_behavior.py
```

This prints for each query:
- query text
- routed specialist
- routing confidence
- routing reason
- orchestrator model calls
- specialist model calls

## Versioning and Releases

Mobius uses semantic versioning (`MAJOR.MINOR.PATCH`), currently in the `0.x`
phase while architecture is still evolving.

- `PATCH` (`0.1.1`): bug fixes and safe internal changes
- `MINOR` (`0.2.0`): new features or behavior/config changes during `0.x`
- `MAJOR` (`1.0.0`): stable, intentionally versioned public contract

Runtime version visibility:

- CLI: `mobius --version`
- API: `GET /diagnostics` includes a top-level `version` field

### Release Checklist

1. Update version in:
   - `pyproject.toml` (`[project].version`)
   - `src/mobius/__init__.py` (`__version__`)
2. Run tests:
   - `python -m pytest -q`
3. Commit:
   - `git commit -m "release: vX.Y.Z"`
4. Tag:
   - `git tag -a vX.Y.Z -m "Mobius vX.Y.Z"`
5. Push branch and tags:
   - `git push && git push --tags`
6. Deploy a pinned release (recommended):
   - `REPO_REF=vX.Y.Z bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/vX.Y.Z/ct/mobius.sh)"`

## Install in Proxmox LXC

### Tteck-Style One-liner (Proxmox host)

Run on the Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/<BRANCH>/ct/mobius.sh)"
```

By default, fresh installs now attempt local DB bootstrap (PostgreSQL + pgvector)
and enable `state.enabled=true` automatically when bootstrap succeeds.

To skip this behavior:

```bash
MOBIUS_BOOTSTRAP_LOCAL_DB=no \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/<BRANCH>/ct/mobius.sh)"
```

Optional overrides (same style as community-scripts):

```bash
var_ctid=230 var_ram=8192 var_cpu=4 var_disk=20 \
REPO_REF=<REPO_REF> \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/<BRANCH>/ct/mobius.sh)"
```

This installer follows the same lifecycle pattern as tteck/community-scripts:

- host-side CT creation through `build.func`
- in-CT install through `install/mobius-install.sh`
- same command inside CT triggers `update_script`

### Update from inside the LXC (same command)

Run inside the container (recommended):

```bash
mobius update
```

If you want update-time DB bootstrap as well, run:

```bash
MOBIUS_BOOTSTRAP_LOCAL_DB_ON_UPDATE=yes mobius update
```

Note: even when `MOBIUS_BOOTSTRAP_LOCAL_DB_ON_UPDATE=no`, the updater now
auto-runs local DB bootstrap if your config has `state.enabled: true` but
`MOBIUS_STATE_DSN` is missing, so the service does not restart into a crash loop.

Equivalent one-liner:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/<BRANCH>/ct/mobius.sh)"
```

Retrofit local DB bootstrap in an existing LXC:

```bash
sudo mobius db bootstrap-local
```

When executed inside LXC, this runs the script update flow and refreshes:

- repo code in `/opt/mobius`
- Python environment
- systemd service unit
- service restart

Then edit:

- `/etc/mobius/config.yaml`
- `/etc/mobius/mobius.env`

Restart:

```bash
sudo systemctl restart mobius
```

Check:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
curl http://localhost:8080/diagnostics
```

## Open WebUI Connection

Point Open WebUI OpenAI connection to:

- Base URL: `http://<mobius-host>:8080/v1`
- API Key: one of `server.api_keys` values
- Model shown in Open WebUI: `mobius` (configurable via `api.public_model_id`)

Note: in Open WebUI, use **Admin Settings -> Connections -> OpenAI API** (backend connection).  
Direct browser-side connection checks can fail with `OpenAI: Network Problem` when browser network path differs.

## Configure Specialist System Prompts

Prompts are loaded from markdown files in:

- local: `./system_prompts`
- LXC service default: `/etc/mobius/system_prompts`

Config location:

```yaml
api:
  attribution:
    enabled: true
    include_model: true
    include_general: false
    template: "Answered by {display_name} (the {domain_label} specialist){model_suffix}."

specialists:
  prompts_directory: ./system_prompts
  auto_reload: true
  orchestrator_prompt_file: _orchestrator.md
  by_domain:
    general:
      model: gpt-5.2
      prompt_file: general.md
    health:
      model: gpt-5.2
      prompt_file: health.md
      display_name: The Coach
    parenting:
      model: gpt-5.2
      prompt_file: parenting.md
      display_name: The Coach
    relationships:
      model: gpt-5.2
      prompt_file: relationships.md
      display_name: The Counselor
    homelab:
      model: gpt-5.2
      prompt_file: homelab.md
      display_name: The Tinkerer
    personal_development:
      model: gpt-5.2
      prompt_file: personal_development.md
      display_name: The Mentor

runtime:
  inject_current_timestamp: true
  timezone: Europe/Ljubljana
  include_timestamp_in_routing: false
```

The master routing orchestrator prompt is:

- `_orchestrator.md`

When `auto_reload: true`, prompt edits are reloaded automatically on next request.
If you changed `config.yaml` itself, restart the service.

`display_name` is optional. If omitted, Mobius falls back to the catalog label for that specialist.

```bash
sudo systemctl restart mobius
```

## Onboarding Command

After install, run:

```bash
mobius onboarding
```

It will guide you through:

- API keys in env file
- service host/port
- prompt directory path
- writing env/config safely

Then it prints:

- restart command
- health-check commands
- Open WebUI connection settings

