# newsgrid — Next steps toward production

Ten steps to take the weather server (and the servers that follow it) from a working
prototype to a business-grade service. They are ordered roughly by priority: correctness
and security first, then operations, then lifecycle. Each step names the problem in the
current code, what to do about it, and how we know it is done.

---

## 1. Fix geocoding correctness and make ranking explicit

**Problem.** `WeatherService.search` keeps only results whose name *exactly* equals the
query when any exist. "Frankfurt" therefore keeps small places literally named
"Frankfurt" and drops "Frankfurt am Main" — the place almost everyone means. Ambiguity
detection (`is_ambiguous`) only compares the top two populations, and many small places
have `population = None`.

**Do.**
- Rank candidates with a scoring function (name match quality, population, feature class
  such as capital/city/village) instead of a hard exact-match filter.
- Treat a missing population as "unknown", not 0, and cap how far a village can outrank a city.
- Add regression tests for known traps: Frankfurt, Springfield, Paris (FR vs. TX), Portland.

**Done when.** A table-driven test suite of ~30 ambiguous names passes, and the best match
for each is the one a human would expect.

## 2. Secure the HTTP transport

**Problem.** `--transport http` serves Streamable HTTP with no authentication, no
`Origin` check and no TLS. Anyone who can reach the port can call the tools.

**Do.**
- Add authorization following the MCP spec (OAuth 2.1 resource server: validate bearer
  tokens against an identity provider, check audience and scopes).
- Validate the `Origin` header to prevent DNS-rebinding attacks; keep binding to
  `127.0.0.1` by default and require an explicit flag for `0.0.0.0`.
- Terminate TLS at a reverse proxy (Caddy, nginx, a cloud load balancer); never expose the
  Python process directly.
- Limit input: maximum length for `location`, a cap on `days`, rate limits per client/token.

**Done when.** Requests without a valid token get 401, a foreign `Origin` gets 403, and a
security test suite covers both.

## 3. Treat upstream content as untrusted

**Problem.** Alert titles and descriptions from NWS and MeteoAlarm are passed verbatim to
an LLM. Upstream text is untrusted data and can carry prompt-injection content or markup.

**Do.**
- Strip control characters and HTML and truncate free-text fields in the provider layer.
- Keep structured fields (severity, onset, expiry) separate from free text, so clients can
  render them without interpreting prose.
- Document in the tool descriptions that the text fields are third-party content.

**Done when.** Provider tests include hostile fixtures (HTML, very long text, injection
phrases) and the server output is sanitized.

## 4. Clarify upstream licensing and SLAs

**Problem.** The free Open-Meteo API is licensed for **non-commercial** use only. NWS asks
for contact details in the User-Agent. MeteoAlarm has its own terms of use. None of the
three provides an SLA.

**Do.**
- Decide on the commercial path: an Open-Meteo API subscription (key via
  `NEWSGRID_OPEN_METEO_API_KEY`) or an alternative provider.
- Make `NEWSGRID_USER_AGENT` with a contact address mandatory in production.
- Put providers behind an interface so a second source can serve as a fallback.
- Record the attribution each source requires and show it in the tool output.

**Done when.** A short `PROVIDERS.md` lists the license, quota, attribution and fallback for
each source, signed off by whoever owns legal and compliance questions.

## 5. Production-grade caching and resilience

**Problem.** `HttpClient` caches in a process-local `dict` with no size limit and no
eviction, so memory grows with every distinct query. Each replica has its own cache.
Concurrent misses for the same key all hit the upstream (cache stampede). The timeout
budget is 20 s × 3 attempts, which can exceed what MCP clients wait for.

**Do.**
- Use a bounded LRU locally and optionally Redis as a shared cache across replicas.
- Coalesce in-flight requests per key, and serve stale data while revalidating on
  upstream errors.
- Add a circuit breaker per upstream host.
- Set an overall deadline per tool call (e.g. 10 s) that retries must fit into.

**Done when.** Memory stays flat under a load test with random locations, and the server
keeps answering from cache while an upstream is down (tested with respx fault injection).

## 6. Observability

**Problem.** There are no metrics, traces or health endpoints. When a client reports
"weather is slow", we can't see why.

**Do.**
- Write structured JSON logs with a request/session ID on every line, with no personal
  data or tokens.
- Add OpenTelemetry traces across tool call → resolver → upstream HTTP, plus metrics:
  tool latency (p50/p95/p99), upstream error rate, cache hit rate, elicitation outcomes.
- Expose `/healthz` (process alive) and `/readyz` (upstreams reachable), separate from
  `/mcp`.
- Define SLOs (e.g. 99.5 % of `get_forecast` calls under 2 s) and alert on error budgets.

**Done when.** A dashboard shows the four golden signals per tool, and an upstream outage
fires an alert within 5 minutes.

## 7. Packaging, deployment and scaling

**Problem.** The server runs from a local `.venv`. Its default port is 8001, but our MCP
config points to 8005, so the two can drift apart. Elicitation needs session state, which
affects how the server can be scaled.

**Do.**
- Commit a `uv.lock` and build a slim, non-root container image with a pinned base image
  and a health check.
- Read all runtime settings, including host and port, from `NEWSGRID_*` variables, with
  one documented default per environment.
- Scale horizontally with session affinity at the load balancer, or move session state
  to a shared store, so elicitation works across replicas.
- Shut down gracefully: drain in-flight tool calls and close `HttpClient` on SIGTERM.

**Done when.** `docker compose up` (or a Helm chart) starts the server behind TLS with
auth, and a rolling restart drops no in-flight requests.

## 8. CI/CD and quality gates

**Problem.** Ruff, pyright and pytest are configured but only run locally, and nothing
runs on push.

**Do.**
- Add a GitHub Actions pipeline: ruff → pyright (strict mode for `src/`) → pytest with a
  coverage gate (e.g. ≥ 85 %) → image build → deploy to staging.
- Add MCP contract tests: start the server, list tools and resources, and compare each
  tool's input and output schemas against a committed snapshot.
- Add nightly live smoke tests against the real upstreams (separate from the hermetic,
  respx-mocked unit tests) to catch API changes early.
- Protect `main` so changes merge only through reviewed PRs that pass these checks.

**Done when.** Every PR shows green checks, and an accidental schema change fails the
build.

## 9. Supply-chain security and dependency lifecycle

**Problem.** Dependencies are version ranges without a lock file. `mcp` is pinned to
`<2.4` with no plan for upgrading it. There is no vulnerability scanning.

**Do.**
- Use a lock file and let Renovate or Dependabot open grouped weekly update PRs.
- Run `pip-audit` or OSV scanning in CI, generate an SBOM (CycloneDX) per release, and
  scan and sign container images (Trivy, cosign).
- Track MCP spec and SDK releases and budget a quarterly upgrade window.
- Add `SECURITY.md` with a vulnerability-reporting contact.

**Done when.** A known-vulnerable dependency fails the build, and every release ships
with an SBOM and a signed image.

## 10. Versioning and API lifecycle

**Problem.** Tool names, arguments and output schemas form a public contract for every
MCP client. The project is at `0.1.0` with no changelog and no deprecation policy.

**Do.**
- Version semantically: a breaking schema change is a major release, a new optional
  field is a minor release.
- Deprecate in steps: mark the tool or field in its description, keep it for a defined
  period (e.g. two minor releases), then remove it.
- Automate releases (release-please or similar) to produce a changelog, tag, image and
  SBOM.
- Grow `ARCHITECTURE.md` with ADRs (architecture decision records) for choices such as
  the cache backend, the auth provider and the provider fallbacks.

**Done when.** `1.0.0` is cut with a published changelog, and a schema snapshot test
blocks breaking changes outside major releases.

---

### Suggested sequencing

| Phase | Steps | Outcome |
|---|---|---|
| Harden | 1, 2, 3 | Correct answers, safe to expose on a network |
| Operate | 5, 6, 7 | Runs reliably as a hosted service |
| Sustain | 4, 8, 9, 10 | Legally sound, maintainable, releasable |
