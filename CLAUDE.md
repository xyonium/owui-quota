# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Quota Keeper** — a pair of Open WebUI plugin Functions that meter per-user token cost (web UI and direct API calls) and enforce credit quotas:

- `quota_keeper_filter.py` — **Filter Function**: `inlet()` blocks requests over quota, `stream()`/`outlet()` record token usage (cached/input/output/cache_write) converted to USD via a pricing table.
- `quota_keeper_admin.py` — **Event Function** (requires Open WebUI ≥ 0.10.0): on startup/enable registers `/api/v1/quota-keeper/*` admin API + `/quota` single-page admin UI onto `__app__`, and runs a background loop that refreshes the model pricing table.

`quota_keeper_README.md` (user-facing install/config guide, Chinese) and `quota_keeper_HANDOFF.md` (detailed dev handoff with algorithms, known limitations, and roadmap — **read it before non-trivial changes**) are the authoritative docs.

## Build / test / deploy

There is no build system, test suite, or dependency manifest in this repo — both `.py` files are **self-contained single files** pasted into Open WebUI's Admin Panel → Functions. "Deployment" = paste file contents into the Function editor and enable.

- Syntax check: `python -m py_compile quota_keeper_filter.py quota_keeper_admin.py`
- Logic can be unit-tested without Open WebUI by stubbing pydantic (`sys.modules['pydantic']` injection); regression cases for usage normalization, price matching, quota resolution, and time multipliers are listed in `quota_keeper_HANDOFF.md` §7.

## Critical constraint: duplicated shared code

Shared helpers (data dir, file locking, JSON cache, config schema, pricing fetch/match, quota resolution, time multiplier) are **intentionally duplicated verbatim in both files** — Open WebUI loads Functions as standalone single files with no cross-import. **Any change to shared logic must be applied identically to both files** (marked by the comment `==== shared helpers: keep in sync ====`). This is the #1 maintenance hazard.

## Architecture essentials

- **Data**: all state lives in `$DATA_DIR/quota_keeper/` (default `/app/backend/data/quota_keeper/`): `config.json` (authoritative schema = `DEFAULT_CONFIG`), `ledger.json` (usage, written by Filter), `pricing_cache.json` (written by Event). Writes are atomic (tmp + fsync + os.replace) under an fcntl flock; the Open WebUI database is never touched.
- **Quota resolution priority**: per-user quota > max of user's group quotas > default > unlimited. Effective quota = resolved quota × time multiplier (night and weekend multipliers multiply; night window may span midnight).
- **Cost model**: `credits_per_usd` (default 1000 credits = $1). Prices come from an upstream URL (LiteLLM flat format or models.dev nested format, auto-detected), cached per-1M-token.
- **Price matching** (`qk_find_pricing`): override → exact → date-suffix-stripped exact → path suffix → path segment → longest substring (`contains`), with `.`↔`-` normalization. Unmatched models cost 0 and get flagged `priced=false` in the ledger.
- **Fail-open**: the Filter logs and passes through on unexpected errors; admins bypass enforcement by default; background tasks (title/tag generation) are never blocked but still metered.
- **Dedup**: usage is recorded once per response id (`_seen` OrderedDict, capped 4096).

## Known sharp edges (see HANDOFF §8 for full list)

- Not yet verified against a real Open WebUI instance — `__user__`/`__metadata__` shapes and stream event formats vary by connector.
- Function hot-reload can double-register the API router (page route has a dup check, API does not).
- The lock degrades to a process-local `threading.Lock` without fcntl (Windows + multi-worker loses writes).
- `contains` price matching can misfire between model families with very different prices — check the `how` field when debugging cost anomalies.

## Conventions

- Logging: `log.warning`/`log.info` with `quota-keeper` prefix.
- Code and docs are primarily in Chinese in the docs; code comments/docstrings are in English.
