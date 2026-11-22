# Home-Front-Command-MCP: agent context

Release tier: **small** (`v1.0`, `v1.1`, ...).

- Python via `uv`. Server: `src/home_front_command_mcp/server.py`; bundled data: `src/home_front_command_mcp/data/`
  (oref areas/categories/instructions, refreshed by `scripts/refresh_data.py`; guidelines snapshot under `guidelines/`).
- README "Endpoints" is the record of what oref serves and which headers it needs; update it with dates when you
  re-verify. oref is geofenced to Israel and blocks non-browser User-Agents.
- Never invent protection times or instructions in `say`: only numbers present in the feed or the bundled data.
- Keep it generic: no private hosts or deployment details here. Deployments build from a tag of this repo.
