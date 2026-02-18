# Mobius

Mobius is a custom router/orchestrator service that exposes an OpenAI-compatible API for Open WebUI and coordinates specialist behavior internally.

## MVP Features

- OpenAI-compatible endpoints:
  - `GET /v1/models`
  - `POST /v1/chat/completions` (streaming and non-streaming)
- LLM-based specialist routing with one coherent final response
- Image payload passthrough through `chat/completions`
- Configurable specialist prompts loaded from markdown files
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

For local macOS testing, use `config.local.yaml` so data and logs stay under `./data`.

### Specialist Routing Model

`models.orchestrator` is used as the specialist routing orchestrator model.

Default:

```yaml
models:
  orchestrator: gpt-5-nano-2025-08-07
```

On each turn, the orchestrator chooses exactly one specialist domain from:
`general`, `health`, `parenting`, `relationships`, `homelab`, `personal_development`.

For non-general routes, the assistant response starts with:

`*Answered by the <specialist> specialist.*`

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
mobius onboarding                 # interactive setup (with overwrite confirmation)
mobius version                    # print version
mobius paths                      # print config/env/prompts/log paths
mobius diagnostics                # print curl checks and detected local IP
mobius status                     # systemd status (LXC/server)
mobius start                      # systemd start   (LXC/server)
mobius stop                       # systemd stop    (LXC/server)
mobius restart                    # systemd restart (LXC/server)
mobius logs --follow              # journal logs (default source)
mobius logs --file --follow       # file logs from configured log path
```

### Local Debug Modes

You can control debug verbosity and output using config or env overrides.

- Levels: `ERROR`, `WARNING`, `INFO`, `DEBUG`, `TRACE`
- Outputs: `console`, `file`, `both`
- Daily file rotation: `logging.daily_rotation: true`

Quick override examples (without editing YAML):

```bash
MOBIUS_LOG_LEVEL=DEBUG MOBIUS_LOG_OUTPUT=console mobius
MOBIUS_LOG_LEVEL=TRACE MOBIUS_LOG_OUTPUT=both MOBIUS_LOG_DIR="$(pwd)/data/logs" mobius
```

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
python -m pytest -q tests/test_specialist_router.py tests/test_orchestrator_routing_behavior.py
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

Run inside the container:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/<BRANCH>/ct/mobius.sh)"
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
    parenting:
      model: gpt-5.2
      prompt_file: parenting.md
    relationships:
      model: gpt-5.2
      prompt_file: relationships.md
    homelab:
      model: gpt-5.2
      prompt_file: homelab.md
    personal_development:
      model: gpt-5.2
      prompt_file: personal_development.md
```

The master routing orchestrator prompt is:

- `_orchestrator.md`

When `auto_reload: true`, prompt edits are reloaded automatically on next request.
If you changed `config.yaml` itself, restart the service.

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

