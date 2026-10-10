# agent-bridge

Stateless HTTP service on the agent host. Translates requests from `mcp-gateway` into `herdr`
CLI calls and returns their parsed JSON. Herdr is the source of truth for sessions.

Listens on the LAN only. Starts sessions only in allowlisted project directories.

| Endpoint (planned) | herdr |
|---|---|
| list sessions | `agent list` |
| session details | `agent get` |
| send prompt and wait | `agent prompt` + `agent wait` |
| read output | `agent read` |
| start session | session start |
| stop session | `session stop` |
