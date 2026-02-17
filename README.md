# AI Agents Hub

AI Agents Hub is a custom router/supervisor service that exposes an OpenAI-compatible API for Open WebUI and coordinates specialist behavior internally.

## MVP Features

- OpenAI-compatible endpoints:
  - `GET /v1/models`
  - `POST /v1/chat/completions` (streaming and non-streaming)
- Specialist routing with coherent final response synthesis
- Web search augmentation with source-aware output
- Image payload passthrough through `chat/completions`
- AI-curated domain memory store (one markdown file per domain/topic)
- Obsidian daily journal writing (journals are not part of memory store)
- Restart-safe persistence and diagnostics endpoints

## Configuration

Main config file: `config.yaml`

API keys are referenced from environment variables:

- `${ENV:OPENAI_API_KEY}`
- `${ENV:GEMINI_API_KEY}`
- `${ENV:AI_AGENTS_HUB_API_KEY}`

When Gemini is configured with the OpenAI-compatible endpoint
(`https://generativelanguage.googleapis.com/v1beta/openai/`), requests are sent
through OpenAI-compatible transport and do not require Vertex/Google SDK libs.

Use:

- `.env` for local development secrets (copy from `.env.example`)
- `/etc/ai-agents-hub/ai-agents-hub.env` for systemd deployments

For local macOS testing, use `config.local.yaml` so data and logs stay under `./data`.

### Specialist Routing Model

`models.default_chat` is used as the specialist routing classifier model.

Default:

```yaml
models:
  default_chat: gpt-5-nano-2025-08-07
```

On each turn, the classifier chooses exactly one specialist domain from:
`general`, `health`, `parenting`, `relationship`, `homelab`, `personal_development`.

For non-general routes, the assistant response starts with:

`Answered by the <specialist> specialist.`

## Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# edit .env and set keys
export AI_AGENTS_HUB_CONFIG="$(pwd)/config.local.yaml"
ai-agents-hub
```

### Local Debug Modes

You can control debug verbosity and output using config or env overrides.

- Levels: `ERROR`, `WARNING`, `INFO`, `DEBUG`, `TRACE`
- Outputs: `console`, `file`, `both`
- Daily file rotation: `logging.daily_rotation: true`

Quick override examples (without editing YAML):

```bash
AI_AGENTS_HUB_LOG_LEVEL=DEBUG AI_AGENTS_HUB_LOG_OUTPUT=console ai-agents-hub
AI_AGENTS_HUB_LOG_LEVEL=TRACE AI_AGENTS_HUB_LOG_OUTPUT=both AI_AGENTS_HUB_LOG_DIR="$(pwd)/data/logs" ai-agents-hub
```

Tail daily-rotating log file:

```bash
tail -f data/logs/ai-agents-hub.log
```

### Local Behavior Tests

You can validate routing and memory behavior locally before pushing/deploying:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q tests/test_specialist_router.py tests/test_supervisor_routing_behavior.py tests/test_memory_curator.py tests/test_memory_workflow.py
```

To print each routing test query and selected specialist:

```bash
python -m pytest -s -q tests/test_specialist_router.py
```

To run a live OpenWebUI-like routing probe (real model calls, no stubs):

```bash
AI_AGENTS_HUB_LIVE_TESTS=1 AI_AGENTS_HUB_CONFIG=config.local.yaml \
python -m pytest -s -q tests/test_live_openwebui_behavior.py
```

This prints for each query:
- query text
- routed specialist
- classifier model calls
- specialist answer model calls

## Install in Proxmox LXC

### Tteck-Style One-liner (Proxmox host)

Run on the Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/<BRANCH>/ct/aiagentshub.sh)"
```

Optional overrides (same style as community-scripts):

```bash
var_ctid=230 var_ram=8192 var_cpu=4 var_disk=20 \
REPO_REF=<REPO_REF> \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/<BRANCH>/ct/aiagentshub.sh)"
```

This installer follows the same lifecycle pattern as tteck/community-scripts:

- host-side CT creation through `build.func`
- in-CT install through `install/aiagentshub-install.sh`
- same command inside CT triggers `update_script`

### Update from inside the LXC (same command)

Run inside the container:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/<BRANCH>/ct/aiagentshub.sh)"
```

When executed inside LXC, this runs the script update flow and refreshes:

- repo code in `/opt/ai-agents-hub`
- Python environment
- systemd service unit
- service restart

Then edit:

- `/etc/ai-agents-hub/config.yaml`
- `/etc/ai-agents-hub/ai-agents-hub.env`

Restart:

```bash
sudo systemctl restart ai-agents-hub
```

Check:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
curl http://localhost:8080/diagnostics
```

## Open WebUI Connection

Point Open WebUI OpenAI connection to:

- Base URL: `http://<ai-agents-hub-host>:8080/v1`
- API Key: one of `server.api_keys` values
- Model shown in Open WebUI: `ai-agents-hub` (configurable via `openai_compat.master_model_id`)

Note: in Open WebUI, use **Admin Settings -> Connections -> OpenAI API** (backend connection).  
Direct browser-side connection checks can fail with `OpenAI: Network Problem` when browser network path differs.

## Configure Specialist System Prompts

Prompts are loaded from markdown files in:

- local: `./prompts/specialists`
- LXC service default: `/etc/ai-agents-hub/prompts/specialists`

Config location:

```yaml
specialists:
  prompts:
    directory: ./prompts/specialists
    auto_reload: true
    files:
      supervisor: supervisor.md
      general: general.md
      health: health.md
      parenting: parenting.md
      relationship: relationship.md
      homelab: homelab.md
      personal_development: personal_development.md
```

The master routing/synthesis agent prompt is:

- `supervisor.md`

When `auto_reload: true`, prompt edits are reloaded automatically on next request.
If you changed `config.yaml` itself, restart the service.

```bash
sudo systemctl restart ai-agents-hub
```

## Onboarding Command

After install, run:

```bash
ai-agents-hub onboard
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

## Memory Editing Strategy

Canonical memory storage is one markdown file per domain under:

- `memories/domains/<domain>.md`

The memory curator model decides whether a new durable memory should be added, and avoids
duplicates when a similar memory already exists.

Default memory curator config:

```yaml
memory:
  curator:
    enabled: true
    model: gemini-2.5-flash
    min_confidence: 0.55
    max_existing_chars: 8000
    max_summary_chars: 160
```

You can manually edit each domain file directly in Obsidian or any editor.
