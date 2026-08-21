# Quota Keeper /quota UI 增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `/quota` 单页加 favicon、admin KPI 区补渠道拆分与 4 张卡曲线、非 admin 个人页加 model 分布/recent 动态/模型价目表。

**Architecture:** 所有改动只落在 `quota_keeper_admin.py`（后端聚合 + 内嵌 SPA）。共享 helper 区块（`==== shared helpers: keep in sync ====`）不触碰；`quota_keeper_filter.py` 完全不动（渠道/tokens 数据它早已在记，本次只是聚合与展示）。`/stats` 的 series 每桶从 `{bucket, by_model}` 扩为 `{bucket, cost, requests, tokens}`（改名 by_model→cost；该端点的唯一消费者就是本 SPA）。`/recent` 与 `/models` 从 admin-only 放宽为登录用户可用，非 admin 强制按本人过滤。

**Tech Stack:** Python 3.10 / FastAPI 0.110（测试用 `.venv` 中固定版本，见 `pytest.ini` 的 `pythonpath = .venv`）；前端为 `QK_PAGE` 内嵌原生 JS（无框架）；测试 pytest + `tests/conftest.py` 的 pydantic/open_webui stub。

## Global Constraints

- **不改 `quota_keeper_filter.py`**，不动共享 helper 区块（`qk_data_dir` … `qk_resolve_quota` 等）。
- 页面**零轮询**：所有区块手动 Refresh / 切 span 时才拉数据（沿用现有约定）。
- 价格表全量数据**不对普通用户开放**：`/pricing` 维持 admin-only；`/models?mine=1` 只给本人用过模型的匹配价。
- 非 admin 数据隔离：服务端强制按 `user.id` 过滤，不靠前端传参。
- 每次功能提交**同步 bump 两个文件 frontmatter 的 `version:`**（本次 → 0.5.0；用户明确要求）。
- 提交后 **push 到 GitHub**（网络抖动时用 `git -c http.sslVerify=false push`，这是本机已验证的绕法）。
- 测试命令：`python -m pytest tests/...`（根目录 `pytest.ini` 已把 `.venv`  prepend 进 pythonpath）；语法检查 `python -m py_compile quota_keeper_admin.py quota_keeper_filter.py`。

## File Structure

- `quota_keeper_admin.py`（唯一改动的源文件）
  - `QK_PAGE`：`<head>` 加 favicon；`renderKpis()` 加渠道小字 + 4 张曲线 + 2 张合计小字；`renderTrend()` 读 `b.cost`；`renderPersonal()` 重写（新增 3 个区块 + 其 render/fetch 函数）。
  - `qk_stats()` / `qk_stats_window()`：KPI 加 `channels`；series 桶结构扩展。
  - `api_recent()` / `api_models()`：放宽权限 + `mine=1`。
  - 新增 `api_me_usage()`（`GET /me/usage`）。
- `tests/test_endpoints.py`：`qk_stats` 新 series 结构断言更新 + `/recent?mine=1`、`/models?mine=1`、`/me/usage` 的新用例。
- `tests/test_metering.py`：`qk_stats_window` 三序列用例（window 用例集中在这里的 24h 区块附近）——实际放在 `test_endpoints.py` 的 window 区块旁，见 Task 3。

## 接口契约（各任务 Producer/Consumer 对齐用）

**/stats 响应（Task 2/3 后）：**
```json
{
  "kpi": {"requests": 0, "tokens": {"cached":0,"input":0,"output":0}, "cost_usd": 0.0,
          "unpriced_requests": 0, "cache_rate": 0.0, "channels": {"webui": 0, "api": 0},
          "window_partial": false},
  "series": [{"bucket": "2026-08-20", "cost": {"m/x": 0.1}, "requests": 3, "tokens": 150.0}],
  "users": [...], "models": [...]
}
```
- day 粒度桶：`cost` 键为模型 id；hour 粒度桶：`cost` 键恒为 `"_"`。
- model 过滤 + hour 粒度：series 为空 list（hours 桶跨模型无法拆，与现状一致）。
- `kpi.channels`：无 model 过滤时 = 全 span 渠道合计；有 model 过滤时 = 该模型渠道合计。

**GET /me/usage?span=7d|30d（Task 4，`_require_user`）：**
```json
{"span": "7d",
 "channels": {"webui": 0, "api": 0},
 "trend": [{"day": "YYYY-MM-DD", "requests": 0, "cost_usd": 0.0}],
 "models": [{"model": "m", "requests": 0,
             "tokens": {"cached":0,"input":0,"output":0}, "cost_usd": 0.0}]}
```
- `trend` 长度 = span 天数，升序，最后一天 = 今天（按 `qk_local_now(cfg)`）。
- `models` 按 `cost_usd` 降序。

**GET /recent?mine=1（Task 5）：** 非 admin 强制 `user_id==me.id`、最多 50 条、剥 `email`；admin 不带参数行为不变（200 条、含 email）。

**GET /models?mine=1（Task 6）：** 非 admin 只见本人用过的模型；条目结构与现有 `/models` 相同（`{model, used, requests, unpriced_requests, cost_usd, matched, how, price, override}`），其中 requests/cost 为本人合计。`price` 为每 1M token 的 `{input, output, cached, cache_write}`（字段值可为 `null`）；`matched:false` 时 `price:null`。

---

### Task 1: Favicon

**Files:**
- Modify: `quota_keeper_admin.py`（`QK_PAGE` 的 `<head>`，约 1286 行 `<title>` 后）

**Interfaces:**
- Consumes: 无
- Produces: `<link rel="icon">`，data URI 内联 SVG（无新路由、无二进制）

- [ ] **Step 1: 加 link 标签**

在 `<title>Quota Keeper</title>` 后插入一行（URL-encoded 单行 SVG；圆角深色底 + 天蓝仪表盘弧线与指针，呼应页面 #0f172a/#38bdf8 配色）：

```html
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230f172a'/%3E%3Cpath d='M8 20a8 8 0 0 1 16 0' fill='none' stroke='%2338bdf8' stroke-width='2.4' stroke-linecap='round'/%3E%3Cline x1='16' y1='20' x2='21' y2='13.5' stroke='%2338bdf8' stroke-width='2.4' stroke-linecap='round'/%3E%3Ccircle cx='16' cy='20' r='2' fill='%2338bdf8'/%3E%3C/svg%3E"/>
```

- [ ] **Step 2: 验证**

Run: `python -m py_compile quota_keeper_admin.py && python -c "import re; s=open('quota_keeper_admin.py').read(); assert 'rel=\"icon\"' in s and 'data:image/svg+xml' in s; print('favicon link present')"`
Expected: `favicon link present`

- [ ] **Step 3: Commit**

```bash
git add quota_keeper_admin.py
git commit -m "feat: favicon for the /quota page (inline SVG data URI)"
```

---

### Task 2: `qk_stats` — KPI channels + series 三序列

**Files:**
- Modify: `quota_keeper_admin.py`（`qk_stats`，682-823 行）
- Test: `tests/test_endpoints.py`（`test_stats_aggregates` 144 行、`test_stats_hour_granularity_buckets` 187 行、`test_stats_filters` 207 行附近）

**Interfaces:**
- Consumes: 现有 ledger day 级/model 级 `channels:{webui,api}` 与 `tokens`（filter 早已写入）
- Produces: 上方"接口契约"的 /stats 结构（`kpi.channels`、series 桶 `{cost, requests, tokens}`）

- [ ] **Step 1: 改测试（先红）**

`tests/test_endpoints.py` 中：
1. `test_stats_aggregates` 末尾的 `assert out["series"] == [{"bucket": "2026-08-17", "by_model": {"m/x": 1.9e-4}}]` 改为：
```python
    assert out["series"] == [{"bucket": "2026-08-17", "cost": {"m/x": 1.9e-4},
                              "requests": 1, "tokens": 150.0}]
    assert out["kpi"]["channels"] == {"webui": 0, "api": 1}  # 该测试未传 channel -> 默认 api
```
2. `test_stats_hour_granularity_buckets` 中两处 series 断言改为：
```python
    assert out["series"] == [
        {"bucket": "2026-08-17T09", "cost": {"_": 1.9e-4}, "requests": 1, "tokens": 150.0},
        {"bucket": "2026-08-17T14", "cost": {"_": 1.9e-4}, "requests": 1, "tokens": 150.0},
    ]
    ...
    assert out["series"] == [
        {"bucket": "2026-08-17", "cost": {"m/x": 3.8e-4}, "requests": 2, "tokens": 300.0},
    ]
```
3. `test_stats_filters` 中 `s = out["series"][0]["by_model"]["m/x"]` 改为 `s = out["series"][0]["cost"]["m/x"]`。
4. 新增 model 过滤下 channels 只算该模型的用例（追加到 `test_stats_filters` 末尾）：
```python
    out = adm.qk_stats(model="m/x")
    assert out["kpi"]["channels"] == {"webui": 0, "api": 1}  # 只算 m/x 那一次
    out = adm.qk_stats()
    assert out["kpi"]["channels"] == {"webui": 0, "api": 2}  # 两次都未传 channel
```
（注：上述既有用例的 `qk_record_usage` 均未传 `channel`，默认 `api`；token 合计 10+90+50=150。）

Run: `python -m pytest tests/test_endpoints.py -k "stats_aggregates or stats_hour or stats_filters" -v`
Expected: FAIL（`by_model` KeyError / channels KeyError）

- [ ] **Step 2: 实现 `qk_stats`**

在 `qk_stats` 的 `kpi` dict 初始化（703-708 行）加 `"channels": {"webui": 0, "api": 0}`；在 `series, users_rows, models_rows = {}, [], {}` 后加 `series_rt = {}`（每桶 `{"requests":0,"tokens":0.0}`）。

day 循环体内改动：
- model 过滤分支（`mm` 已知处，`row["requests"] += mm.get(...)` 之后）加：
```python
                kpi["requests"] += 0  # no-op, kpi summed from row later
                sb_rt = series_rt.setdefault(day, {"requests": 0, "tokens": 0.0})
                sb_rt["requests"] += mm.get("requests", 0)
                sb_rt["tokens"] += sum((mm.get("tokens") or {}).get(k, 0) or 0 for k in ("cached", "input", "output"))
                for cname, cnt in ((mm.get("channels") or {}).items()):
                    if cname in kpi["channels"]:
                        kpi["channels"][cname] += cnt or 0
```
- 非过滤分支（`row["requests"] += drec.get(...)` 之后）加：
```python
                sb_rt = series_rt.setdefault(day, {"requests": 0, "tokens": 0.0})
                sb_rt["requests"] += drec.get("requests", 0)
                sb_rt["tokens"] += sum((drec.get("tokens") or {}).get(k, 0) or 0 for k in ("cached", "input", "output"))
                for cname, cnt in ((drec.get("channels") or {}).items()):
                    if cname in kpi["channels"]:
                        kpi["channels"][cname] += cnt or 0
```
- hours 循环（`for h, hrec in hours.items():` 内，`granularity == "hour" and not model` 分支）补 requests/tokens：
```python
                    series.setdefault(bkey, {})
                    series[bkey]["_"] = series[bkey].get("_", 0) + (
                        (hrec.get("cost_usd") or 0) if isinstance(hrec, dict) else 0
                    )
                    rt = series_rt.setdefault(bkey, {"requests": 0, "tokens": 0.0})
                    if isinstance(hrec, dict):
                        rt["requests"] += hrec.get("requests", 0) or 0
                        rt["tokens"] += sum((hrec.get("tokens") or {}).get(k, 0) or 0 for k in ("cached", "input", "output"))
```

返回 dict 的 series 行改为：
```python
        "series": [
            {"bucket": b, "cost": v,
             "requests": (series_rt.get(b) or {}).get("requests", 0),
             "tokens": (series_rt.get(b) or {}).get("tokens", 0.0)}
            for b, v in sorted(series.items())
        ],
```

Run: `python -m pytest tests/test_endpoints.py -k "stats_aggregates or stats_hour or stats_filters" -v`
Expected: PASS

- [ ] **Step 3: 全量回归 + Commit**

```bash
python -m pytest tests/test_endpoints.py tests/test_metering.py -q
git add quota_keeper_admin.py tests/test_endpoints.py
git commit -m "feat: /stats series carries requests+tokens per bucket; kpi aggregates channels"
```

---

### Task 3: `qk_stats_window` — 24h 窗口三序列 + channels

**Files:**
- Modify: `quota_keeper_admin.py`（`qk_stats_window`，826-938 行）
- Test: `tests/test_endpoints.py`（`test_stats_window_24h`，594 行区块）

**Interfaces:**
- Consumes: `recent.json` 每条 `{ts, channel, tokens, cost_usd, priced, ...}`
- Produces: 与 Task 2 相同的 series/kpi 结构（hour 桶键 `"_"`）

- [ ] **Step 1: 改测试（先红）**

`test_stats_window_24h` 追加（在既有 `out = adm.qk_stats_window(now_ts - 86400)` 断言块后）：
```python
    assert out["kpi"]["channels"] == {"webui": 1, "api": 1}
    b0 = out["series"][0]
    assert set(("bucket", "cost", "requests", "tokens")) <= set(b0)
    assert all(set(b["cost"]) == {"_"} for b in out["series"])
    tot_req = sum(b["requests"] for b in out["series"])
    tot_tok = sum(b["tokens"] for b in out["series"])
    assert tot_req == 2                       # 25h 前的那条被窗口裁掉
    assert abs(tot_tok - (150.0 + 140.0)) < 1e-9   # item1: 0+100+50, item2: 10+90+40
```
（该用例既有 items：item1 tokens cached0/input100/output50=150；item2 cached10/input90/output40=140；第三条 25h 前被裁。）

Run: `python -m pytest tests/test_endpoints.py::test_stats_window_24h -v`
Expected: FAIL（`channels` KeyError）

- [ ] **Step 2: 实现 `qk_stats_window`**

- `kpi` 初始化加 `"channels": {"webui": 0, "api": 0}`；`series` 后加 `series_rt = {}`。
- 逐项循环内（`kpi["requests"] += 1` 附近）加：
```python
        if chan in kpi["channels"]:
            kpi["channels"][chan] += 1
```
- 小时桶处（`sb = series.setdefault(bkey, {}); sb["_"] = ...` 之后）加：
```python
        rt = series_rt.setdefault(bkey, {"requests": 0, "tokens": 0.0})
        rt["requests"] += 1
        rt["tokens"] += sum(float(tok.get(k) or 0.0) for k in ("cached", "input", "output"))
```
- 返回 dict 的 series 行同 Task 2 的生成式。

Run: `python -m pytest tests/test_endpoints.py -k "stats_window" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
python -m pytest tests/test_endpoints.py -q
git add quota_keeper_admin.py tests/test_endpoints.py
git commit -m "feat: 24h window stats also carry requests/tokens series and channels"
```

---

### Task 4: `GET /me/usage` 端点

**Files:**
- Modify: `quota_keeper_admin.py`（`api_me` 之后，约 2577 行处插入）
- Test: `tests/test_endpoints.py`（`/me` 区块后追加）

**Interfaces:**
- Consumes: `qk_get_config()`、`qk_local_now(cfg)`、ledger days（含 `channels`/`models`）
- Produces: 上方契约的 `/me/usage` 结构

- [ ] **Step 1: 写测试（先红）**

追加到 `tests/test_endpoints.py`（复用文件内 `_app` 与 `_stub_self_user`）：
```python
def test_me_usage_own_data_only(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1")
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/x",
                       {"cached": 10, "input": 90, "output": 50, "cache_write": 0},
                       now=datetime(2026, 8, 20, 12, 0), channel="webui")
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/y",
                       {"cached": 0, "input": 10, "output": 10, "cache_write": 0},
                       now=datetime(2026, 8, 21, 9, 0), channel="api")
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/z",
                       {"cached": 0, "input": 999, "output": 999, "cache_write": 0},
                       now=datetime(2026, 8, 21, 9, 0), channel="api")
    c, adm = _app(load_admin)
    monkeypatch.setattr(adm, "qk_local_now", lambda cfg: datetime(2026, 8, 21, 12, 0))

    r = c.get("/api/v1/quota-keeper/me/usage?span=7d")
    assert r.status_code == 200
    body = r.json()
    assert body["span"] == "7d"
    assert len(body["trend"]) == 7
    assert body["trend"][-1]["day"] == "2026-08-21"
    assert body["trend"][-1]["requests"] == 1          # 只算 u1，不含 u2
    assert body["channels"] == {"webui": 1, "api": 1}  # u2 的 api 不计
    models = {m["model"]: m for m in body["models"]}
    assert set(models) == {"m/x", "m/y"}               # u2 的 m/z 不出现
    assert models["m/x"]["tokens"]["cached"] == 10.0
    # cost 降序：m/x (0.00028) 应排在 m/y (0.00004) 前
    assert [m["model"] for m in body["models"]] == ["m/x", "m/y"]

    r = c.get("/api/v1/quota-keeper/me/usage?span=30d")
    assert len(r.json()["trend"]) == 30

def test_me_usage_requires_auth(load_admin):
    c, _ = _app(load_admin)
    assert c.get("/api/v1/quota-keeper/me/usage").status_code == 401
```
（文件顶部已有 `from datetime import date, datetime`，无需新增 import。）

Run: `python -m pytest tests/test_endpoints.py -k me_usage -v`
Expected: FAIL（404）

- [ ] **Step 2: 实现 `api_me_usage`**

在 `api_me` 之后插入：
```python
@qk_router.get("/me/usage")
async def api_me_usage(request: Request, user=Depends(_require_user)):
    cfg = qk_get_config()
    span = request.query_params.get("span") or "7d"
    days_n = 30 if span == "30d" else 7
    span = "30d" if span == "30d" else "7d"
    led = (qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {}).get(user.id) or {}
    days = led.get("days") or {}
    now = qk_local_now(cfg)
    trend, channels, models = [], {"webui": 0, "api": 0}, {}
    for i in range(days_n - 1, -1, -1):
        kd = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        dd = days.get(kd) or {}
        trend.append({"day": kd, "requests": dd.get("requests", 0),
                      "cost_usd": dd.get("cost_usd", 0) or 0})
        for cname, cnt in ((dd.get("channels") or {}).items()):
            if cname in channels:
                channels[cname] += cnt or 0
        for m, mm in ((dd.get("models") or {}).items()):
            mm = mm or {}
            row = models.setdefault(m, {"model": m, "requests": 0,
                                        "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                                        "cost_usd": 0.0})
            row["requests"] += mm.get("requests", 0) or 0
            row["cost_usd"] += mm.get("cost_usd", 0) or 0
            for k in ("cached", "input", "output"):
                row["tokens"][k] += (mm.get("tokens") or {}).get(k, 0) or 0
    return JSONResponse({
        "span": span,
        "channels": channels,
        "trend": trend,
        "models": sorted(models.values(), key=lambda x: -x["cost_usd"]),
    })
```

Run: `python -m pytest tests/test_endpoints.py -k me_usage -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add quota_keeper_admin.py tests/test_endpoints.py
git commit -m "feat: GET /me/usage (7d/30d own-data models/channels/trend)"
```

---

### Task 5: `/recent?mine=1` 放宽

**Files:**
- Modify: `quota_keeper_admin.py`（`api_recent`，2516-2519 行）
- Test: `tests/test_endpoints.py`

**Interfaces:**
- Consumes: `QK_RECENT_PATH`
- Produces: 契约所述 mine/admin 两种行为

- [ ] **Step 1: 改测试（先红）**

- `test_stats_forbidden_for_plain_user` 中删掉 `assert c.get("/api/v1/quota-keeper/recent").status_code == 403` 这行（recent 不再 admin-only）。
- 新增：
```python
def test_recent_mine_filters_and_strips(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1", role="user")
    for i in range(3):
        qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, f"m/{i}",
                           {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/other",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    r = c.get("/api/v1/quota-keeper/recent?mine=1")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    assert all(it["user_id"] == "u1" for it in items)
    assert all("email" not in it for it in items)
    # admin 视角（无 mine）不受影响在既有 test_recent_endpoint_newest_first 覆盖

def test_recent_mine_caps_at_50(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1", role="user")
    for i in range(60):
        qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, f"m/{i}",
                           {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    items = c.get("/api/v1/quota-keeper/recent?mine=1").json()["items"]
    assert len(items) == 50
    assert items[0]["model"] == "m/59"   # newest first preserved
```

Run: `python -m pytest tests/test_endpoints.py -k "recent_mine or forbidden_for_plain" -v`
Expected: FAIL（403 或不过滤）

- [ ] **Step 2: 实现**

替换 `api_recent`：
```python
@qk_router.get("/recent")
async def api_recent(request: Request, user=Depends(_require_user)):
    rec = qk_load_json(QK_RECENT_PATH, {"items": []})
    items = list(reversed(rec.get("items") or []))
    if request.query_params.get("mine") == "1" or getattr(user, "role", "") != "admin":
        items = [it for it in items if it.get("user_id") == user.id][:50]
        items = [{k: v for k, v in it.items() if k != "email"} for it in items]
    return JSONResponse({"items": items})
```
（语义：非 admin 无论是否带 `mine` 都只看本人——服务端强制隔离；admin 不带参数仍是全量 200。）

Run: `python -m pytest tests/test_endpoints.py -k recent -v`
Expected: PASS（含既有 `test_recent_endpoint_newest_first`/`test_recent_ring_buffer` 回归）

- [ ] **Step 3: Commit**

```bash
git add quota_keeper_admin.py tests/test_endpoints.py
git commit -m "feat: /recent?mine=1 - self-service recent feed (50 rows, email stripped)"
```

---

### Task 6: `/models?mine=1` 放宽

**Files:**
- Modify: `quota_keeper_admin.py`（`api_models`，2611-2657 行）
- Test: `tests/test_endpoints.py`

**Interfaces:**
- Consumes: 现有 `qk_find_pricing`、overrides、pricing cache
- Produces: 契约所述条目结构

- [ ] **Step 1: 写测试（先红）**

```python
def test_models_mine_own_models_with_prices(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1", role="user")
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/x",
                       {"cached": 0, "input": 10, "output": 5, "cache_write": 0})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/unpriced",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/theirs",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    r = c.get("/api/v1/quota-keeper/models?mine=1")
    assert r.status_code == 200
    items = {it["model"]: it for it in r.json()["items"]}
    assert set(items) == {"m/x", "m/unpriced"}       # u2 的 m/theirs 不出现
    assert items["m/x"]["matched"] is True
    assert items["m/x"]["price"]["input"] == 1.0
    assert items["m/unpriced"]["matched"] is False
    assert items["m/unpriced"]["price"] is None
    assert items["m/x"]["requests"] == 1             # 本人合计

def test_models_mine_forbidden_shape_admin_unchanged(qk, load_admin, monkeypatch):
    # admin 不带 mine：全量（既有行为）
    _stub_webui_auth(monkeypatch)
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/x",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/y",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    items = c.get("/api/v1/quota-keeper/models").json()["items"]
    assert {it["model"] for it in items} == {"m/x", "m/y"}
```
（`_stub_webui_auth` 文件顶部已 import。）

Run: `python -m pytest tests/test_endpoints.py -k models_mine -v`
Expected: FAIL（403）

- [ ] **Step 2: 实现**

`api_models` 装饰器改 `@qk_router.get("/models")`，签名改 `async def api_models(request: Request, user=Depends(_require_user)):`；`used` 聚合循环改为按 `mine` 过滤：
```python
    mine = request.query_params.get("mine") == "1" or getattr(user, "role", "") != "admin"
    used = {}  # model -> {"requests": n, "unpriced_requests": n, "cost_usd": x}
    led = (qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {})
    for uid, u in led.items():
        if mine and uid != user.id:
            continue
        for d in (u.get("days") or {}).values():
            ...
```
（其余价格匹配/override 提取逻辑原样保留。）

Run: `python -m pytest tests/test_endpoints.py -k "models_mine or models" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add quota_keeper_admin.py tests/test_endpoints.py
git commit -m "feat: /models?mine=1 - per-user used-model price list"
```

---

### Task 7: 前端 renderKpis / renderTrend 适配

**Files:**
- Modify: `quota_keeper_admin.py`（`renderKpis` 1771-1788、`renderTrend` 1792-1833）

**Interfaces:**
- Consumes: Task 2/3 的 series/kpi 结构
- Produces: 6 张卡：4 张曲线（Cost/Credits/Requests/Tokens）+ Requests 卡渠道小字 + Cache rate/Unpriced 合计小字

- [ ] **Step 1: 替换 `renderKpis()`**

```javascript
function renderKpis(){
  const k=STATE.stats.kpi||{},cpu=Number(STATE.cfg.credits_per_usd)||1000;
  const tk=k.tokens||{};
  const tot=(tk.cached||0)+(tk.input||0)+(tk.output||0);
  // series buckets carry {cost:{m:v}|{_:v}, requests, tokens} (Tasks 2/3);
  // Cost/Credits use the cost series, Requests/Tokens the new per-bucket series.
  const ser=STATE.stats.series||[];
  const costPer=ser.map(b=>Object.values(b.cost||{}).reduce((a,c)=>a+c,0));
  const reqPer=ser.map(b=>b.requests||0);
  const tokPer=ser.map(b=>b.tokens||0);
  const ch=k.channels||{};
  const ci=(tk.cached||0)+(tk.input||0);
  const cards=[
    {lbl:'Requests',val:fmt(k.requests||0,0),sp:sparkSvg(reqPer,140,34,'#c98500'),
     sub:`webui ${fmt(ch.webui||0,0)} · api ${fmt(ch.api||0,0)}`},
    {lbl:'Tokens',val:fmt(tot||0,0),sp:sparkSvg(tokPer,140,34,'#9085e9')},
    {lbl:'Cost $',val:'$'+fmt(k.cost_usd||0,2),sp:sparkSvg(costPer,140,34,'#38bdf8')},
    {lbl:'Credits',val:fmt((k.cost_usd||0)*cpu,0),sp:sparkSvg(costPer.map(v=>v*cpu),140,34,'#34d399')},
    {lbl:'Cache rate',val:fmt((k.cache_rate||0)*100,1)+'%',
     sub:`cached ${fmt(tk.cached||0,0)} / in ${fmt(ci,0)}`},
    {lbl:'Unpriced',val:fmt(k.unpriced_requests||0,0),
     sub:`of ${fmt(k.requests||0,0)} req`},
  ];
  $('kpis').innerHTML=cards.map(c=>`<div class="kpi"><div class="lbl">${c.lbl}</div><div class="val">${c.val}</div>${c.sp||''}${c.sub?`<div class="small muted">${c.sub}</div>`:''}</div>`).join('');
}
```

- [ ] **Step 2: `renderTrend()` 读 `b.cost`**

`renderTrend` 内三处 `b.by_model` 改 `b.cost`（`ser.forEach(b=>{Object.entries(b.cost||{})...`、`rows=ser.map(b=>{const bm=b.cost||{};...`），注释同步改为 `cost per bucket by model`。

- [ ] **Step 3: 验证（无前端测试基线，做结构断言 + 全量 pytest）**

Run: `python -c "s=open('quota_keeper_admin.py').read(); assert 'b.cost' in s and 'by_model' not in s.split('function renderTrend')[1].split('function renderUsers')[0]; assert 'webui ' in s and 'c.sub' in s; print('UI wiring ok')" && python -m pytest -q`
Expected: `UI wiring ok` + 85+ passed

- [ ] **Step 4: Commit**

```bash
git add quota_keeper_admin.py
git commit -m "feat: KPI cards - webui/api split, sparklines for Requests/Tokens, totals for Cache/Unpriced"
```

---

### Task 8: 前端 renderPersonal 重写（By model / Recent / Model prices）

**Files:**
- Modify: `quota_keeper_admin.py`（`renderPersonal` 1664-1697 + 其后新增函数；`STATE` 1625-1634 加字段）

**Interfaces:**
- Consumes: `/me/usage`（Task 4）、`/recent?mine=1`（Task 5）、`/models?mine=1`（Task 6）
- Produces: 个人页三个新区块；`STATE.personal={span,usage,recent,models}`

- [ ] **Step 1: `STATE` 加字段**

`STATE` 字面量中 `tou:null,` 后加：
```javascript
  personal:{span:localStorage.getItem('qk_myspan')||'7d',usage:null,recent:null,models:null},
```

- [ ] **Step 2: 替换 `renderPersonal()` 并新增 fetch/render 函数**

`renderPersonal` 末尾（`$('secPersonal').hidden=false;` 前）把原来单一 innerHTML 保留 4 卡 + 趋势，再在 `secPersonal` 后追加三个容器区块（直接包含在 `secPersonal` 的 innerHTML 里）：

innerHTML 末尾追加：
```javascript
   <div class="spans" style="margin-top:16px">
    <button data-myspan="7d" onclick="setMySpan('7d')">7d</button>
    <button data-myspan="30d" onclick="setMySpan('30d')">30d</button>
    <span class="small muted">by model · recent · prices</span>
   </div>
   <h3>By model</h3>
   <div class="scroll"><table id="myModelsT"><thead><tr>
    <th>Model</th><th class="num">Req</th><th class="num">Tokens</th><th class="num">Cost $</th><th>Share</th>
   </tr></thead><tbody><tr><td colspan="5" class="empty">loading…</td></tr></tbody></table></div>
   <h3>Recent activity <button style="margin-left:8px" onclick="loadMyRecent()">Refresh</button></h3>
   <div class="scroll"><table id="myRecentT"><thead><tr>
    <th>Time</th><th>Model</th><th>Via</th><th class="num">Cached</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cost $</th><th>Tier</th>
   </tr></thead><tbody><tr><td colspan="8" class="empty">loading…</td></tr></tbody></table></div>
   <h3>Model prices</h3>
   <p class="hint">价格为 0 的模型暂未匹配到价目；价格表由后台定期刷新，使用后会按最新价格自动计价（历史未计价记录由 admin reprice 回填）。</p>
   <div class="scroll"><table id="myPricesT"><thead><tr>
    <th>Model</th><th class="num">Input $/M</th><th class="num">Output $/M</th><th class="num">Cached $/M</th><th></th>
   </tr></thead><tbody><tr><td colspan="5" class="empty">loading…</td></tr></tbody></table></div>
```
并在 `$('secPersonal').hidden=false;` 后调用 `loadPersonal();`，新增：
```javascript
// ---------- personal page: by-model / recent / prices ----------
function setMySpan(k){STATE.personal.span=k;localStorage.setItem('qk_myspan',k);
  document.querySelectorAll('[data-myspan]').forEach(b=>b.classList.toggle('active',b.dataset.myspan===k));
  loadMyUsage();}
async function loadPersonal(){
  document.querySelectorAll('[data-myspan]').forEach(b=>b.classList.toggle('active',b.dataset.myspan===STATE.personal.span));
  loadMyUsage();loadMyRecent();loadMyPrices();  // three independent lazy loads, each toasts on failure
}
async function loadMyUsage(){
  try{STATE.personal.usage=await api('/me/usage?span='+STATE.personal.span);renderMyModels()}
  catch(e){toast('Usage load failed: '+e.message)}
}
function renderMyModels(){
  const u=STATE.personal.usage;if(!u)return;
  const rows=u.models||[],tot=rows.reduce((s,m)=>s+(m.cost_usd||0),0)||1;
  $('myModelsT').querySelector('tbody').innerHTML=rows.map(m=>{
    const t=m.tokens||{},tt=(t.cached||0)+(t.input||0)+(t.output||0),pct=(m.cost_usd||0)/tot*100;
    return `<tr><td>${esc(m.model)}</td><td class="num">${fmt(m.requests,0)}</td><td class="num">${fmt(tt,0)}</td><td class="num">$${fmt(m.cost_usd,4)}</td><td><div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div><span class="pct">${pct.toFixed(1)}%</span></td></tr>`;
  }).join('')||'<tr><td colspan="5" class="empty">No usage in this span.</td></tr>';
}
async function loadMyRecent(){
  try{STATE.personal.recent=await api('/recent?mine=1');renderMyRecent()}
  catch(e){toast('Recent load failed: '+e.message)}
}
function renderMyRecent(){
  const items=((STATE.personal.recent||{}).items||[]);
  $('myRecentT').querySelector('tbody').innerHTML=items.map(it=>{
    const t=it.tokens||{},dt=new Date((it.ts||0)*1000),p=n=>String(n).padStart(2,'0');
    const time=(dt.getMonth()+1)+'-'+p(dt.getDate())+' '+p(dt.getHours())+':'+p(dt.getMinutes());
    const tier=(it.tou_tier&&it.tou_tier!=='off')?`<span class="tag t-${esc(it.tou_tier)}">${esc(it.tou_tier)}</span>`:'';
    const chan=it.channel==='webui'?'webui':'api';
    return `<tr><td class="small muted">${time}</td><td>${esc(it.model)}${it.priced===false?' <span class="tag unpriced">unpriced</span>':''}</td><td><span class="tag ch-${chan}">${chan}</span></td><td class="num">${fmt(t.cached,0)}</td><td class="num">${fmt(t.input,0)}</td><td class="num">${fmt(t.output,0)}</td><td class="num">$${fmt(it.cost_usd,4)}</td><td>${tier}</td></tr>`;
  }).join('')||'<tr><td colspan="8" class="empty">No activity yet.</td></tr>';
}
async function loadMyPrices(){
  try{STATE.personal.models=await api('/models?mine=1');renderMyPrices()}
  catch(e){toast('Prices load failed: '+e.message)}
}
function renderMyPrices(){
  const items=((STATE.personal.models||{}).items||[]).slice().sort((a,b)=>String(a.model).localeCompare(String(b.model)));
  $('myPricesT').querySelector('tbody').innerHTML=items.map(it=>{
    const p=it.price||{};
    const tag=it.override?'<span class="tag manual">manual</span>':(it.matched?'':'<span class="tag unpriced">unmatched</span>');
    const f=v=>(typeof v==='number')?fmt(v,2):'0';
    return `<tr><td>${esc(it.model)}</td><td class="num">${f(p.input)}</td><td class="num">${f(p.output)}</td><td class="num">${f(p.cached)}</td><td>${tag}</td></tr>`;
  }).join('')||'<tr><td colspan="5" class="empty">No models used yet.</td></tr>';
}
```

- [ ] **Step 3: 验证**

Run: `python -c "s=open('quota_keeper_admin.py').read(); [print(f,'ok') for f in ['setMySpan','loadMyUsage','renderMyModels','loadMyRecent','renderMyRecent','loadMyPrices','renderMyPrices','/me/usage?span=','/recent?mine=1','/models?mine=1'] if f in s]; assert all(f in s for f in ['setMySpan','renderMyPrices','/models?mine=1'])" && python -m pytest -q`
Expected: 全部 `ok` + 测试全过

- [ ] **Step 4: Commit**

```bash
git add quota_keeper_admin.py
git commit -m "feat: personal page - by-model table, own recent feed, used-model prices (7d/30d)"
```

---

### Task 9: 版本 bump 0.5.0 + 全量验证 + push

**Files:**
- Modify: `quota_keeper_admin.py`（frontmatter `version:`）

- [ ] **Step 1: bump 版本**

`quota_keeper_admin.py` 第 4 行 `version: 0.4.8` → `version: 0.5.0`。
（`quota_keeper_filter.py` 本次未改，保持 0.4.8——版本号跟随各自文件的实际变更。）

- [ ] **Step 2: 全量验证**

Run: `python -m py_compile quota_keeper_admin.py quota_keeper_filter.py && python -m pytest -q`
Expected: 85+ passed

- [ ] **Step 3: Commit + push**

```bash
git add quota_keeper_admin.py
git commit -m "chore: bump admin version to 0.5.0 (UI enhancements)"
git push origin main   # 若 TLS 抖动：git -c http.sslVerify=false push origin main
```

- [ ] **Step 4: 手动验收清单（部署到实例后逐项过）**

- [ ] tab 显示仪表盘图标
- [ ] admin 6 张 KPI：Requests/Tokens/Cost/Credits 有曲线；Requests 卡有 `webui X · api Y`；Cache rate/Unpriced 有合计小字
- [ ] 24h/7d/30d/90d/custom 各 span 下曲线与小字正常
- [ ] 个人页（非 admin 账号）：7d/30d 切换、By model 占比条、Recent 只有本人、Model prices 未匹配显示 0 + 备注
- [ ] 非 admin 直接访问 `/api/v1/quota-keeper/recent`（不带 mine）也只看到本人；`/stats`、`/pricing` 仍 403
