# Quota Keeper for Open WebUI

> **Per-user token metering + credit quota enforcement for Open WebUI** - covers web UI chats *and* direct API calls, with cached/input/output token breakdown, USD cost accounting, and a built-in `/quota` admin dashboard.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![OpenWebUI](https://img.shields.io/badge/OpenWebUI-Function-blue)](https://github.com/open-webui/open-webui)
[![Python](https://img.shields.io/badge/Python-3.10+-3776ab)](https://www.python.org)

---

## 🌟 Features

- 📊 **Full-path Metering**: records `cached / input / output / cache_write` tokens for every chat completion - both streaming and non-streaming, from the web UI **and** direct API clients (curl / OpenCode / Continue.dev…), which native Analytics misses.
- 🚦 **Quota Enforcement**: blocks requests once a user's credit usage exceeds their quota. Resolution priority: `user quota > max of group quotas > default > unlimited`.
- 💰 **Real-cost Accounting**: token usage is priced against an auto-fetched pricing table (LiteLLM `model_prices_and_context_window.json` or models.dev format) and converted to credits (`credits_per_usd`, default 1000 credits = $1).
- 🔍 **Fuzzy Price Matching**: `override -> exact -> date-suffix-stripped -> path suffix -> tail segment -> contains` with `.`/`-` normalization, so `openai/gpt-4o` and `gpt-4o-2024-08-06` both resolve. A "Test match" box in the admin UI previews every match.
- 🌙 **Time-based Multipliers**: night window (may span midnight) and weekend multipliers shrink/expand the effective quota on schedule.
- 🛡 **Fail-open by Design**: metering or enforcement errors never break conversations; admins can bypass enforcement; background tasks (title/tag generation) pass through but are still metered.
- 🗄 **No Database Touch**: all state lives in `$DATA_DIR/quota_keeper/` (`config.json` / `ledger.json` / `pricing_cache.json`) - atomic writes under an `flock`, the Open WebUI DB is never modified.

## 🏛 Architecture

```
Open WebUI instance
├── Filter (quota_keeper_filter.py)      ← attach to models, or Global
│   ├── inlet()   enforcement point: resolve quota × time multiplier,
│   │             check period usage, raise QuotaBlocked when exceeded
│   ├── stream()  streaming: read usage from the terminal SSE chunk
│   └── outlet()  non-streaming: read body usage; orphan adoption
├── Event  (quota_keeper_admin.py)       ← requires Open WebUI ≥ 0.10.0
│   ├── mounts /api/v1/quota-keeper/*    (admin-only JSON API)
│   ├── mounts /quota                    (single-page admin UI)
│   └── background loop refreshing the pricing table
└── $DATA_DIR/quota_keeper/
    ├── config.json         all settings (authored by the admin page)
    ├── ledger.json         usage ledger (written by the Filter)
    └── pricing_cache.json  pricing table cache (written by the Event)
```

## 📦 Install

1. Open WebUI -> Admin Panel -> Functions -> "+", paste each file's contents, save.
2. Enable **both** Functions.
3. Attach the Filter to the models you want metered (Admin Panel -> Settings -> Models -> *model* -> Filters -> Quota Keeper), or flip its **Global** switch.
4. The Event Function requires Open WebUI >= 0.10.0 (Event primitive).

After enabling, open `https://<your-instance>/quota` for the admin page (admin session required).

## ⚙️ Quota Resolution

```
per-user quota  >  max of the user's group quotas  >  default quota  >  unlimited
```

- Each user row can override individually (empty = inherit).
- Among groups the highest quota wins.
- Effective quota = resolved quota × time multiplier (night × weekend multiply; night window may span midnight, default 22:00-08:00).

## 💳 Billing

- Cost splits tokens into `cached / input / output / cache_write`, all counted into cost (USD).
- `credits_per_usd` defaults to 1000 (1000 credits = $1).
- Pricing source defaults to LiteLLM's `model_prices_and_context_window.json` (per-token prices auto-converted to per-1M); models.dev nested format is auto-detected too. URL is configurable.
- Unmatched models cost 0 and get an `unpriced` flag in the usage table unless fallback pricing is configured.

## 🔒 Data & Safety

- Everything is stored under `$DATA_DIR/quota_keeper/`, written atomically under a file lock; the Open WebUI database is untouched.
- APIs mount at `/api/v1/quota-keeper/*`, the page at `/quota` (both prefixes configurable via Event Valves), all requiring an admin session.
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

Core logic implemented and unit-tested against stubs; **not yet validated against a live Open WebUI instance** - `__user__`/`__metadata__` shapes and stream event formats vary by connector. See [quota_keeper_HANDOFF.md](./quota_keeper_HANDOFF.md) for the full algorithm reference, known limitations and roadmap (Chinese).

## 📄 License

[MIT](./LICENSE)
