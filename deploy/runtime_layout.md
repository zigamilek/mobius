# Runtime Layout

AI Agents Hub uses persistent paths so the service survives restarts.

- App code: `/opt/ai-agents-hub`
- Config: `/etc/ai-agents-hub/config.yaml`
- Environment file: `/etc/ai-agents-hub/ai-agents-hub.env`
- Memory root: `/var/lib/ai-agents-hub/memories`
- Obsidian vault: `/var/lib/ai-agents-hub/obsidian`
- Logs: `/var/log/ai-agents-hub`
- Specialist prompts: `/etc/ai-agents-hub/prompts/specialists/*.md`

## Memory Files

- Atomic domain memories:
  - `/var/lib/ai-agents-hub/memories/domains/<domain>/<year>/<date>-<memory_id>-<slug>.md`
- Event log:
  - `/var/lib/ai-agents-hub/memories/_events/<date>.jsonl`
- Derived index:
  - `/var/lib/ai-agents-hub/memories/_index/memory_index.sqlite`

## Journal Files

- Journals are written to Obsidian daily notes:
  - `/var/lib/ai-agents-hub/obsidian/Daily/YYYY-MM-DD.md`

## Diagnostics

- Health: `/healthz`
- Readiness: `/readyz`
- Diagnostics: `/diagnostics`
