# Runtime Layout

Mobius uses persistent paths so the service survives restarts.

- App code: `/opt/mobius`
- Config: `/etc/mobius/config.yaml`
- Environment file: `/etc/mobius/mobius.env`
- Logs: `/var/log/mobius`
- System prompts: `/etc/mobius/system_prompts/*.md`
- Stateful projection root: `/var/lib/mobius/state`

## Diagnostics

- Health: `/healthz`
- Readiness: `/readyz`
- Diagnostics: `/diagnostics`

## Stateful Projection Layout (Phase 1)

- User root: `/var/lib/mobius/state/users/<user_key>/`
- Tracks registry: `tracks.md`
- Check-ins: `checkins/<track_slug>.md`
- Memories: `memories/<domain>.md` (`memory`, `first_seen`, `last_seen`, `occurrences`)
- Write operation log: `ops.log`
