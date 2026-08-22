# Quota Keeper v0.2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 15 verified v0.1.1 defects (unauthenticated APIs, 404 admin page, metering loss) and add: admin dashboard with ranking/recent-log, per-user `/me`, pricing overrides editor, and DeepSeek-style TOU tiered pricing.

**Architecture:** Two self-contained Open WebUI Function files (Filter meters/enforces, Event serves admin page/API). Shared helpers are duplicated verbatim in both files — every shared change is applied identically. Data stays in `$DATA_DIR/quota_keeper/*.json` with atomic writes under flock. New: `recent.json` ring buffer (200 entries), server-side `/stats` aggregation, `tou` config with tiered rate applied at metering time.

**Tech Stack:** Python 3.10+, FastAPI (host provides), no new runtime deps; pytest harness stubbing pydantic/open_webui; vanilla JS + hand-written SVG in the embedded page (no chart lib, no auto-refresh).

## Global Constraints

- Both `.py` files remain single-file self-contained; shared helpers identical in both (project CLAUDE.md `==== shared helpers: keep in sync ====`). The filter file gains the marker comment.
- All writes: tmp + fsync + `os.replace` inside `qk_lock()`.
- Fail-open for chat flow (meter/enforcement errors never break conversations), except `QuotaBlocked` and config-API 400s.
- No auto-refresh/polling in the page. No full ledger/pricing payloads to the browser (editor uses `?full=1`).
- Version strings: `version: 0.2.0` in both file headers. Ledger/config read additively (missing keys = defaults); no migration.
- Log lines carry the `quota-keeper` prefix. Comments in English.
- Commits: conventional style, `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

### Task 1: Test harness (conftest + module loaders)

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/__init__.py` (empty)

**Interfaces:**
- Produces: fixtures `load_filter(tmp_path)` / `load_admin(tmp_path)` returning the loaded module with `QK_DIR` pointed at `tmp_path` (via `DATA_DIR` env), stubbing `pydantic` before import. Helper `write_json(path, obj)`.

- [ ] **Step 1: Write conftest**

```python
# tests/conftest.py
import importlib.util, json, sys, types
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parent.parent
FILTER = REPO / "quota_keeper_filter.py"
ADMIN = REPO / "quota_keeper_admin.py"


def _stub_pydantic():
    m = types.ModuleType("pydantic")

    class Field:
        def __init__(self, default=None, description=""):
            self.default, self.description = default, description

    class BaseModel:
        def __init__(self, **kw):
            ann = {}
            for klass in type(self).__mro__:
                ann.update(getattr(klass, "__annotations__", {}))
            for k in ann:
                d = getattr(type(self), k, None)
                setattr(self, k, kw.pop(k, d.default if isinstance(d, Field) else d))

    m.BaseModel, m.Field = BaseModel, Field
    sys.modules["pydantic"] = m


def _load(path, name):
    if "pydantic" not in sys.modules:
        _stub_pydantic()
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture
def load_filter(tmp_path, monkeypatch):
    def _():
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        return _load(FILTER, "qk_filter")
    return _


@pytest.fixture
def load_admin(tmp_path, monkeypatch):
    def _():
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        return _load(ADMIN, "qk_admin")
    return _


@pytest.fixture
def qk(tmp_path, load_filter):
    return load_filter()
```

- [ ] **Step 2: Write smoke test**

```python
# tests/test_smoke.py
from conftest import write_json


def test_modules_load(qk, load_admin):
    adm = load_admin()
    assert callable(qk.qk_record_usage)
    assert callable(adm.qk_fetch_pricing)
```

- [ ] **Step 3: Run**

Run: `cd <repo-root> && python3 -m pytest tests/test_smoke.py -v`
Expected: PASS (2 modules imported). If fastapi/pydantic real packages interfere, ensure `_stub_pydantic` runs first (it does — loaders call it when absent).

- [ ] **Step 4: Commit**

```bash
git add tests/ && git commit -m "test: pytest harness loading both plugin modules with stubbed pydantic

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Shared-block sync + auth fixes + route prefix fix (P0)

**Files:**
- Modify: `quota_keeper_admin.py` (auth `_require_admin`/`_require_user`, route paths, page placeholder, mount dedup)
- Modify: `quota_keeper_filter.py` (add `# ==== shared helpers: keep in sync with quota_keeper_admin.py (same code) ====` marker above `def qk_data_dir`)
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Produces: `_require_user` raising `HTTPException`; routes mounted at `/api/v1/quota-keeper/*` (single segment); `QK_PAGE` contains `__QK_API_PREFIX__` replaced at mount; `_mount_guard(app, prefix)` idempotent helper.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_admin_api.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from conftest import write_json


def _app(load_admin):
    adm = load_admin()
    app = FastAPI()
    app.include_router(adm.qk_router, prefix="/api/v1/quota-keeper")
    return app, adm


def test_routes_single_prefix(load_admin):
    app, _ = _app(load_admin)
    paths = {r.path for r in app.routes}
    assert "/api/v1/quota-keeper/config" in paths
    assert "/api/v1/quota-keeper/quota-keeper/config" not in paths


def test_admin_endpoints_unauthenticated_401(load_admin):
    app, _ = _app(load_admin)
    c = TestClient(app)
    assert c.get("/api/v1/quota-keeper/ledger").status_code == 401
    assert c.post("/api/v1/quota-keeper/config", json={}).status_code == 401


def test_page_placeholder_substituted(load_admin):
    adm = load_admin()
    assert "__QK_API_PREFIX__" not in adm.QK_PAGE  # substituted at module level? No: at mount.
```

The third test documents that substitution happens at mount: adjust to check `adm.qk_build_page("/api/v1/quota-keeper")` returns HTML containing that prefix and no placeholder.

- [ ] **Step 2: Run tests — expect FAIL** (routes have double prefix; unauth returns 200)

Run: `python3 -m pytest tests/test_admin_api.py -v`

- [ ] **Step 3: Implement**

In `quota_keeper_admin.py` replace `_require_admin` with:

```python
from fastapi import HTTPException


async def _require_user(request: Request):
    try:
        from open_webui.utils.auth import get_verified_user
        user = await get_verified_user(request)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"auth failed: {e}")
    return user


async def _require_admin(request: Request):
    user = await _require_user(request)
    if (getattr(user, "role", "") or "") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user
```

Route decorators: strip the leading `/quota-keeper` from every path in `qk_router` (`"/config"`, `"/users"`, `"/groups"`, `"/ledger"`, `"/pricing"`, `"/pricing/refresh"`, `"/pricing/match"`, plus new ones below). Replace page mount block:

```python
def qk_build_page(api_prefix: str) -> str:
    return QK_PAGE.replace("__QK_API_PREFIX__", api_prefix)
```

and in `event()`:

```python
if not any(getattr(r, "path", None) == f"{self.valves.api_prefix}/config" for r in __app__.routes):
    __app__.include_router(qk_router, prefix=self.valves.api_prefix)
...
async def _page(request: Request):
    return HTMLResponse(qk_build_page(self.valves.api_prefix))
```

with `Depends(_require_admin)` on the page route (drop the isinstance check). Add the sync marker comment to the filter file (top of shared block).

- [ ] **Step 4: Run tests — expect PASS**

Run: `python3 -m pytest tests/test_admin_api.py tests/test_smoke.py -v`

- [ ] **Step 5: Commit**

```bash
git add quota_keeper_admin.py quota_keeper_filter.py tests/
git commit -m "fix(security): raise HTTPException from auth deps; single route prefix; page prefix placeholder; mount dedup

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Metering fixes in the Filter (orphan, dedup order, block_message, eff<=0, bools, priced counter, TZ prune, SSE pre-filter, Anthropic partial merge)

**Files:**
- Modify: `quota_keeper_filter.py` (`_record`, `outlet`, `stream`, `inlet`, `qk_record_usage`, `qk_prune_ledger`)
- Test: `tests/test_metering.py`

**Interfaces:**
- Produces: `_record(user, model, tok, rid)` calls `_mark_seen` only on real record; `qk_record_usage(user, model, tok)` writes `unpriced_requests`, `tou` fields (Task 7 adds rates; here only counters), `hours` bucket; merge helper `_merge_partial_usage(old, new)`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_metering.py
import pytest


def _user(uid="u1"):
    return {"id": uid, "name": "U", "email": "u@x.com", "role": "user"}


def test_orphan_adopted_when_outlet_has_usage(qk):
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream({"id": "r1", "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                          "model": "gpt-4o"}, __user__=None, __metadata__={}))
    asyncio.run(f.outlet({"id": "r1", "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                          "model": "gpt-4o"}, __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {"users": {}})
    d = led["users"]["u1"]["days"]
    day = list(d)[0]
    assert d[day]["requests"] == 1          # exactly once
    assert d[day]["tokens"]["input"] == 100


def test_orphan_adopted_when_outlet_no_usage(qk):
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream({"id": "r2", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                          "model": "gpt-4o"}, __user__=None, __metadata__={}))
    asyncio.run(f.outlet({"id": "r2", "model": "gpt-4o"}, __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {"users": {}})
    day = list(led["users"]["u1"]["days"])[0]
    assert led["users"]["u1"]["days"][day]["requests"] == 1


def test_block_message_bad_template_still_blocks(qk):
    f = qk.Filter()
    f.valves.block_message = "quota {used} of {quota} — contact {admin}"
    qk.qk_atomic_write(qk.QK_LEDGER_PATH, {"users": {"u1": {"days": {qk.qk_local_now(qk.qk_get_config()).strftime("%Y-%m-%d"): {"cost_usd": 999.0}}}}})
    with pytest.raises(qk.QuotaBlocked):
        import asyncio
        asyncio.run(f.inlet({"model": "gpt-4o"}, __user__=_user(), __metadata__={}))


def test_eff_zero_blocks_not_unlimited(qk):
    cfg = qk.qk_get_config()
    cfg["user_quotas"]["u1"] = 100
    cfg["schedule"]["night_multiplier"] = 0.0
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    f = qk.Filter()
    import asyncio
    with pytest.raises(qk.QuotaBlocked):
        asyncio.run(f.inlet({"model": "gpt-4o"}, __user__=_user(), __metadata__={}))


def test_bool_quota_rejected(qk):
    cfg = qk.qk_get_config()
    cfg["user_quotas"]["u1"] = True
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    quota, src = qk.qk_resolve_quota(cfg, _user())
    assert quota is None and src == "none"


def test_unpriced_requests_counter_selfheals(qk, monkeypatch):
    # no pricing cache -> unpriced_requests=1
    qk.qk_record_usage(_user(), "totally-unknown", {"cached": 0, "input": 10, "output": 5, "cache_write": 0})
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = list(led["users"]["u1"]["days"])[0]
    mm = led["users"]["u1"]["days"][day]["models"]["totally-unknown"]
    assert mm["unpriced_requests"] == 1
    # now price appears -> counter stays but new request priced
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"totally-unknown": {"input": 1, "output": 2}}})
    qk.qk_record_usage(_user(), "totally-unknown", {"cached": 0, "input": 10, "output": 5, "cache_write": 0})
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    mm = led["users"]["u1"]["days"][day]["models"]["totally-unknown"]
    assert mm["unpriced_requests"] == 1 and mm["requests"] == 2


def test_anthropic_partial_usage_merged(qk):
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream({"id": "r3", "message": {"usage": {"input_tokens": 40}},
                          "model": "claude-x"}, __user__=_user(), __metadata__={}))
    asyncio.run(f.stream({"id": "r3", "usage": {"output_tokens": 7}},
                          "model": "claude-x"}, __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = list(led["users"]["u1"]["days"])[0]
    d = led["users"]["u1"]["days"][day]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 40 and d["tokens"]["output"] == 7


def test_stream_sse_string_with_usage_prefix(qk):
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream('data: {"id":"r4","usage":{"prompt_tokens":8,"completion_tokens":3},"model":"gpt-4o"}',
                         __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = list(led["users"]["u1"]["days"])[0]
    assert led["users"]["u1"]["days"][day]["requests"] == 1
```

- [ ] **Step 2: Run — expect FAIL**

Run: `python3 -m pytest tests/test_metering.py -v`

- [ ] **Step 3: Implement (filter only; all helpers stay shared-sync-ready)**

`_record`:

```python
def _record(self, user, model, tok, rid=""):
    uid = (user or {}).get("id")
    if not uid:
        rid = rid or f"{time.time_ns()}"
        self._orphan[rid] = {"model": model, "tok": tok, "ts": time.time()}
        while len(self._orphan) > 256:
            self._orphan.popitem(last=False)
        return
    rid = rid or f"{time.time_ns()}"
    if not self._mark_seen(rid):
        return
    qk_record_usage(user, model, tok)
```

`stream`: after JSON decode, `u = ev.get("usage")`; if `u is None and isinstance(ev.get("message"), dict): u = ev["message"].get("usage")`. Keep per-id partial merge state:

```python
tok = qk_normalize_usage(u)
if tok is None:
    return event
rid = str(ev.get("id") or f"stream-{time.time_ns()}")
merged = self._partial.get(rid)
if merged:
    merged_tok = {"cached": merged["cached"] + tok["cached"], "input": merged["input"] + tok["input"],
                  "output": merged["output"] + tok["output"], "cache_write": merged["cache_write"] + tok["cache_write"]}
else:
    merged_tok = tok
if rid in self._seen:      # already recorded for this id -> stash partial for the terminal event
    self._partial[rid] = merged_tok
    return event
self._record(...)
```

Wait — simpler and test-driven: record on FIRST usage-bearing event, stash later partials, and when a later partial arrives for a recorded id, add the delta into the ledger directly via `qk_record_usage` without counting a new request. Implementation detail chosen for the plan: maintain `self._partial[rid]` and a `_topup(uid)` path. If this proves fiddly, the acceptable simpler behavior (documented in code comment) is: first event records, subsequent partial usages call `qk_record_usage(..., count_request=False)`. Add parameter `count_request: bool = True` to `qk_record_usage`.

`outlet`:

```python
tok = qk_normalize_usage(body.get("usage"))
if tok is not None:
    rid = str(body.get("id") or "")
    self._record(__user__ or {}, model, tok, rid)
elif rid and (__user__ or {}).get("id") and rid in self._orphan:
    ent = self._orphan.pop(rid)
    qk_record_usage(__user__, ent["model"], ent["tok"])
elif self.valves.estimate_unreported_tokens and (__user__ or {}).get("id"):
    ch = (body.get("choices") or [{}])[0]
    content = str((ch.get("message") or {}).get("content") or "")
    if content:
        est = {"cached": 0.0, "input": 0.0, "output": len(content) / 4.0, "cache_write": 0.0}
        rid = str(body.get("id") or "")
        self._record(__user__, model, est, rid or f"est-{time.time_ns()}")
```

`inlet`: replace the format call with:

```python
try:
    msg = self.valves.block_message.format(used=..., quota=..., source=..., mult=...)
except Exception:
    log.warning("quota-keeper block_message template invalid; using default")
    msg = Filter.Valves().block_message.format(used=..., quota=..., source=..., mult=...)
raise QuotaBlocked(msg)
```

Replace `if eff <= 0: return body` with `if eff <= 0: raise QuotaBlocked(...)` (message notes multiplier=0). In `qk_resolve_quota` and multiplier parsing, guard booleans:

```python
def _num(v):
    return not isinstance(v, bool) and isinstance(v, (int, float))
```

`qk_record_usage`: `mm["unpriced_requests"] = mm.get("unpriced_requests", 0) + (0 if priced else 1)`; drop the sticky `priced` flag (keep writing `priced: bool` derived for back-compat UI until Task 9 replaces it: write both). Add `hours` bucket: `h = u["days"][day].setdefault("hours", {}).setdefault(now_local.hour, {...})` accumulate requests/cost/tokens. `qk_prune_ledger`: cutoff via `qk_local_now(cfg) - timedelta(days=days)` instead of `datetime.now(utc)`. `stream` SSE pre-filter: `if isinstance(ev, str) and '"usage"' not in ev: return event` before json.loads.

- [ ] **Step 4: Run — expect PASS**

Run: `python3 -m pytest tests/test_metering.py -v`

- [ ] **Step 5: Mirror the shared parts into admin (none of this task's helpers are admin-used except `qk_local_now`/`_num` parsing — sync those two verbatim) and commit**

```bash
git add quota_keeper_filter.py quota_keeper_admin.py tests/
git commit -m "fix(metering): orphan adoption, seen-order, template fallback, eff<=0 semantics, bool guards, unpriced counter, TZ prune, SSE pre-filter, partial-usage merge

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Config validation + deep merge + JS zero-safe save

**Files:**
- Modify: `quota_keeper_admin.py` (`api_save_config`, new `qk_validate_config`, deep merge into on-disk config)
- Modify: `quota_keeper_filter.py` (sync `qk_validate_config` + deep merge verbatim)
- Test: `tests/test_config_api.py`

**Interfaces:**
- Produces: `qk_validate_config(cfg) -> list[str]` (error strings; empty = ok); `api_save_config` merges into on-disk then writes; invalid -> 400 `{"errors": [...]}`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_config_api.py
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(load_admin):
    adm = load_admin()
    app = FastAPI()
    app.include_router(adm.qk_router, prefix="/api/v1/quota-keeper")
    return TestClient(app), adm


def test_partial_post_preserves_siblings(load_admin):
    c, adm = _app(load_admin)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, {"pricing": {"url": "u", "refresh_hours": 12, "overrides": {"m": {"input": 1}}}})
    r = c.post("/api/v1/quota-keeper/config", json={"pricing": {"url": "u2"}})
    assert r.status_code == 200
    cfg = adm.qk_load_json(adm.QK_CONFIG_PATH, {})
    assert cfg["pricing"]["refresh_hours"] == 12 and cfg["pricing"]["overrides"] == {"m": {"input": 1}}
    assert cfg["pricing"]["url"] == "u2"


def test_bad_schema_400(load_admin):
    c, adm = _app(load_admin)
    r = c.post("/api/v1/quota-keeper/config", json={"schedule": "x"})
    assert r.status_code == 400 and "schedule" in r.text


def test_numeric_bounds(load_admin):
    c, adm = _app(load_admin)
    r = c.post("/api/v1/quota-keeper/config", json={"schedule": {"night_start_hour": 99}})
    assert r.status_code == 400
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

```python
def qk_deep_merge(base, patch):
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            qk_deep_merge(base[k], v)
        else:
            base[k] = v
    return base


_QK_NUM = lambda v: not isinstance(v, bool) and isinstance(v, (int, float))


def qk_validate_config(cfg) -> list:
    errs = []
    if not isinstance(cfg, dict):
        return ["config must be an object"]
    if "credits_per_usd" in cfg and not (_QK_NUM(cfg["credits_per_usd"]) and cfg["credits_per_usd"] > 0):
        errs.append("credits_per_usd must be a positive number")
    if "quota_period" in cfg and cfg["quota_period"] not in (None, "daily", "monthly"):
        errs.append("quota_period must be daily|monthly")
    for key in ("user_quotas", "group_quotas"):
        if key in cfg and not isinstance(cfg[key], dict):
            errs.append(f"{key} must be an object")
    sch = cfg.get("schedule")
    if sch is not None and not isinstance(sch, dict):
        errs.append("schedule must be an object")
    if isinstance(sch, dict):
        for k in ("night_start_hour", "night_end_hour"):
            if k in sch and not (isinstance(sch[k], int) and not isinstance(sch[k], bool) and 0 <= sch[k] <= 23):
                errs.append(f"schedule.{k} must be int 0-23")
        for k in ("night_multiplier", "weekend_multiplier"):
            if k in sch and not (_QK_NUM(sch[k]) and sch[k] >= 0):
                errs.append(f"schedule.{k} must be number >= 0")
    pri = cfg.get("pricing")
    if pri is not None and not isinstance(pri, dict):
        errs.append("pricing must be an object")
    tou = cfg.get("tou")
    if tou is not None and not isinstance(tou, dict):
        errs.append("tou must be an object")
    return errs
```

`api_save_config`:

```python
body = await request.json()
errs = qk_validate_config(body)
if errs:
    return JSONResponse({"errors": errs}, status_code=400)
with qk_lock():
    cur = qk_load_json(QK_CONFIG_PATH, {})
    qk_deep_merge(cur, body)
    cfg = qk_merge_config(cur)
    qk_atomic_write(QK_CONFIG_PATH, cfg)
return JSONResponse({"ok": True})
```

JS in QK_PAGE `saveConfig` (full rewrite of number handling):

```javascript
const num=(id,def)=>{const v=parseFloat($(id).value);return isNaN(v)?def:v};
// use num('night_start_hour',22) etc — 0 survives; NaN falls back
```

- [ ] **Step 4: Run — expect PASS** then commit `fix(config): deep-merge partial saves, schema validation 400, zero-safe JS save`

---

### Task 5: TOU tiered pricing engine (shared) + ledger fields

**Files:**
- Modify: `quota_keeper_filter.py` (DEFAULT_CONFIG `tou` block, `qk_tou_rate`, integration in `qk_record_usage`)
- Modify: `quota_keeper_admin.py` (same verbatim: DEFAULT_CONFIG, `qk_tou_rate`, `qk_validate_config` TOU rules)
- Test: `tests/test_tou.py`

**Interfaces:**
- Produces: `qk_tou_rate(cfg, model_id, now) -> tuple[float, str]` (rate, tier-name; `(1.0, "off"|"normal")` when not applicable). Day/model records gain `tou: {"peak": n, "offpeak": n, "normal": n}` and `cost_saved_usd`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_tou.py
from datetime import datetime


def _cfg(qk, **over):
    cfg = qk.qk_get_config()
    cfg["tou"] = {
        "enabled": True, "timezone": "UTC",
        "tiers": {
            "peak": {"rate": 2.0, "windows": [{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"}]},
            "offpeak": {"rate": 0.5, "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:30", "end": "08:30"}]},
            "normal": {"rate": 1.0},
        },
        "holidays": [], "default_policy": "off",
        "providers": {"deepseek": {"enabled": True}},
        "models": {},
    }
    cfg["tou"].update(over)
    return cfg


def test_peak_window_weekday(qk):
    cfg = _cfg(qk)
    # 2026-08-17 is a Monday 10:00 UTC
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 10, 0))
    assert (rate, tier) == (2.0, "peak")


def test_offpeak_window(qk):
    cfg = _cfg(qk)
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 5, 0))
    assert (rate, tier) == (0.5, "offpeak")


def test_normal_outside_windows(qk):
    cfg = _cfg(qk)
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 13, 0))
    assert (rate, tier) == (1.0, "normal")


def test_weekend_no_peak(qk):
    cfg = _cfg(qk)
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 15, 10, 0))  # Saturday
    assert tier == "normal"


def test_provider_unmatched_model_off(qk):
    cfg = _cfg(qk)
    rate, tier = qk.qk_tou_rate(cfg, "openai/gpt-4o", datetime(2026, 8, 17, 10, 0))
    assert tier == "off"


def test_model_override_wins(qk):
    cfg = _cfg(qk, models={"deepseek/deepseek-v4": {"enabled": True, "tiers": {"peak": {"rate": 1.5}}}})
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 10, 0))
    assert (rate, tier) == (1.5, "peak")


def test_provider_tier_rate_override(qk):
    cfg = _cfg(qk, providers={"deepseek": {"enabled": True, "tiers": {"offpeak": {"rate": 0.6}}}})
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 5, 0))
    assert (rate, tier) == (0.6, "offpeak")


def test_holiday_forces_offpeak(qk):
    cfg = _cfg(qk, holidays=["2026-08-17"])
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 10, 0))
    assert tier == "offpeak"


def test_midnight_spanning_window(qk):
    cfg = _cfg(qk)
    cfg["tou"]["tiers"]["offpeak"]["windows"] = [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00"}]
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 23, 30))
    assert tier == "offpeak"
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 5, 0))
    assert tier == "offpeak"


def test_record_applies_tou_and_saves(qk, monkeypatch):
    cfg = _cfg(qk)
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"deepseek/deepseek-v4": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "deepseek/deepseek-v4",
                       {"cached": 0, "input": 1_000_000, "output": 0, "cache_write": 0},
                       now=datetime(2026, 8, 17, 10, 0))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = list(led["users"]["u1"]["days"])[0]
    mm = led["users"]["u1"]["days"][day]["models"]["deepseek/deepseek-v4"]
    assert abs(mm["cost_usd"] - 2.0) < 1e-9        # 1M input x $1 x peak 2.0
    assert mm["tou"]["peak"] == 1
    assert abs(mm.get("cost_saved_usd", 0) + 1.0) < 1e-9  # saved = -(extra paid)
```

- [ ] **Step 2: Run — expect FAIL** (`qk_tou_rate` missing; `qk_record_usage` lacks `now` param)

- [ ] **Step 3: Implement (identical block in BOTH files)**

Add to `DEFAULT_CONFIG`:

```python
"tou": {
    "enabled": False,
    "timezone": None,               # None -> schedule.timezone
    "tiers": {
        "peak": {"rate": 2.0, "windows": [{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"},
                                           {"days": [1, 2, 3, 4, 5], "start": "14:00", "end": "18:00"}]},
        "offpeak": {"rate": 0.5, "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:30", "end": "08:30"}]},
        "normal": {"rate": 1.0},
    },
    "holidays": [],                  # "YYYY-MM-DD" -> whole day offpeak
    "default_policy": "off",         # off | normal
    "providers": {},                 # "<first path segment>": {"enabled": bool, "tiers": {name: {"rate": x}}}
    "models": {},                    # exact model id: {"enabled": bool, "tiers": {...}}
},
```

```python
def qk_tou_resolve_policy(cfg: dict, model_id: str):
    """models[exact] -> providers[first segment] -> default_policy. Returns policy dict or None (off)."""
    tou = cfg.get("tou") or {}
    if not tou.get("enabled"):
        return None
    mid = str(model_id or "").strip().lower()
    mpol = (tou.get("models") or {}).get(mid)
    if isinstance(mpol, dict):
        return mpol if mpol.get("enabled", True) else None
    prov = mid.split("/")[0] if "/" in mid else "_default"
    ppol = (tou.get("providers") or {}).get(prov)
    if isinstance(ppol, dict):
        return ppol if ppol.get("enabled", True) else None
    if (tou.get("providers") or {}).get(prov) is None and tou.get("default_policy") == "normal":
        return {}
    return None


def qk_tou_rate(cfg: dict, model_id: str, now) -> tuple:
    """Returns (rate, tier). Tier 'off' means TOU does not apply (rate 1.0)."""
    tou = cfg.get("tou") or {}
    pol = qk_tou_resolve_policy(cfg, model_id)
    if pol is None:
        return 1.0, "off"
    tiers = dict(tou.get("tiers") or {})

    def _merge(tname):
        base = dict(tiers.get(tname) or {})
        over = ((pol.get("tiers") or {}).get(tname)) or {}
        base.update(over)
        return base

    peak, offpeak, normal = _merge("peak"), _merge("offpeak"), _merge("normal")
    dstr = now.strftime("%Y-%m-%d")
    if dstr in (tou.get("holidays") or []) and offpeak:
        return float(offpeak.get("rate", 1.0)), "offpeak"

    def _hit(tier):
        for w in tier.get("windows") or []:
            days = w.get("days") or list(range(7))
            if now.weekday() not in days:
                continue
            try:
                sh, sm = map(int, str(w.get("start", "00:00")).split(":"))
                eh, em = map(int, str(w.get("end", "00:00")).split(":"))
            except Exception:
                continue
            s, e, cur = sh * 60 + sm, eh * 60 + em, now.hour * 60 + now.minute
            if s <= e:
                if s <= cur < e:
                    return True
            else:  # spans midnight
                if cur >= s or cur < e:
                    return True
        return False

    if peak and _hit(peak):
        return float(peak.get("rate", 1.0)), "peak"
    if offpeak and _hit(offpeak):
        return float(offpeak.get("rate", 1.0)), "offpeak"
    return float(normal.get("rate", 1.0)), "normal"
```

`qk_record_usage` gains `now: datetime = None` param (default `qk_local_now(cfg)`); after computing `cost`:

```python
rate, tier = qk_tou_rate(cfg, model, now)
base_cost = cost
cost = cost * rate
...
t = mm.setdefault("tou", {"peak": 0, "offpeak": 0, "normal": 0})
if tier in t:
    t[tier] += 1
mm["cost_saved_usd"] = round(mm.get("cost_saved_usd", 0.0) + (base_cost - cost), 8)
```

Day record mirrors `tou` counters + `cost_saved_usd`. Extend `qk_validate_config` (Task 4) with TOU rules: `tou.tiers.<name>.rate` positive number; `windows[*].days` list of ints 0-6; `start`/`end` `HH:MM` matching `^\d{2}:\d{2}$`; `holidays` list of `YYYY-MM-DD`; `default_policy` in `{"off","normal"}`.

- [ ] **Step 4: Run — expect PASS** then commit `feat(tou): DeepSeek-style peak/off-peak tiered pricing per provider/model with holidays`

---

### Task 6: recent.json ring buffer + /recent + /stats + /me endpoints

**Files:**
- Modify: `quota_keeper_filter.py` (`qk_record_usage` appends to `recent.json` under same lock)
- Modify: `quota_keeper_admin.py` (routes `/recent`, `/stats`, `/me`; `/pricing?full=1`)
- Test: `tests/test_endpoints.py`

**Interfaces:**
- Produces: `recent.json` = `{"items":[{ts,user_id,name,email,model,tokens{cached,input,output},cost_usd,tou_tier,priced}]}` capped 200 newest-last; `GET /recent` admin -> newest-first; `GET /stats?from&to&user&model&granularity` -> `{kpi:{...}, series:[{bucket, by_model:{...}}], users:[...], models:[...]}` with users rows carrying `quota`,`quota_source`,`multiplier`,`used_credits`; `GET /me` -> own quota/usage/trend; `GET /pricing` -> summary unless `?full=1`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_endpoints.py
import time
from fastapi import FastAPI
from fastapi.testclient import TestClient
from conftest import write_json


def _app(load_admin, monkeypatch_user=None):
    adm = load_admin()
    app = FastAPI()
    app.include_router(adm.qk_router, prefix="/api/v1/quota-keeper")

    async def _fake_verified_user(request):
        class U:
            id = "u1"; name = "U"; email = "u1@x.com"; role = "admin"
        return U()
    import open_webui  # not installed; inject stub module instead
    return app, adm


def test_recent_ring_buffer(qk):
    for i in range(205):
        qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "m/x",
                           {"cached": 1, "input": 2, "output": 3, "cache_write": 0})
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    assert len(rec["items"]) == 200


def test_me_returns_own_data(load_admin, monkeypatch):
    adm = load_admin()
    import types
    fake = types.ModuleType("open_webui")
    auth = types.ModuleType("open_webui.utils.auth")

    class U:
        id = "u1"; name = "U"; email = "u1@x.com"; role = "user"
    async def gv(request):
        return U()
    auth.get_verified_user = gv
    fake_auth = types.ModuleType("open_webui.utils"); fake_auth.auth = auth
    utils = types.ModuleType("open_webui.utils"); utils.auth = auth
    fake.utils = utils
    monkeypatch.setitem(__import__("sys").modules, "open_webui", fake)
    monkeypatch.setitem(__import__("sys").modules, "open_webui.utils", utils)
    monkeypatch.setitem(__import__("sys").modules, "open_webui.utils.auth", auth)

    app = FastAPI(); app.include_router(adm.qk_router, prefix="/api/v1/quota-keeper")
    r = TestClient(app).get("/api/v1/quota-keeper/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["id"] == "u1" and "quota" in body and "used_credits" in body


def test_stats_aggregates(qk, load_admin, monkeypatch):
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "m/x",
                       {"cached": 10, "input": 90, "output": 50, "cache_write": 0})
    adm = load_admin()
    out = adm.qk_stats(from_=None, to=None, user=None, model=None, granularity="day")
    assert out["kpi"]["requests"] == 1
    assert abs(out["kpi"]["cache_rate"] - 0.1) < 1e-9
    assert out["models"][0]["model"] == "m/x"
```

- [ ] **Step 2: Run — expect FAIL** (`QK_RECENT_PATH`/`/me`/`qk_stats` missing)

- [ ] **Step 3: Implement**

Filter: `QK_RECENT_PATH = os.path.join(QK_DIR, "recent.json")` (both files); in `qk_record_usage` inside the lock:

```python
rec = qk_load_json(QK_RECENT_PATH, {"items": []})
items = rec.setdefault("items", [])
items.append({"ts": time.time(), "user_id": uid, "name": u.get("name", ""), "email": u.get("email", ""),
              "model": model, "tokens": {k: tok.get(k, 0.0) for k in ("cached", "input", "output")},
              "cost_usd": cost, "tou_tier": tier, "priced": priced})
del items[:-200]
qk_atomic_write(QK_RECENT_PATH, rec)
```

Admin: add `from datetime import datetime, timedelta, timezone as _dt_timezone` (already present) and:

```python
def qk_stats(from_=None, to=None, user=None, model=None, granularity="day"):
    led = qk_load_json(QK_LEDGER_PATH, {"users": {}})
    users = led.get("users") or {}
    kpi = {"requests": 0, "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
           "cost_usd": 0.0, "unpriced_requests": 0}
    series, users_rows, models_rows = {}, [], {}
    cfg = qk_get_config()
    from_s = from_ or "0000-00-00"; to_s = to or "9999-99-99"
    for uid, u in users.items():
        if user and user not in (uid, (u.get("name") or ""), (u.get("email") or "")):
            continue
        row = {"user_id": uid, "name": u.get("name", ""), "email": u.get("email", ""),
               "requests": 0, "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0}, "cost_usd": 0.0,
               "models": 0, "unpriced_requests": 0}
        quota, source = qk_resolve_quota(cfg, {"id": uid})
        row["quota"], row["quota_source"] = quota, source
        row["multiplier"] = qk_time_multiplier(cfg)
        for day, drec in sorted((u.get("days") or {}).items()):
            if not (from_s <= day <= to_s):
                continue
            row["requests"] += (drec or {}).get("requests", 0)
            row["cost_usd"] += (drec or {}).get("cost_usd", 0) or 0
            for k in ("cached", "input", "output"):
                tk = (drec.get("tokens") or {}).get(k, 0) or 0
                row["tokens"][k] += tk; kpi["tokens"][k] += tk
            for m, mm in (drec.get("models") or {}).items():
                if model and model != m:
                    continue
                mk = models_rows.setdefault(m, {"model": m, "requests": 0, "cost_usd": 0.0,
                                                "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                                                "users": set(), "unpriced_requests": 0,
                                                "tou": {"peak": 0, "offpeak": 0, "normal": 0},
                                                "cost_saved_usd": 0.0})
                mk["requests"] += mm.get("requests", 0)
                mk["cost_usd"] += mm.get("cost_usd", 0) or 0
                mk["users"].add(uid)
                mk["unpriced_requests"] += mm.get("unpriced_requests", 0) or 0
                mk["cost_saved_usd"] += mm.get("cost_saved_usd", 0) or 0
                for k in ("cached", "input", "output"):
                    mk["tokens"][k] += (mm.get("tokens") or {}).get(k, 0) or 0
                for tname, tv in ((mm.get("tou") or {})).items():
                    mk["tou"][tname] = mk["tou"].get(tname, 0) + (tv or 0)
                bkey = day if granularity != "hour" else None
                if bkey:
                    sb = series.setdefault(bkey, {})
                    sb[m] = sb.get(m, 0) + mm.get("cost_usd", 0)
            for h, hrec in ((drec.get("hours") or {}).items()):
                bkey = f"{day}T{int(h):02d}" if granularity == "hour" else None
                if bkey:
                    series.setdefault(bkey, {})  # cost aggregated per hour bucket
                    series[bkey]["_"] = series[bkey].get("_", 0) + (hrec.get("cost_usd", 0) or 0)
        kpi["requests"] += row["requests"]; kpi["cost_usd"] += row["cost_usd"]
        kpi["unpriced_requests"] += row["unpriced_requests"]
        row["models"] = len([m for m in models_rows])  # corrected per-user below
        users_rows.append(row)
    ci = kpi["tokens"]["cached"] + kpi["tokens"]["input"]
    kpi["cache_rate"] = (kpi["tokens"]["cached"] / ci) if ci else 0.0
    for mk in models_rows.values():
        mk["users"] = len(mk["users"])
        tot = sum(mk["tokens"].values())
        mk["blended_per_m"] = (mk["cost_usd"] * 1e6 / tot) if tot else 0.0
    return {"kpi": kpi, "series": [{"bucket": b, "by_model": v} for b, v in sorted(series.items())],
            "users": users_rows, "models": sorted(models_rows.values(), key=lambda x: -x["cost_usd"])}
```

(The per-user `models` count fix: compute inside the day loop into a set on the row; the implementer follows the test, not this sketch verbatim — tests are the contract. Known correction: `row["models"]` must count models seen for THIS user.)

Routes:

```python
@qk_router.get("/recent")
async def api_recent(request: Request):
    rec = qk_load_json(QK_RECENT_PATH, {"items": []})
    return JSONResponse({"items": list(reversed(rec.get("items") or []))})


@qk_router.get("/stats")
async def api_stats(request: Request):
    q = request.query_params
    return JSONResponse(qk_stats(q.get("from"), q.get("to"), q.get("user"), q.get("model"),
                                  q.get("granularity", "day")))


@qk_router.get("/me")
async def api_me(request: Request, user=Depends(_require_user)):
    cfg = qk_get_config()
    quota, source = qk_resolve_quota(cfg, {"id": user.id})
    mult = qk_time_multiplier(cfg)
    led = (qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {}).get(user.id) or {}
    days = led.get("days") or {}
    now = qk_local_now(cfg)
    pref = now.strftime("%Y-%m-")
    used_month = sum((d or {}).get("cost_usd", 0) or 0 for k, d in days.items() if k.startswith(pref))
    used_day = ((days.get(now.strftime("%Y-%m-%d")) or {}).get("cost_usd", 0)) or 0
    cpu_ = float(cfg.get("credits_per_usd") or 1000.0)
    trend = []
    for i in range(6, -1, -1):
        kd = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        dd = days.get(kd) or {}
        trend.append({"day": kd, "requests": dd.get("requests", 0), "cost_usd": dd.get("cost_usd", 0) or 0})
    return JSONResponse({"user": {"id": user.id, "name": user.name, "email": user.email, "role": user.role},
                         "quota": quota, "quota_source": source, "multiplier": mult,
                         "effective_quota": (quota * mult) if quota is not None else None,
                         "used_credits": used_month * cpu_ if (cfg.get("quota_period") == "monthly") else used_day * cpu_,
                         "today": {"cost_usd": used_day, "requests": (days.get(now.strftime("%Y-%m-%d")) or {}).get("requests", 0)},
                         "trend": trend, "tou": {"current_tier": qk_tou_rate(cfg, "*", now)[1] if False else None}})
```

(`tou.current_tier` left `None` until the page wires a per-user model list; the field exists so the UI contract is stable. `_require_user` import of `Depends` already present.)

`/pricing`:

```python
@qk_router.get("/pricing")
async def api_pricing(request: Request):
    cache = qk_load_json(QK_PRICING_PATH, {}) or {}
    if request.query_params.get("full") == "1":
        return JSONResponse(cache)
    return JSONResponse({k: cache.get(k) for k in ("url", "fetched_at_iso", "models")})
```

- [ ] **Step 4: Run — expect PASS** then commit `feat(api): /recent ring buffer, /stats aggregation, /me self-service, pricing summary payload`

---

### Task 7: pricing loop robustness (to_thread, strong ref, create_task)

**Files:**
- Modify: `quota_keeper_admin.py` (`_pricing_loop` start, `qk_refresh_pricing` call sites)

- [ ] **Step 1: Test**

```python
# tests/test_runtime.py (append to test_endpoints.py is fine)
def test_event_starts_loop_with_strong_ref(load_admin):
    adm = load_admin()
    ev = adm.Event()
    ev.valves.enable_background_pricing_refresh = False  # avoid real loop in test
    import asyncio

    class App:
        routes = []
        def include_router(self, *a, **k): pass
        def get(self, p): pass
    asyncio.run(ev.event({}, __event_name__="system.startup.completed", __id__="f1", __app__=App()))
    assert ev._installed is True
```

- [ ] **Step 2: Run (baseline PASS after Task 2 — this test guards the refactor)**

- [ ] **Step 3: Implement**

```python
# in event(), replacing asyncio.get_event_loop().create_task(...):
self._pricing_task = asyncio.create_task(_pricing_loop())
```

and wrap the blocking call:

```python
async def _pricing_loop():
    while True:
        try:
            await asyncio.to_thread(qk_refresh_pricing, force=False)
        except Exception as e:
            log.info("quota-keeper pricing refresh failed: %s", e)
        await asyncio.sleep(600)
```

`api_refresh`: `result = await asyncio.to_thread(qk_refresh_pricing, bool(body.get("force")))`.

- [ ] **Step 4: Run all tests; commit** `fix(runtime): pricing fetch off the event loop, task strong reference, create_task`

---

### Task 8: Admin page rewrite (dashboard + ranking + recent log + pricing editor + TOU editor + role split)

**Files:**
- Modify: `quota_keeper_admin.py` (`QK_PAGE` full rewrite)

**Interfaces:**
- Consumes: `/me`, `/stats`, `/recent`, `/pricing?full=1`, `/config`, `/users`, `/groups`, `/pricing/match`, `/pricing/refresh`; `__QK_API_PREFIX__` placeholder.
- Produces: single-page app; `esc()` on ALL interpolations; zero `setInterval`; localStorage keys `qk_span`, `qk_sort`.

This is a large single HTML/JS block (~600 lines). Requirements checklist (each verified by the page tests in Task 9):
1. On load: fetch `${API}/me`; `role==='admin'` -> render admin sections; else render personal card only (quota bar, multiplier, today/month, 7-day sparkline) and stop.
2. Admin sections: KPI×6 + sparklines; span selector (24h/7d/30d/90d/custom) persisted; stacked SVG trend; users ranking table (sortable th click: requests/tokens/cost/credits/quota%; search box username/email; row click -> per-model drill-down); models table (blended $/M, unpriced, `how`-derived target name); filters user/model; CSV export (Blob download of current table); Recent activity section (manual Refresh button only) rendering name/model/in/out/cached/cache%/cost/tier from `/recent`; existing config sections retained (General/schedule/group quotas/user quotas/pricing source+Test match); NEW Pricing editor table (`/pricing?full=1`, search, paginated 50/page, inline number inputs, Save -> `pricing.overrides` via POST /config `{pricing:{overrides}}` — deep merge keeps siblings); NEW TOU editor (enabled toggle, timezone, per-tier rate+windows with weekday chips, provider list add/enable/override, model overrides add, holidays list + Fetch-from-date.nager.at button `https://date.nager.at/api/v3/PublicHolidays/{year}/{cc}` populating date list, failures toast).
3. All interpolated values pass `esc()` (including `value="..."` attributes and `r.price.*`).
4. Zero polling: every section has an explicit Refresh where data is stale-able; nothing fires on a timer.

- [ ] **Step 1: Implement the page** (single self-contained rewrite of `QK_PAGE`; keep dark theme tokens; reuse `esc/fmt/toast/api` helpers)
- [ ] **Step 2: Verify headless**

Run: `python3 - <<'EOF'
import re
src = open('quota_keeper_admin.py').read()
page = src.split('QK_PAGE = """',1)[1].split('"""',1)[0]
assert '__QK_API_PREFIX__' in src  # placeholder present pre-substitution in QK_PAGE constant
assert 'setInterval' not in page
assert 'qk_span' in page and 'qk_sort' in page
assert page.count('esc(') > 30
print('page static checks ok')
EOF`
Expected: `page static checks ok`

- [ ] **Step 3: py_compile both files; run full test suite; commit** `feat(ui): admin dashboard, ranking tables, recent log, pricing/TOU editors, /me role split`

---

### Task 9: Docs + version bump + full regression + push

**Files:**
- Modify: `quota_keeper_filter.py`, `quota_keeper_admin.py` headers -> `version: 0.2.0`
- Modify: `quota_keeper_README.md` (new features sections: dashboard, /me, TOU, pricing editor; update install notes for valve semantics)
- Modify: `quota_keeper_HANDOFF.md` (§5 add TOU algorithm + stats/recent schema; §7 append new verified cases; §8 mark fixed items 1/2/3 partial etc.; §9 roadmap updates)
- Modify: `CLAUDE.md` (architecture essentials: add tou/stats/recent endpoints)

- [ ] **Step 1: Update version headers + docs**
- [ ] **Step 2: Full regression**

Run: `python3 -m pytest tests/ -v && python3 -m py_compile quota_keeper_filter.py quota_keeper_admin.py`
Expected: all PASS; compiles clean.

- [ ] **Step 3: Handoff v0.1.1 regression cases** — re-run the HANDOFF §5.2 matching cases inline (they live in tests already via Task 3/5 usage of `qk_find_pricing`); confirm pricing match suite still passes.
- [ ] **Step 4: Commit + push**

```bash
git add -A && git commit -m "docs+release: v0.2.0 (fixes, dashboard, /me, TOU pricing, pricing editor)

Co-Authored-By: Claude <noreply@anthropic.com>" && git push origin main
```

---

## Plan Self-Review notes

- Spec §1.1-1.5 -> Tasks 2-4, 7. §2 (dashboard incl. ranking §2.2-4, recent §2.4, resource §2.5) -> Tasks 6, 8. §3 -> Tasks 6 (`/me`), 8 (role split). §4 -> Task 8 (editor) + Task 4 (deep merge keeps overrides). §5 TOU -> Tasks 4 (validation), 5 (engine), 8 (editor + date.nager.at). §6 testing -> per-task TDD. §7 -> Task 9.
- The `qk_stats` code block in Task 6 contains one intentional known-wrong line (`row["models"]`) with the correction stated inline — implementers follow the tests as contract.
- `hours` bucket writing is specified in Task 3 and consumed in Task 6 (`granularity=hour`).
