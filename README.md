# Quota Keeper for Open WebUI

> **Per-user token metering + credit quota enforcement for Open WebUI** - covers web UI chats *and* direct API calls, with cached/input/output token breakdown, USD cost accounting, and a built-in `/quota` admin dashboard.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![OpenWebUI](https://img.shields.io/badge/OpenWebUI-Function-blue)](https://github.com/open-webui/open-webui)
[![Python](https://img.shields.io/badge/Python-3.10+-3776ab)](https://www.python.org)

---

## 🌟 Features

- 📊 **Full-path Metering**: records `cached / input / output / cache_write` tokens for every chat completion - both streaming and non-streaming, from the web UI **and** direct API clients (curl / OpenCode / Continue.dev…), which native Analytics misses. A passthrough ingestion middleware additionally meters Open WebUI's pure-proxy endpoints (`/api/v1/messages`, `/openai/responses`) that never touch the Filter.
- 🚦 **Quota Enforcement**: blocks requests once a user's credit usage exceeds their quota. Resolution priority: `user quota > max of group quotas > default > unlimited`, over a `daily / weekly / monthly` period.
- 💰 **Real-cost Accounting**: token usage is priced against an auto-fetched pricing table (LiteLLM `model_prices_and_context_window.json` or models.dev format) and converted to credits (`credits_per_usd`, default 1000 credits = $1).
- 🔍 **Fuzzy Price Matching**: `override -> exact -> date-suffix-stripped -> path suffix -> tail segment -> contains` with `.`/`-` normalization, so `openai/gpt-4o` and `gpt-4o-2024-08-06` both resolve. A "Test match" box in the admin UI previews every match.
- 🌙 **Time-of-Use (TOU) Pricing**: optional peak/offpeak/normal tiers multiply the per-request price by a schedule (weekday windows that may span midnight, plus holidays), DeepSeek-style - independent of the quota ceiling.
- 🛡 **Fail-open by Design**: metering or enforcement errors never break conversations; admins can bypass enforcement; background tasks (title/tag generation) pass through but are still metered.
- 🗄 **No Database Touch**: all state lives in `$DATA_DIR/quota_keeper/` (`config.json` / `ledger.json` / `pricing_cache.json` / `recent.json`) - atomic writes under an `flock`, the Open WebUI DB is never modified.

## 🏛 Architecture

```
Open WebUI instance
├── Filter (quota_keeper_filter.py)      ← attach to models, or Global
│   ├── inlet()   enforcement point: resolve the effective quota,
│   │             check period usage, raise QuotaBlocked when exceeded
│   ├── stream()  streaming: read usage from the terminal SSE chunk
│   └── outlet()  non-streaming: read body usage; orphan adoption
├── Event  (quota_keeper_admin.py)       ← requires Open WebUI ≥ 0.10.0
│   ├── mounts /api/v1/quota-keeper/*    (JSON API; admin-only except /me)
│   ├── mounts /quota                    (admin console + self-service card)
│   ├── passthrough middleware metering /api/v1/messages & /openai/responses
│   └── background loop refreshing the pricing table
└── $DATA_DIR/quota_keeper/
    ├── config.json         all settings (authored by the admin page)
    ├── ledger.json         usage ledger (written by the Filter + middleware)
    ├── pricing_cache.json  pricing table cache (written by the Event)
    └── recent.json         last-200 ring buffer feeding the dashboard
```

## 📦 Install

1. Open WebUI -> Admin Panel -> Functions -> "+", paste each file's contents, save.
2. Enable **both** Functions.
3. Attach the Filter to the models you want metered (Admin Panel -> Settings -> Models -> *model* -> Filters -> Quota Keeper), or flip its **Global** switch.
4. The Event Function requires Open WebUI >= 0.10.0 (Event primitive).

After enabling, open `https://<your-instance>/quota` (any signed-in session): admins get the full console, regular users see a self-service card backed by `GET /me` (own data only, enforced server-side).

## ⚙️ Quota Resolution

```
per-user quota  >  max of the user's group quotas  >  default quota  >  unlimited
```

- Each user row can override individually (empty = inherit).
- Among groups the highest quota wins.
- Usage is summed over the configured `quota_period` (`daily` / `weekly` / `monthly`), day-bucketed in the `schedule.timezone` (default `Asia/Shanghai`).

## 💳 Billing

- Cost splits tokens into `cached / input / output / cache_write`, all counted into cost (USD).
- `credits_per_usd` defaults to 1000 (1000 credits = $1).
- Pricing source defaults to LiteLLM's `model_prices_and_context_window.json` (per-token prices auto-converted to per-1M); models.dev nested format is auto-detected too. One URL or a list (merged in order, first source wins on conflicts).
- Per-model **overrides** (`pricing.overrides`) take top priority and survive refreshes: set manual prices, or `{"alias": "<model>", "multiplier": m}` to reuse another model's price scaled by `m`.
- Unmatched models cost 0 and get an `unpriced` flag in the usage table unless fallback pricing (`pricing.default_pricing`) is configured; the admin console can **reprice** them retroactively once a price exists.

## 🌙 Time-of-Use (TOU) Pricing

- Enable via `tou.enabled`; tiers `peak / offpeak / normal` each carry a `rate` and time `windows` (JS weekday numbering `0=Sunday..6=Saturday`, `HH:MM`, may span midnight). `holidays` force a whole day to offpeak.
- Resolution: `models[exact]` → `models[*glob*]` → `providers[first path segment]` → `default_policy` (`off` recommended).
- The matched tier's `rate` multiplies the whole per-request cost (cached+input+cache_write+output); the ledger records per-tier request counts and `cost_saved_usd`. This replaces the removed night/weekend quota multipliers (v0.4.0) - TOU scales price, not the quota ceiling.

## 🔒 Data & Safety

- Everything is stored under `$DATA_DIR/quota_keeper/`, written atomically under a file lock; the Open WebUI database is untouched.
- APIs mount at `/api/v1/quota-keeper/*`, the page at `/quota` (both prefixes configurable via Event Valves). All endpoints require a signed-in session; admin-only routes enforce `role=admin`, while `/me`, `/recent`, `/stats`, `/models` self-scope to the caller's own data for non-admins.
- The Filter is fail-open by default (metering/query errors never block chat), admins are exempt by default, background tasks pass but are metered.
- Streaming responses are read at the terminal chunk (both OpenAI-style `prompt_tokens_details.cached_tokens` and Anthropic-style `cache_read_input_tokens` are supported); per-response-id dedup prevents double counting.

## 🎛 Main Valves (Filter)

| Valve | Default | Description |
|-------|---------|-------------|
| `enable_enforcement` | true | off = meter only, never block |
| `admins_bypass` | true | admins skip checks |
| `allow_background_tasks` | true | background tasks pass (still metered) |
| `estimate_unreported_tokens` | false | estimate tokens from chars/4 when upstream reports none |
| `block_message` | … | block notice template with `{used} {quota} {source} {mult}` placeholders |

## ⚠️ Status

Core logic is unit-tested (130 cases against stubs, `python3 -m pytest tests/`) and the route-mounting/auth chain plus passthrough ingestion have been validated on a live Open WebUI instance (0.11). Remaining runtime shapes (`__metadata__.task`, per-connector stream event formats) still vary by connector - see [docs/quota_keeper_HANDOFF.md](./docs/quota_keeper_HANDOFF.md) for the full algorithm reference, known limitations and roadmap (Chinese).

## 📄 License

[MIT](./LICENSE)
