# newsgrid — Deployment roadmap

How to make the newsgrid MCP servers available beyond a development checkout. Today only
`newsgrid-weather` exists; the news and briefing servers follow the same pattern
(see [ARCHITECTURE.md](ARCHITECTURE.md), milestones 7–8).

There are two ways to reach a server, and they need different things:

| | **Local (stdio)** | **Remote (Streamable HTTP)** |
|---|---|---|
| Who runs the process | The MCP client starts it as a subprocess | A long-running service you host |
| Reached by | One user on one machine | Anyone with the URL (and credentials) |
| Auth needed | No (runs as the user) | **Yes**, before it is exposed publicly |
| Containers | Optional, rarely worth it | Optional, convenient |

## Phase 0 — Prerequisites (both paths)

- [ ] **Package metadata:** version, license, README, `requires-python` in `pyproject.toml`.
- [ ] **Lock dependencies:** commit `uv.lock` so every deployment installs the same versions.
- [ ] **Zone data on every platform:** `tzdata` is a dependency (Windows ships without
      IANA zones; slim container images may too).
- [ ] **Configuration via environment only:** all settings already use the `NEWSGRID_`
      prefix (`NEWSGRID_HTTP_TIMEOUT`, `NEWSGRID_FORECAST_TTL`, …). Document them.
- [ ] **Logging:** stdout is the stdio transport. Logs go to stderr only.
- [ ] **CI:** ruff, pyright and pytest on every push; build the wheel as an artifact.

## Phase 1 — Local distribution (stdio, no containers)

Goal: anyone can add the server to Claude Code, Claude Desktop or the MCP Inspector with
one command and no checkout.

1. [ ] **Run from Git without installing:**
       `uvx --from git+https://github.com/<owner>/newsgrid newsgrid-weather`
2. [ ] **Publish to PyPI** (trusted publishing from CI on tagged releases), then simply
       `uvx newsgrid-weather` or `pipx install newsgrid`.
3. [ ] **Client configuration snippets** in the README:
       ```json
       { "mcpServers": { "newsgrid-weather": { "command": "uvx", "args": ["newsgrid-weather"] } } }
       ```
       Claude Code: `claude mcp add newsgrid-weather -- uvx newsgrid-weather`
4. [ ] **Optional:** a Claude Desktop extension bundle (`.mcpb`) for one-click install.

Nothing to host, no auth, elicitation works out of the box. This should be the default
way to use newsgrid.

## Phase 2 — Remote HTTP without containers

Goal: a URL clients can connect to, run as a plain service on a VM.

1. [ ] **Run the HTTP transport:** `newsgrid-weather --transport http --host 127.0.0.1 --port 8001`.
       Keep it bound to localhost and put a reverse proxy in front.
2. [ ] **Process supervision:** a systemd unit (Linux) or NSSM / Windows service, with
       restart-on-failure and the `NEWSGRID_*` environment.
3. [ ] **Reverse proxy with TLS** (Caddy or nginx): HTTPS, forward the MCP endpoint path,
       **disable response buffering** (Streamable HTTP uses SSE), long read timeouts.
4. [ ] **Auth before public exposure:**
       - Short term: a shared bearer token or IP allowlist enforced at the proxy.
       - Target: MCP OAuth authorization on the HTTP transport (milestone 8), so clients
         like Claude can connect with a proper sign-in flow.
5. [ ] **Host / origin checks:** when serving on a public hostname, configure the SDK's
       allowed hosts and origins (DNS-rebinding protection) instead of disabling them.
6. [ ] **Health check:** add a lightweight `/healthz` route for the proxy and monitoring.
7. [ ] **Verify** with the Inspector (Streamable HTTP mode) and
       `claude mcp add --transport http newsgrid-weather https://<host>/mcp`.

## Phase 3 — Containers

Goal: one reproducible image per server, run together with Docker Compose, deployable to
any container host.

1. [ ] **Dockerfile** (multi-stage, `uv`):
       - build stage: `uv sync --frozen --no-dev` into `/app/.venv`
       - runtime stage: `python:3.14-slim`, non-root user, copy the venv
       - `CMD ["newsgrid-weather", "--transport", "http", "--host", "0.0.0.0", "--port", "8001"]`
       - `HEALTHCHECK` against `/healthz`
2. [ ] **One image, three entry points:** the package contains all servers; Compose picks
       the command per service.
3. [ ] **`compose.yaml`:** `weather` (8001), `news` (8002), `briefing` (8003) on an internal
       network, plus the reverse proxy (Caddy) as the only published service.
       Volumes for `profiles.sqlite` and `reports.sqlite`.
4. [ ] **Image publishing:** build and push to GHCR from CI on tagged releases.
5. [ ] **Hosting:** a single VM with Compose is enough at first. Managed container
       platforms work too if they support long-lived streaming responses.
6. [ ] **Stdio in a container (optional):** `docker run -i --rm ghcr.io/<owner>/newsgrid
       newsgrid-weather` as an MCP command, for users who prefer isolation over `uvx`.

## Phase 4 — Operating it

- [ ] **Scaling:** the TTL cache and MCP sessions live in process memory. Elicitation is a
      server→client request inside a session, so run **one replica**, or use sticky
      sessions. Move the cache to a shared store only if more replicas are needed.
- [ ] **Upstream etiquette:** a descriptive `NEWSGRID_USER_AGENT` with contact info
      (NWS requires one); keep the TTLs so Open-Meteo and MeteoAlarm aren't hammered.
- [ ] **Rate limiting** per client at the proxy.
- [ ] **Observability:** structured logs, request and upstream-error counts, uptime check.
- [ ] **Release process:** tag → CI builds wheel + image → PyPI + GHCR → redeploy.

## Suggested order

1. Phase 0 and Phase 1: low effort, makes the server usable by others immediately.
2. Phase 3 steps 1–3 locally: Compose is the easiest way to run all three servers over HTTP.
3. Phase 2 auth and TLS, whether the service runs in containers or not.
4. Public deployment, then Phase 4.
