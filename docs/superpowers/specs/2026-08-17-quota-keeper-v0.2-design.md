# Quota Keeper v0.2.0 Design

> Date: 2026-08-17 · Status: approved in brainstorming + spec review
> Scope: P0/P1 defect fixes + admin dashboard + per-user self-service + pricing overrides UI + DeepSeek-style time-of-use (TOU) tiered pricing.

## 0. Background

v0.1.1 core algorithms (usage normalization, price matching, quota resolution, time multipliers) are correct, but a max-effort code review found the integration layer broken or unsafe (15 verified findings). This release fixes those and adds the features requested by the admin:

1. Fix the out-of-the-box failures (unauthenticated APIs, admin page 404s) and metering-loss defects.
2. Admin dashboard: KPI cards, time spans, trends, sortable/filterable user & model tables, CSV export, cache rate.
3. Normal users see their own quota at `/quota` (role-split page backed by a `GET /me` API).
4. Pricing: manual per-model overrides via editable table (showing fuzzy-matched target name).
5. DeepSeek-V4-style peak/off-peak (TOU) tiered pricing, configured per provider with per-model overrides, holidays included.

## 1. P0/P1 Fixes (foundation)

### 1.1 Authentication

- `_require_admin` / new `_require_user`: on failure **raise `HTTPException`** (401 auth failed, 403 non-admin). Never return a JSONResponse from a dependency.
- Router keeps `Depends(_require_admin)`. The `/quota` page route uses the same dependency (drop the `isinstance(guard, ...)` pattern).
- `GET /me` (see §3) uses `_require_user`.

### 1.2 Route prefix

- Route paths drop the `/quota-keeper` segment (mounted paths become `/api/v1/quota-keeper/*`, matching the page).
- `QK_PAGE` gets a `__QK_API_PREFIX__` placeholder, substituted with the actual `api_prefix` valve at mount time. Page JS no longer hardcodes the base URL. (Changing the valve keeps the page working.)

### 1.3 Metering correctness

- `_record` rework: `_mark_seen` is called only when usage is actually recorded. When `__user__` is absent, stash into `_orphan` **without** consuming a seen slot.
- `outlet`:
  - user present + usage in body → record (stream phase never blocked the id anymore);
  - user present + no usage → adopt orphan by rid **unconditionally** (no longer gated on `estimate_unreported_tokens`);
  - estimation fallback stays valve-gated and additionally requires the record to be plausible (see dead-code note: response bodies have no `messages`, so estimation uses `choices[0].message.content` when present, else is skipped).
- `stream`: pre-filter SSE text with a cheap `'"usage"'` substring check before `json.loads`.
- `block_message.format()` wrapped: on failure fall back to the default template + `log.warning` (never fail-open due to template).
- `inlet`: `eff <= 0` with a resolved quota now **blocks** (semantic fix), booleans are rejected as quota/multiplier values (`isinstance(v, bool)` excluded before numeric checks).
- `qk_record_usage`: `priced` replaced by `unpriced_requests` counter (self-healing per day/model); retention cutoff computed in config timezone.
- Anthropic-native SSE shapes (`message_start` → `ev["message"]["usage"]`, `message_delta` → partial usage) normalized via a merge step: consecutive per-id partial usages are merged (input side from the first event, output side from later events) instead of dedup-dropped.

### 1.4 Config API

- `POST /config`: schema validation (types, ranges) → 400 on violation; deep-merge into the **on-disk config** (partial POSTs no longer reset sibling keys).
- JS `saveConfig`: explicit `isNaN` checks; `0` is a valid, saveable value (hours, multipliers, refresh_hours).

### 1.5 Runtime robustness

- Router mount dedup: check `__app__.routes` for the prefix before `include_router` (same protection the page route has).
- `_pricing_loop`: started via `asyncio.create_task`, strong reference stored on the Event instance; `requests.get` wrapped in `asyncio.to_thread` (event loop never blocks on pricing fetch, both in the loop and in the manual refresh endpoint).
- `_JsonCache`: also keyed on file size (mtime granularity workaround).
- Sync-with-shared-algorithm note: every change in this section that touches shared helpers is applied identically to both files (the filter gains the missing "keep in sync" marker comment).

## 2. Admin Dashboard (admin view of `/quota`)

### 2.1 New endpoints

- `GET /stats?from&to&user&model&granularity=hour|day` — server-side aggregation over the ledger: KPI totals (requests, tokens, cost USD, credits, cache rate, unpriced requests), per-granularity time series (stackable by model), per-user table rows, per-model table rows (incl. blended $/M), with filters. Response is compact JSON; the page stops fetching the raw ledger and the raw pricing table.
- `GET /pricing` no longer returns the full table by default (`?full=1` for the editor view).

### 2.2 Page sections (admin)

1. **KPI cards ×6** with 7-day sparklines (hand-written SVG, no external chart lib, following Open WebUI's own analytics approach): requests / tokens / cost $ / credits / cache rate `cached/(cached+input)` / unpriced requests.
2. **Time span selector**: 24h · 7d · 30d · 90d · custom (persisted in localStorage); ≤24h uses hour granularity when present, else falls back to day totals.
3. **Trend chart**: stacked cost/tokens per bucket, by model, ≤8 colors + "Others".
4. **Users ranking table** (Open WebUI analytics style): search by username/email, sortable and re-rankable columns (requests/tokens/cost/credits/quota %) - ranking by requests, by tokens, or by cost at the admin's click; quota progress bar computed **server-side** (resolved quota × current multiplier vs used - eliminates the browser-TZ and multiplier drift), row click drills into that user's per-model detail.
5. **Models table**: model / requests / users / token split / cost / blended $/M / unpriced flag / **fuzzy-match target name** (from `how`).
6. **Filters**: user / model / date range, linked; **CSV export** generated client-side from the aggregated stats response.

### 2.4 Recent activity log (admin)

- New rolling record file `$DATA_DIR/quota_keeper/recent.json`: a bounded ring buffer of the **last 200 metered responses**, appended at metering time under the existing lock: `{ts, user_id, name, email, model, tokens{cached,input,output}, cost_usd, tou_tier, priced}`. Oldest entries are dropped; file stays in the KB range; no indexing, no per-request history beyond 200.
- `GET /recent` (admin): returns the buffer newest-first. Page section "Recent activity" renders username + model + input/output/cached + cache% + cost + tier; manual refresh only.
- Normal users' `/me` does **not** expose this (their view shows only their own aggregates).

### 2.5 Resource policy (server is already heavily loaded)

- **No auto-refresh anywhere**: no `setInterval`/polling in the page. All dashboard data loads on page open and on explicit manual refresh (button) or filter change. The 10s-auto-refresh pattern of cpa-usage-keeper is explicitly rejected.
- Aggregation happens **server-side in `/stats`** on demand (only when an admin asks); the page never downloads the raw ledger or the raw pricing table (pricing editor uses `?full=1` explicitly).
- `recent.json` is capped (200 entries) so per-response writes stay O(1) small-file writes; ledger day/hour aggregates remain bounded by retention pruning.
- The pricing background loop keeps its 600s idle check (no work when cache is fresh); pricing fetch runs in a worker thread (`asyncio.to_thread`) so it never blocks the event loop.
- No new long-lived in-memory caches beyond the existing `_JsonCache` (which gains size-keyed invalidation only); no chart library (hand-written SVG); CSV export is generated client-side.

- `days.<d>.hours.<H>` = `{requests, cost_usd, tokens{cached,input,output}}` (per user only, not per model — bounded size).

_This is the “2.3 Ledger schema additions” block (renumbered: 2.3 recent-activity, 2.4 resource policy)._
- Day/model records gain `unpriced_requests`, `tou_tier` counters (`peak/offpeak/normal` request counts), `cost_saved_usd` (discount vs normal rate).
- Old ledgers without these fields keep working (all readers treat missing keys as 0/absent).

## 3. Per-User Self-Service (`/quota` role split + `/me`)

- `GET /me` (`_require_user`): returns the caller's resolved quota + source (user/group/default/none), current time multiplier, effective quota, period used/remaining, today/month token split & cost, 7-day trend. Server enforces uid = session user (no cross-user access).
- `/quota` page: fetch `/me` first; `role === 'admin'` renders the full admin console; otherwise renders a personal card (quota, progress bar, multiplier notice, usage detail, 7-day trend). Single URL for both roles.

## 4. Pricing Overrides Editor

- Admin page "Pricing" section: searchable, paginated table of the cached pricing table. Each row shows the entry key (the **fuzzy-match target**), current per-1M prices (input/cached/cache_write/output), and inline editing.
- Saves go to `pricing.overrides` (highest match priority, never overwritten by upstream refresh). Rows sourced from overrides are marked with a "manual" badge.
- Usage tables and the existing Test-match box display the matched target (`how` field already carries it).

## 5. Time-of-Use (TOU) Tiered Pricing — DeepSeek V4 style

Config (new top-level key `tou`):

```jsonc
{
  "tou": {
    "enabled": false,
    "timezone": null,                 // null -> schedule.timezone
    "tiers": {
      "peak":    { "rate": 2.0, "windows": [ {"days":[1,2,3,4,5], "start":"09:00","end":"12:00"},
                                             {"days":[1,2,3,4,5], "start":"14:00","end":"18:00"} ] },
      "offpeak": { "rate": 0.5, "windows": [ {"days":[0,1,2,3,4,5,6], "start":"00:30","end":"08:30"} ] },
      "normal":  { "rate": 1.0 }
    },
    "holidays": [],                   // "YYYY-MM-DD" -> whole day forced offpeak
    "default_policy": "off",          // models matching no provider rule: off | normal
    "providers": {                    // provider = first path segment of model id; bare names -> "_default"
      "deepseek":  { "enabled": true },
      "anthropic": { "enabled": true, "tiers": { "offpeak": { "rate": 0.6 } } }
    },
    "models": {                       // exact model id override, highest priority
      "openai/gpt-4o": { "enabled": true, "tiers": { "peak": { "rate": 1.5 } } }
    }
  }
}
```

Semantics:

- Evaluated at metering time (`qk_record_usage`) for every response. Resolution order: `models[exact id]` → `providers[first segment]` → `default_policy`.
- Window matching: per tier, list of windows `{days (weekday numbers), start, end}`; windows may span midnight; a hit ⇒ the tier's `rate` multiplies the **entire** unit price (cached + input + cache_write + output), DeepSeek-style.
- Holidays: any date in `holidays` forces `offpeak` for the whole day (when offpeak exists; else the lowest-rate tier).
- Independent of the existing `schedule` quota multipliers (those change quota caps; TOU changes prices).
- Ledger: each day/model records `tou_tier` request counts and `cost_saved_usd` (normal-rate cost − actual cost), auditable.
- Admin UI: TOU section with global enable, tier editor (rate + windows with weekday picker), provider list with enable/override, model overrides, holiday list with **one-click fetch from date.nager.at** (`GET /api/v3/PublicHolidays/{year}/{CC}`, no key, no dependency; country code + year selectable; failures degrade gracefully with a toast).
- `/me` and dashboard show the user's current tier ("当前闲时 5 折").

## 6. Testing

- Same stub-pydantic harness as v0.1.1 (documented in HANDOFF §7), extended cases:
  - auth: dependency raises 401/403 (fastapi TestClient, empirical pattern from the review);
  - prefix: mounted paths exactly `/api/v1/quota-keeper/*`; page placeholder substitution;
  - orphan adoption: stream-no-user → outlet-user-with-usage records exactly once; outlet-user-no-usage adopts;
  - block_message fallback on malformed template; `eff<=0` blocks; booleans rejected;
  - config validation/deep-merge (partial POST preserves siblings);
  - TOU: weekday windows, midnight-spanning, holiday override, provider/model resolution order, rate applied to all token fields, `cost_saved_usd`;
  - stats aggregation: hour/day granularity, filters, cache rate, quota progress numbers;
  - `/me`: own-data-only enforcement;
  - regression: all v0.1.1 HANDOFF §5/§7 cases still pass.

## 7. Compatibility & Release

- Ledger/config are read additively (missing keys = defaults); no migration step.
- Version bump to 0.2.0 in both file headers; `required_open_webui_version` unchanged.
- Docs updated (README + HANDOFF) after implementation; pushed to GitHub `main` on completion.
