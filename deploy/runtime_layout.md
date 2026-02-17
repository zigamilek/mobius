# Runtime Layout

AI Agents Hub uses persistent paths so the service survives restarts.

- App code: `/opt/ai-agents-hub`
- Config: `/etc/ai-agents-hub/config.yaml`
- Environment file: `/etc/ai-agents-hub/ai-agents-hub.env`
- Logs: `/var/log/ai-agents-hub`
- Specialist prompts: `/etc/ai-agents-hub/prompts/specialists/*.md`

## Diagnostics

- Health: `/healthz`
- Readiness: `/readyz`
- Diagnostics: `/diagnostics`
