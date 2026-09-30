# web-mcp

A local, guard-railed **web research** MCP server for any MCP-capable agent (Claude Desktop, Claude Code, Cursor, Windsurf, VS Code, Cline / PostQode, …).

Instead of handing an agent an open browser, `web-mcp` makes it **declare what it is researching**, gives it a **budget**, and checks every search and page read against that plan. When a limit is hit, the **user** is asked whether to continue — the agent cannot approve itself.

- **4 small tools** (~600 tokens of context in total): `research_start`, `web_search`, `web_fetch`, and `research_approve` (used only when the user has to be asked something)
- **Budgets** per session and per day; when they run out **the agent asks you in the conversation** — no pop-ups, no terminals
- **Guardrails**: topic-drift check, secret/PII/code leak blocking, SSRF protection, prompt-injection defences, domain policy, robots.txt, audit log
- **Latest info**: freshness filters, published dates, and today's date returned to the agent
- **Local & portable**: Python, stdio transport, no cloud service, no API key required (DuckDuckGo fallback; SearXNG or Brave/Tavily/Serper optional)

---

## Contents
1. [Quick start](#1-quick-start) · 2. [Architecture](#2-architecture) · 3. [How it works](#3-how-it-works) · 4. [Tools](#4-tools) · 5. [Guardrails](#5-guardrails) · 6. [Permission flow](#6-permission-flow) · 7. [Settings](#7-settings-reference) · 8. [Providers](#8-search-providers) · 9. [CLI](#9-cli) · 10. [Client setup](#10-client-setup) · 11. [Privacy & security](#11-privacy--security-notes) · 12. [Troubleshooting](#12-troubleshooting) · 13. [Development](#13-development)

---

## 1. Quick start

**Global install** (isolated environment, `web-mcp` on your PATH, all dependencies included):

```bash
git clone https://github.com/Sreenuraj/web-mcp.git && cd web-mcp
./scripts/install.sh                                   # installs uv if missing, then web-mcp
# options:
./scripts/install.sh --client claude-desktop --client cursor   # also register with clients
./scripts/install.sh --extras keyring                          # OS-keychain support for API keys
./scripts/install.sh --searxng                                 # also start local SearXNG (Docker)
```

Manual equivalents: `uv tool install .` · `pipx install .` · `pip install .` (Python ≥ 3.10). Windows: `scripts\install.ps1`.
Upgrade: re-run the installer. Uninstall: `./scripts/install.sh --uninstall`.

**Check it, then register it with your agent:**

```bash
web-mcp doctor --live            # verifies deps, paths, providers and runs one real search
web-mcp install-client print     # prints the JSON block to paste into any client
web-mcp install-client claude-desktop   # or: claude-code | cursor | windsurf  (edits config, keeps a .bak)
```

Generic MCP client entry (what `print` outputs):

```json
{ "mcpServers": { "web-mcp": { "command": "/absolute/path/to/web-mcp", "args": ["serve"] } } }
```

Works with zero configuration: with no SearXNG running, it falls back to DuckDuckGo. For better, more reliable results run `web-mcp searxng up` (Docker) or set a `BRAVE_API_KEY`.

---

## 2. Architecture

```
                         ┌──────────────────────────── web-mcp process (stdio) ────────────────────────────┐
 MCP client / agent      │                                                                                  │
 ┌───────────────┐  3    │  server.py  ──►  core.Engine  (orchestrates every call)                          │
 │ research_start│◄────► │   FastMCP        │                                                               │
 │ web_search    │ tools │                  ├─► session.py     plan, budget counters, result registry (S1…) │
 │ web_fetch     │       │                  ├─► policy/        leakage · relevance(drift/dup) · domains ·   │
 └───────────────┘       │                  │                  injection · rate limiter                     │
        ▲                │                  ├─► approval/      broker ─► relay (ask in chat) │ elicitation │ deny
        │ user prompt    │                  ├─► providers/     chain: searxng → duckduckgo → brave/tavily…  │
        │ (never the     │                  ├─► net/safe_http  DNS-pinned, SSRF-safe, redirect-checked GET  │
        │  agent)        │                  ├─► extract/       trafilatura · pypdf · BM25 passage selection │
        └────────────────┼──────────────────┤                                                              │
                         │                  └─► storage.py     SQLite: sessions, counters, approvals, cache │
                         │                      audit.py       append-only JSONL                            │
                         └──────────────────────────────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `server.py` | Defines exactly 4 tools with minimal schemas (schemas are slimmed to save context). |
| `core.py` | The engine. Runs the ordered check pipeline for every call and turns outcomes into short text results. |
| `session.py` | Research plan, per-session limits & usage, result-ID registry, persistence (survives restarts). |
| `policy/` | Pure, unit-tested rules: `leakage` (secrets/PII/code), `relevance` (topic drift, duplicates), `domains`, `injection`, `budget` (rate limiter). |
| `approval/` | `broker` implements the approval channels: `relay` (agent asks the user in the conversation), `elicitation`, `deny`. |
| `providers/` | Search backends behind one interface; `chain` adds fallback + circuit breaker. |
| `net/safe_http.py` | The only code that touches arbitrary URLs. |
| `extract/` | HTML → main text (hidden text removed), PDF → text, BM25 passage ranking under a token cap. |
| `storage.py` / `audit.py` | SQLite state & cache; JSONL audit trail. |

---

## 3. How it works

```
Agent                          web-mcp                                              User
 │ research_start(topic,…)      │                                                       │
 │─────────────────────────────►│ validate plan · secret scan · session limits          │
 │◄─────────────────────────────│ session_id · today's date · budget · rules            │
 │ web_search("q")              │                                                       │
 │─────────────────────────────►│ ① leak scan  ② duplicate?  ③ topic drift  ④ budget    │
 │                              │ ⑤ cache → provider chain → dedup/rank/freshness       │
 │◄─────────────────────────────│ [S1] title — domain · date / snippet … + budget line  │
 │ web_fetch("S2", focus="…")   │                                                       │
 │─────────────────────────────►│ ① URL/SSRF ② domain policy ③ known-URL ④ budget       │
 │                              │ ⑤ robots ⑥ safe GET ⑦ extract ⑧ BM25 select ⑨ sanitize│
 │◄─────────────────────────────│ <<<UNTRUSTED_WEB_CONTENT>>> passages … + budget line  │
 │            … budget hit …    │                                                       │
 │ web_search("q")              │                                                       │
 │─────────────────────────────►│ LIMIT ── ask ────────────────────────────────────────►│ Allow once / Extend /
 │                              │◄──────────────────────────────────────── decision ────│ Rest of session / Deny
 │◄─────────────────────────────│ results  — or —  "DENIED … do not work around it"     │
```

**Session.** `research_start` takes `topic`, `objective`, `sub_questions`, `depth`, `freshness`. `depth` selects a budget preset:

| depth | searches | page reads | output tokens | wall time |
|---|---|---|---|---|
| quick | 3 | 3 | 12 k | 5 min |
| standard | 8 | 10 | 40 k | 15 min |
| deep | 20 | 25 | 120 k | 40 min |

The plan is turned into a keyword profile used for drift detection. Sessions are stored in SQLite, so they survive a server restart (TTL 2 h).

**Search pipeline** (in order; stops at the first failing check):
1. *Leak scan* — secrets → blocked; PII → warn/block/ask; long or code-like queries → blocked. (Nothing is sent to the search engine.)
2. *Duplicate check* — near-identical earlier query (Jaccard ≥ 0.8) → returns earlier results **free**.
3. *Drift check* — query terms vs. the plan → warn / ask / block.
4. *Budget check* — session limits, then daily caps → permission prompt.
5. *Cache → provider chain* (SearXNG → DuckDuckGo → …). Provider failures are **not charged**.
6. *Normalize* — canonical-URL and title dedup, tracking params stripped, deny-listed domains removed, preferred domains boosted, freshness window applied using published dates, stable IDs `S1…`, snippets trimmed to 300 chars.

**Fetch pipeline:**
1. Resolve `S3` → URL. URL must be http(s), no credentials, allowed port, and **every resolved IP must be public**.
2. Domain policy (deny / strict allow-list / require-approval).
3. *Known-URL rule* — by default only URLs from this session's search results (or links found on pages it read) can be fetched; anything else needs approval. This is the main “no roaming” control.
4. Budget → robots.txt → per-domain rate limit → GET (pinned to the validated IP, each redirect re-validated, size/time/type capped).
5. Extract main text (HTML via trafilatura with hidden elements removed; PDF via pypdf; JSON/text passthrough), cache the **extracted** document.
6. Split into ~350-token passages, rank by BM25 against `focus` (or the session's questions), return the best passages in original order within `max_tokens`, gaps marked `[…]`.
7. Remove invisible characters, flag instruction-like passages, wrap in an untrusted-content envelope, charge the returned tokens to the session.

**Every response ends with a budget line** — there is no status tool:
`budget: search 3/8 · fetch 2/10 · tokens 9k/40k · 11m left`

---

## 4. Tools

### `research_start(topic, objective, sub_questions, depth="standard", freshness="any")`
Required before searching. Returns `session_id`, today's date (so “latest” is unambiguous), the budget and a short rule reminder (this is where usage guidance lives — it costs context only when research actually starts). Plans containing secrets are rejected. Limits on concurrent sessions / sessions per hour ask the user.

### `web_search(session_id, query, freshness?, max_results=5, site?)`
```
via duckduckgo
<<<UNTRUSTED_WEB_CONTENT source="search results">>>
[S1] Python 3.14 released — python.org · 2026-09-02
     Free-threaded build is now officially supported; single-thread overhead ~5-10%...
[S2] ...
<<<END_UNTRUSTED_WEB_CONTENT>>>
budget: search 1/8 · fetch 0/10 · tokens 0.3k/40k · 14m left
```
`freshness`: `any|day|week|month|year`. `site`: bare domain, e.g. `docs.python.org`.

### `web_fetch(session_id, target, focus?, max_tokens=2000)`
`target` is a result ID (`S2`) or a URL. `focus` says what to look for so only relevant passages are returned. Output header shows title, published date, type, and whether the result is `complete` or `partial`.

### `research_approve(approval_id, choice, user_reply)`
Records the **user's** answer to an `APPROVAL REQUIRED` message (see [Permission flow](#6-permission-flow)). The agent must ask the user first; `user_reply` (their words) is required and audited.

### Result vocabulary (what the agent sees when a guardrail acts)
`BLOCKED:` a rule refused it (nothing sent, not charged) · `DENIED:` the user said no — don't retry · `LIMIT:` session over · `APPROVAL REQUIRED (id …):` the agent must ask the user, then call `research_approve` and repeat the call · `ERROR:` bad input / provider down (not charged).

---

## 5. Guardrails

| Guardrail | What it does | Tune with |
|---|---|---|
| **Budgets** | Per-session searches / fetches / output-tokens / minutes. | `[budget.*]` |
| **Daily caps** | Across *all* sessions, so an agent can't dodge limits by opening new sessions. | `limits.daily_searches/fetches` |
| **Session limits** | Max concurrent sessions, sessions per hour → user approval. | `limits.max_concurrent_sessions`, `max_sessions_per_hour` |
| **Topic drift** | Query vocabulary must overlap the declared plan (plus terms learned from pages already read). | `policy.drift_action`, `drift_threshold` |
| **Duplicate queries** | Served free from the earlier result. | — |
| **Secrets** | AWS/GitHub/OpenAI/Slack/Google/Stripe keys, JWTs, private keys, `password=…`, credentials in URLs, high-entropy tokens → blocked. | `policy.secret_action` |
| **PII / local data** | Emails, phones, card numbers (Luhn), local paths, private IPs. | `policy.pii_action` |
| **Code / log paste** | Long or code-like queries rejected so private code isn't sent to search engines. | `limits.max_query_chars` |
| **SSRF** | Public IPs only (IPv4+IPv6, v4-mapped, NAT64, 6to4, CGNAT, metadata IPs), DNS pinned to validated IP, redirects re-validated, port allow-list, no credentials in URL. | `fetch.allowed_ports` |
| **Known-URL rule** | Only URLs from results / read pages, unless the user approves. | `policy.allow_arbitrary_urls` |
| **Domain policy** | Deny list, strict allow list, require-approval list (social media by default), preferred-domain ranking boost. | `[domains]` |
| **robots.txt** | Respected (cached 1 h). | `policy.respect_robots` |
| **Rate limits** | Per-domain token bucket; provider spacing + circuit breaker. | `limits.per_domain_rps`, `providers.*` |
| **Size/type caps** | Byte cap, content-type allow-list, redirect cap, PDF page cap. | `[fetch]` |
| **Prompt-injection defence** | Hidden/offscreen elements and invisible/bidi characters removed; instruction-like passages flagged (or dropped); all web text wrapped in `<<<UNTRUSTED_WEB_CONTENT>>>` and the agent is told to treat it as data. | `policy.strip_suspicious` |
| **Audit log** | Every decision (allowed/blocked/denied/approved) as JSONL. Queries that triggered the secret rule are logged as `[redacted]`. | `[audit]` |

> Injection heuristics reduce risk but cannot eliminate it — treat web content as untrusted, and keep the agent's other tools appropriately restricted.

---

## 6. Permission flow

There are **no dialogs and no terminals** in the default setup. Everything happens inside the agent conversation:

```
Agent ──web_search("…")──────────────► web-mcp   search budget is used up (8/8)
Agent ◄─ "APPROVAL REQUIRED (id a_1f2c): search limit reached (8/8); agent wants to search: …
          Ask the USER now … then call research_approve(...) and repeat the same call." ─┘
Agent ── asks YOU (its ask-user tool, or a plain question in the chat) ──► "Allow once / Extend budget / Deny?"
You   ── "extend it" ──►  Agent
Agent ──research_approve(approval_id="a_1f2c", choice="extend", user_reply="extend it")──► web-mcp: Recorded
Agent ──web_search("…") (same call again)────────────────────────► results
```

How it is kept safe even though the agent carries the answer:

| Protection | Effect |
|---|---|
| **Requests are created by the server** | An `approval_id` exists only after a real gated call answered `APPROVAL REQUIRED`. The agent cannot invent one. |
| **Bound to the exact request** | A decision is only honoured for the same session + same action (same query / URL / topic). It cannot be reused for anything else. |
| **Single use, short-lived** | Consumed on first use; expires after `approval_ttl_seconds` (10 min). |
| **The user's own words are required** | `user_reply` is mandatory and written to the audit log next to the decision, so you can check what was claimed. |
| **Narrow choices** | Only the options shown in the request are accepted. “Unlimited for the rest of the session” is **never** offered for budgets through relay; extensions are capped (`max_extensions`, default 3), after which only “allow once” remains. |
| **Denials stick** | A “no” cannot be retried around: cooldown after a denial, and the session is closed after `max_denials_per_session`. |
| **Sanitised prompt text** | Queries/URLs the agent controls are stripped of control characters and truncated before they are shown to you. |

**Honest limit:** with `relay`, the answer travels through the agent, so a *compromised or misbehaving* agent could claim you said yes. The measures above make that narrow, single-use and auditable (`web-mcp audit`), but not impossible. If you need a channel the agent cannot touch, add the client's own prompt as the first mode — `modes = ["elicitation", "relay"]` — or use `["deny"]` to make every limit hard.

| Mode | Behaviour |
|---|---|
| `relay` (default) | As above: the tool result tells the agent to ask you in the chat, then record your answer with `research_approve`. Works in every MCP client. |
| `elicitation` | If the client supports MCP elicitation, the **client itself** shows the question (in-app, not a separate window). Falls through to the next mode when unsupported. |
| `deny` | Always refuse. Terminal step. |

Choices you can give: **once** (this one action), **extend** (+`extend_factor` of the preset), **session** (allow this kind of *non-budget* action, e.g. a domain or an off-topic query, for the rest of the session), **deny**, **end** (deny and close the session). An optional terminal helper exists (`web-mcp approve`) to list or answer pending requests, but you never need it.

Tip for agent instructions: *“When web-mcp says APPROVAL REQUIRED, ask me, then call research_approve with my exact answer. Never answer on my behalf.”*

---

## 7. Settings reference

Config lookup order (later wins): defaults → `~/.config/web-mcp/config.toml` → `./.web-mcp.toml` → `$WEBMCP_CONFIG` → env vars `WEBMCP_<SECTION>__<KEY>` (values are JSON or plain strings, e.g. `WEBMCP_LIMITS__DAILY_SEARCHES=50`, `WEBMCP_DOMAINS__DENY='["x.com"]'`).
Unknown keys are **errors** (typos never silently disable a guardrail). Create a commented template with `web-mcp config init`; see [`config.example.toml`](config.example.toml). Inspect the effective values with `web-mcp config show`.

**API keys are never read from the config file** — use env vars (`BRAVE_API_KEY`, `TAVILY_API_KEY`, `SERPER_API_KEY`) or the OS keychain (`web-mcp key set brave`, needs the `keyring` extra).

### `[providers]`
| Key | Default | Description |
|---|---|---|
| `chain` | `["searxng","duckduckgo"]` | Providers to try in order. Known: `searxng`, `duckduckgo`, `brave`, `tavily`, `serper`. |
| `searxng_url` | `http://localhost:8888` | SearXNG base URL. |
| `timeout_seconds` | `12` | Per provider call. |
| `breaker_seconds` | `60` | A failing provider is skipped for this long. |
| `min_interval_seconds` | `0.5` | Minimum gap between calls to the same provider. |

### `[budget.quick|standard|deep]`
| Key | quick / standard / deep | Description |
|---|---|---|
| `searches` | 3 / 8 / 20 | Searches per session. |
| `fetches` | 3 / 10 / 25 | Page reads per session. |
| `output_tokens` | 12000 / 40000 / 120000 | Total text returned to the agent per session. |
| `minutes` | 5 / 15 / 40 | Session wall time. |

### `[limits]`
| Key | Default | Description |
|---|---|---|
| `daily_searches` / `daily_fetches` | 100 / 150 | Global daily caps (all sessions). |
| `max_sessions_per_hour` | 10 | More → user approval. |
| `max_concurrent_sessions` | 3 | More → user approval. |
| `max_results` | 10 | Hard cap on results per search. |
| `max_fetch_tokens` | 4000 | Hard cap per page read. |
| `max_query_chars` | 300 | Longer queries rejected. |
| `max_sub_questions` | 5 | Extra sub-questions dropped. |
| `per_domain_rps` | 1.0 | Requests/sec per website (burst 3). |

### `[session]`
| Key | Default | Description |
|---|---|---|
| `ttl_minutes` | 120 | Session lifetime. |
| `idle_minutes` | 10 | A session idle this long no longer counts as “concurrent”. |

### `[approval]`
| Key | Default | Description |
|---|---|---|
| `modes` | `["relay"]` | Channel order: `relay` (agent asks you in the chat), `elicitation` (client's own prompt), `deny`. |
| `timeout_seconds` | 55 | `elicitation` only: how long to wait for the user. Timeout = deny. Keep below your client's tool-call timeout. |
| `approval_ttl_seconds` | 600 | `relay`: how long a pending request / recorded answer stays valid. |
| `max_extensions` | 3 | Budget extensions per session; afterwards only “allow once” is offered. |
| `extend_factor` | 0.5 | “Extend” adds this fraction of the preset. |
| `max_denials_per_session` | 2 | Then the session is closed. |
| `denial_cooldown_seconds` | 60 | No re-prompt right after a denial. |

### `[policy]`
| Key | Default | Description |
|---|---|---|
| `drift_action` | `approve` | Off-topic query: `warn` \| `approve` \| `block`. |
| `drift_threshold` | 0.15 | Minimum share of query terms found in the plan vocabulary. Raise to be stricter. |
| `pii_action` | `warn` | `warn` \| `block` \| `approve`. |
| `secret_action` | `block` | `warn` \| `block`. |
| `allow_arbitrary_urls` | `false` | `true` lets the agent fetch any public URL without approval. |
| `respect_robots` | `true` | Obey robots.txt. |
| `strip_suspicious` | `false` | `true` drops (instead of flags) injection-like passages. |

### `[domains]` (patterns: `example.com` also matches subdomains; `*.gov` matches any `.gov`)
| Key | Default | Description |
|---|---|---|
| `deny` | `[]` | Never fetched, hidden from results. |
| `allow` | `[]` | Non-empty ⇒ **strict mode**: only these can be fetched. |
| `prefer` | docs.python.org, github.com, `*.gov`, `*.edu`, arxiv.org, wikipedia.org | Ranking boost. |
| `require_approval` | x.com, twitter.com, facebook.com, linkedin.com, instagram.com | Ask before reading. |

### `[fetch]`
| Key | Default | Description |
|---|---|---|
| `timeout_seconds` | 15 | Per request. |
| `max_bytes` | 5000000 | Body cap (larger is truncated). |
| `max_redirects` | 5 | Each hop is re-validated. |
| `max_pdf_pages` | 30 | Pages read from a PDF. |
| `allowed_ports` | `[80,443,8080,8443]` | Other ports are blocked. |
| `user_agent` | `web-mcp/0.1 (+local research agent)` | Honest UA. |
| `unsafe_allow_private` | `false` | **Disables SSRF protection.** Don't. |

### `[cache]`, `[state]`, `[audit]`
| Key | Default | Description |
|---|---|---|
| `cache.enabled` / `ttl_hours` / `path` | `true` / 24 / `~/.cache/web-mcp/cache.db` | Search results (shorter TTL for `day`/`week` freshness) and extracted pages. |
| `state.path` | `~/.local/state/web-mcp/state.db` | Sessions, counters, approvals. |
| `audit.enabled` / `path` | `true` / `~/.local/state/web-mcp/audit.jsonl` | Audit log. |

### Common recipes
```toml
# Paranoid: only official sources, never off-topic
[policy]
drift_action = "block"
[domains]
allow = ["docs.python.org", "*.gov", "arxiv.org", "github.com"]

# Hard limits: no questions asked, the agent just stops at the budget
[approval]
modes = ["deny"]     # hard limits: never ask, just stop

# Tight daily spend
[limits]
daily_searches = 30
daily_fetches = 40
```

---

## 8. Search providers

| Provider | Key | Freshness | Notes |
|---|---|---|---|
| `searxng` | none (self-host) | ✔ | Best privacy/quality without keys. `web-mcp searxng up` starts it in Docker, bound to `127.0.0.1:8888`, JSON enabled. Or point `searxng_url` at your own instance (enable `json` under `search.formats`). |
| `duckduckgo` | none | ✔ | Zero setup via the `ddgs` package. Unofficial; can be rate-limited or change. Good fallback. |
| `brave` | `BRAVE_API_KEY` | ✔ | Good quality, free tier. |
| `tavily` | `TAVILY_API_KEY` | ✔ | Built for LLM use. |
| `serper` | `SERPER_API_KEY` | ✔ | Google results. |

Failures, rate limits (429) and empty results fall through to the next provider; a failing provider is skipped for `breaker_seconds`; a provider with a missing/invalid key is disabled for the process. Provider failures are never charged to the budget.

---

## 9. CLI

| Command | Purpose |
|---|---|
| `web-mcp serve` | Run the MCP server on stdio (also the default). |
| `web-mcp doctor [--live]` | Diagnose dependencies, paths, providers, approval channels; `--live` runs a real search. |
| `web-mcp install-client <print\|claude-desktop\|claude-code\|cursor\|windsurf> [--remove]` | Register with a client (JSON configs are backed up to `.bak`). |
| `web-mcp approve [<id>] [--choice once\|extend\|session] [--deny]` | Optional: list / answer pending approvals from a terminal (normally the agent relays your answer). |
| `web-mcp audit [--today] [--hours N] [--json]` | Summarize the audit log. |
| `web-mcp config [show\|init\|path]` | Effective config / write template / list lookup paths. |
| `web-mcp key set\|delete <brave\|tavily\|serper>` | Manage API keys in the OS keychain. |
| `web-mcp searxng up\|down\|status` | Manage a local SearXNG container. |
| `web-mcp cache-clear` | Clear cached pages and searches. |

---

## 10. Client setup

`web-mcp install-client <client>` does this for you. Manual locations:

| Client | Where |
|---|---|
| Claude Desktop | `claude_desktop_config.json` → `mcpServers` |
| Claude Code | `claude mcp add web-mcp --scope user -- web-mcp serve` |
| Cursor | `~/.cursor/mcp.json` → `mcpServers` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` → `mcpServers` |
| VS Code | `.vscode/mcp.json` → `servers` (same `command`/`args`) |
| Cline / Roo / PostQode | MCP Servers panel → edit config → `mcpServers` |

Use the **absolute path** printed by `web-mcp install-client print` — GUI apps often don't inherit your shell PATH.

**Tip:** add to your agent's instructions: *“For anything that needs current information, use web-mcp: research_start first, then a few focused searches, then read only the best sources. When web-mcp says APPROVAL REQUIRED, ask me, then call research_approve with my exact answer — never answer for me.”*

---

## 11. Privacy & security notes

- **What leaves your machine:** search queries (to the provider you configured) and page requests (to the sites read). Queries are screened for secrets/PII/code first. The SearXNG option keeps queries off single-vendor accounts; API providers see your queries and key.
- **What is stored locally:** sessions/counters/approvals (`state.db`), extracted pages and search results (`cache.db`), audit log (JSONL, mode 0600). Clear with `web-mcp cache-clear` or delete the files.
- **Telemetry:** none.
- **Not covered:** the server can't stop a malicious *page* from being persuasive — it labels and flags, but the agent (and its other tools) still decide what to do. Don't give a web-reading agent tools that can exfiltrate data without confirmation.
- `unsafe_allow_private = true` and `allow_arbitrary_urls = true` weaken the main protections; leave them off unless you know why.

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| Agent says search is unavailable | `web-mcp doctor --live`. SearXNG down → DuckDuckGo fallback is used; if both fail check network / `pip show ddgs`. |
| SearXNG “JSON format disabled” | `web-mcp searxng up` (ships a settings file with JSON on), or add `json` to `search.formats`. |
| Client can't find `web-mcp` | Use the absolute path (`web-mcp install-client print`). Restart the client after editing its config. |
| Agent doesn't ask me, just stops | Its instructions may forbid questions. Tell it: “when web-mcp says APPROVAL REQUIRED, ask me, then call research_approve”. Or answer with `web-mcp approve <id>` in a terminal. |
| Agent keeps getting `BLOCKED: … off the declared topic` | Its queries don't use words from the plan. Start the session with better `sub_questions`, or set `drift_action = "warn"`. |
| Page “No readable text” | JS-only page, login wall or scan. Try another result. |
| `Unknown key` config error | A setting name is misspelled — see the reference above. |

---

## 13. Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest        # unit, engine, SSRF and stdio end-to-end tests (no network needed)
.venv/bin/ruff check src tests
```
Tests cover the guardrail matrix (SSRF encodings & redirects, leak detection, injection, budgets, approvals incl. CLI resume, session persistence) and assert the tool-list stays under a context budget. Adding a provider = one class with `async search(query, freshness, max_results, site)` registered in `providers/chain.py`.

License: [MIT](LICENSE)