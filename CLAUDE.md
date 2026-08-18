# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Quota Keeper** — a pair of Open WebUI plugin Functions that meter per-user token cost (web UI and direct API calls) and enforce credit quotas:

- `quota_keeper_filter.py` — **Filter Function**: `inlet()` blocks requests over quota, `stream()`/`outlet()` record token usage (cached/input/output/cache_write) converted to USD via a pricing table.
- `quota_keeper_admin.py` — **Event Function** (requires Open WebUI ≥ 0.10.0): on startup/enable registers `/api/v1/quota-keeper/*` admin API + `/quota` single-page UI (role-split: admin console vs personal card) onto `__app__`, and runs a background loop that refreshes the model pricing table.

`quota_keeper_README.md` (user-facing install/config guide, Chinese) and `quota_keeper_HANDOFF.md` (detailed dev handoff with algorithms, known limitations, and roadmap — **read it before non-trivial changes**) are the authoritative docs.

## Build / test / deploy

There is no build system, test suite, or dependency manifest in this repo — both `.py` files are **self-contained single files** pasted into Open WebUI's Admin Panel → Functions. "Deployment" = paste file contents into the Function editor and enable.

- Syntax check: `python -m py_compile quota_keeper_filter.py quota_keeper_admin.py`
- Logic can be unit-tested without Open WebUI by stubbing pydantic (`sys.modules['pydantic']` injection); regression cases for usage normalization, price matching, quota resolution, and time multipliers are listed in `quota_keeper_HANDOFF.md` §7.

## Critical constraint: duplicated shared code

Shared helpers (data dir, file locking, JSON cache, config schema, pricing fetch/match, quota resolution, time multiplier) are **intentionally duplicated verbatim in both files** — Open WebUI loads Functions as standalone single files with no cross-import. **Any change to shared logic must be applied identically to both files** (marked by the comment `==== shared helpers: keep in sync ====`). This is the #1 maintenance hazard.

## Architecture essentials

- **Data**: all state lives in `$DATA_DIR/quota_keeper/` (default `/app/backend/data/quota_keeper/`): `config.json` (authoritative schema = `DEFAULT_CONFIG`), `ledger.json` (usage, written by Filter), `pricing_cache.json` (written by Event), `recent.json` (last-200 ring buffer, written by Filter alongside the ledger). Writes are atomic (tmp + fsync + os.replace) under an fcntl flock; the Open WebUI database is never touched.
- **Quota resolution priority**: per-user quota > max of user's group quotas > default > unlimited. Effective quota = resolved quota × time multiplier (night and weekend multipliers multiply; night window may span midnight).
- **Cost model**: `credits_per_usd` (default 1000 credits = $1). Prices come from an upstream URL (LiteLLM flat format or models.dev nested format, auto-detected), cached per-1M-token. TOU (`config.tou`, see HANDOFF §5.7) multiplies the whole per-unit cost by the active tier's rate (peak/offpeak/normal windows + holidays; resolution models → providers → `default_policy`), orthogonal to quota multipliers; ledger records per-tier request counts and `cost_saved_usd`.
- **Price matching** (`qk_find_pricing`): override → exact → date-suffix-stripped exact → path suffix → path segment → longest substring (`contains`), with `.`↔`-` normalization. Unmatched models cost 0 and get flagged `priced=false` in the ledger (per-request `unpriced_requests` counter drives the flag).
- **API** (admin-only except `/me`): `GET/POST /config` (validate + deep-merge), `GET /users /groups /ledger`, `GET /pricing` (summary; `?full=1` for the editor), `GET /recent`, `GET /stats?from&to&user&model&granularity=hour|day` (server-side aggregation: KPI, stacked series, per-user/per-model rows), `POST /pricing/refresh`, `GET /pricing/match`; `GET /me` (`_require_user`, own-data-only) backs the non-admin card. Failures raise `HTTPException` (401/403) from the auth dependencies; routes are spliced ahead of OWUI's `spa-static-files` catch-all mount by `_mount_guard` (plain `include_router` appends land *after* it and are shadowed — v0.2.1 fix, prune pattern), which also drops stale same-prefix routes so hot code updates take effect without restart; the pricing loop task is tracked on `app.state` (cancelled and respawned on remount) and runs via `asyncio.create_task` + `asyncio.to_thread`.
- **Fail-open**: the Filter logs and passes through on unexpected errors; admins bypass enforcement by default; background tasks (title/tag generation) are never blocked but still metered.
- **Dedup**: usage is recorded once per response id (`_seen` OrderedDict, capped 4096); orphan adoption in outlet is unconditional when the user is present.

## Known sharp edges (see HANDOFF §8 for full list)

- Route mounting is verified on a real instance (main-slim build); the Filter's runtime shapes (`__user__`/`__metadata__`, stream event formats) are still unverified live and vary by connector.
- Routes under a *changed* `route_prefix`/`api_prefix` valve linger until restart (stale cleanup can only find current-prefix paths) — valve descriptions say so (HANDOFF §8.15).
- TOU topups are additive — a cumulative-style usage reporter would double-count tokens/cost (HANDOFF §8.13).
- Only cost/credits KPI cards carry sparklines (4 of 6 do not); `/me` `tou.current_tier` is a reserved null field (HANDOFF §8.16/17).
- The lock degrades to a process-local `threading.Lock` without fcntl (Windows + multi-worker loses writes).
- `contains` price matching can misfire between model families with very different prices — check the `how` field when debugging cost anomalies.

## Conventions

- Logging: `log.warning`/`log.info` with `quota-keeper` prefix.
- Code and docs are primarily in Chinese in the docs; code comments/docstrings are in English.
