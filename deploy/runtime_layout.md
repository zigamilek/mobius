# Runtime Layout

Mobius uses persistent paths so the service survives restarts.

- App code: `/opt/mobius`
- Config: `/etc/mobius/config.yaml`
- Environment file: `/etc/mobius/mobius.env`
- Logs: `/var/log/mobius`
- System prompts: `/etc/mobius/system_prompts/*.md`

## Diagnostics

- Health: `/healthz`
- Readiness: `/readyz`
- Diagnostics: `/diagnostics`
