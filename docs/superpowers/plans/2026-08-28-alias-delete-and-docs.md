# Alias 删除修复 + issue #2/#3 概念澄清 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复两处 alias 无法删除/切换的 bug（config 深合并只增不删 + price editor alias 分支叠加），并在 README/UI 澄清两个 GitHub issue 的概念误解。

**Architecture:** `POST /config` 对“整表编辑的 map”改为出现即替换（admin-only 路由内 pop-then-merge，不碰 shared helpers）；price editor 让显式清空 alias = 删除意图；文档+UI 文案澄清。filter 文件零改动。

**Tech Stack:** Python（FastAPI router + 内嵌 QK_PAGE HTML/JS）、pytest（含 jsdom 页面前端测试）。

**Spec:** `docs/superpowers/specs/2026-08-28-alias-delete-and-docs-design.md`

## Global Constraints

- **filter 文件不改**：replace 逻辑只存在于 `api_save_config`（admin-only 路由），shared helpers 段（`qk_deep_merge`/`qk_merge_config`/校验/`qk_find_pricing`/`qk_resolve_model_alias`）零改动 → 不触发“两文件同步”危险区。
- 行为变更：`POST /config` 中 `model_aliases`、`group_quotas`、`user_quotas`（顶层）与 `pricing.overrides`、`tou.providers`、`tou.models`（嵌套）由“部分合并”改为“出现即整体替换”；省略该 key = 不动。
- 版本号：`quota_keeper_admin.py` frontmatter `version: 0.5.38` → `0.5.39`。
- 每个任务结束跑 `python -m py_compile quota_keeper_admin.py quota_keeper_filter.py` 与相关 pytest。

## File Structure

- `quota_keeper_admin.py` — 后端 replace 语义 + 前端 `peEff`/`collectOverrides` + UI 文案 + 版本号。
- `quota_keeper_README.md` — 「概念澄清」小节 + 价格覆盖小节语义更新。
- `tests/test_config_api.py` — replace 语义后端测试。
- `tests/test_pe_overrides_jsdom.py` — price editor 前端测试。
- `tests/test_smoke.py` — UI 文案存在性断言。
- `quota_keeper_filter.py` — 不修改。

---

### Task 1: `model_aliases` replace 语义 + 空 spec 剥离（后端）

**Files:**
- Modify: `quota_keeper_admin.py`（`api_save_config`，当前 3436-3453 行）
- Test: `tests/test_config_api.py`

**Interfaces:**
- Produces: `api_save_config` 新行为——body 含 `model_aliases` 时整体替换；`pricing.overrides` 中的空 spec（`None`/`{}`）在落盘前剥离。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_config_api.py`）

```python
def test_model_aliases_replace_semantics(admin_client):
    # body 含 model_aliases 时整体替换：缺席的旧 key 被删除，不再是深合并残留。
    c, adm = _app(admin_client)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, {"model_aliases": {"a": "real-a", "b": "real-b"}})
    r = c.post("/api/v1/quota-keeper/config", json={"model_aliases": {"b": "real-b", "c": "real-c"}})
    assert r.status_code == 200
    cfg = adm.qk_load_json(adm.QK_CONFIG_PATH, {})
    assert cfg["model_aliases"] == {"b": "real-b", "c": "real-c"}  # "a" 被删


def test_model_aliases_clear_all(admin_client):
    # 清空 textarea → 前端发 {} → 现在应清空全部映射（此前是深合并 no-op，旧映射全残留）。
    c, adm = _app(admin_client)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, {"model_aliases": {"a": "real-a"}})
    r = c.post("/api/v1/quota-keeper/config", json={"model_aliases": {}})
    assert r.status_code == 200
    assert adm.qk_load_json(adm.QK_CONFIG_PATH, {}).get("model_aliases") == {}


def test_model_aliases_absent_untouched(admin_client):
    # 省略该 key = 不动（保留既有映射），保证普通保存不清空。
    c, adm = _app(admin_client)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, {"model_aliases": {"a": "real-a"}})
    r = c.post("/api/v1/quota-keeper/config", json={"credits_per_usd": 2000})
    assert r.status_code == 200
    assert adm.qk_load_json(adm.QK_CONFIG_PATH, {}).get("model_aliases") == {"a": "real-a"}


def test_overrides_empty_spec_stripped(admin_client):
    # replace 语义下 null 墓碑与 {} 空 spec 都在落盘前剥离，避免 /models 把 {} 误判成 manual。
    c, adm = _app(admin_client)
    adm.qk_atomic_write(
        adm.QK_CONFIG_PATH,
        {"pricing": {"overrides": {"m1": {"alias": "x"}, "m2": {"input": 1}}}},
    )
    r = c.post(
        "/api/v1/quota-keeper/config",
        json={"pricing": {"overrides": {"m1": None, "m2": {}}}},
    )
    assert r.status_code == 200
    ov = adm.qk_load_json(adm.QK_CONFIG_PATH, {})["pricing"]["overrides"]
    assert ov == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_config_api.py -k "model_aliases or empty_spec" -v`
Expected: FAIL（`model_aliases` 仍含旧 key；overrides 残留 `None`/`{}`）

- [ ] **Step 3: 实现**

在 `quota_keeper_admin.py` 的 `api_save_config` 中，把 `with qk_lock():` 块替换为：

```python
    with qk_lock():
        cur = qk_load_json(QK_CONFIG_PATH, {})
        if not isinstance(cur, dict):
            log.warning("quota-keeper config.json was not an object; starting from defaults")
            cur = {}
        # whole-map keys: the admin UI always edits/saves these as a full map,
        # so a key present in the body REPLACES the stored map (pop-then-merge)
        # instead of deep-merging into it -- otherwise deletions can never be
        # saved (the deep merge is add/overwrite-only). Absent key = untouched.
        for k in ("model_aliases", "group_quotas", "user_quotas"):
            if k in body:
                cur.pop(k, None)
        _pri = body.get("pricing")
        if isinstance(_pri, dict) and "overrides" in _pri and isinstance(cur.get("pricing"), dict):
            cur["pricing"].pop("overrides", None)
        _tou = body.get("tou")
        if isinstance(_tou, dict) and isinstance(cur.get("tou"), dict):
            for k in ("providers", "models"):
                if k in _tou:
                    cur["tou"].pop(k, None)
        qk_deep_merge(cur, body)
        cfg = qk_merge_config(cur)
        # strip empty override specs (None tombstones retired by replace, and
        # {} rows) so /models never reports a bare {} as a manual override
        _ov = (cfg.get("pricing") or {}).get("overrides")
        if isinstance(_ov, dict):
            cfg["pricing"]["overrides"] = {
                k: v for k, v in _ov.items() if isinstance(v, dict) and v
            }
        qk_atomic_write(QK_CONFIG_PATH, cfg)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_config_api.py -v`
Expected: 全 PASS（含旧的 `test_partial_post_preserves_siblings`，确认 pricing 兄弟键仍保留）

- [ ] **Step 5: Commit**

```bash
git add quota_keeper_admin.py tests/test_config_api.py
git commit -m "fix(config): whole-map keys replace-on-save + strip empty override specs

model_aliases/group_quotas/user_quotas (top) and pricing.overrides/
tou.providers/tou.models (nested) now replace the stored map when present
in the POST body instead of deep-merging (add-only). Empty override specs
(None tombstones and {}) are stripped before write so /models never shows
a bare {} as a manual override. Fixes the 'alias can't be deleted' half of
issue #2."
```

---

### Task 2: Price editor alias 可清除、可切手动价格（前端）

**Files:**
- Modify: `quota_keeper_admin.py`（`peEff` 当前 3090-3103 行、`collectOverrides` 当前 3174-3218 行、alias 输入行 3137）
- Test: `tests/test_pe_overrides_jsdom.py`

**Interfaces:**
- Consumes: `STATE.pe.orig[model] = {manual, cleared, cur, base, ...}`（`rebuildPeOrig` 产出）；Task 1 的 replace 语义保证“不发该行 = 删除该 spec”生效。
- Produces: `collectOverrides()` 对“显式清空 alias 且有手动价格”的行发 `{prices}`；对“清空 alias 且无任何内容”的行不发该 key。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_pe_overrides_jsdom.py`）

在现有 jsdom 驱动方式基础上，新增：给一个已存 `{alias:"gpt-x"}` override 的行清空 alias 输入框并填 manual price，触发保存，断言 POST body 中该行 override 为 `{prices: {...}}` 且**不含** `alias` 键；再做一个“清空 alias 且价格为全空”的行，断言 POST body 中**不出现**该模型 key。驱动代码复用本文件已有的 `_extract_page_js()` + jsdom + fetch stub（参照 `test_overrides_save_preserves_non_visible_rows` 的写法），核心断言：

```python
    # 行 "m-alias" 原 override = {"alias": "gpt-x"}；用户清空 alias 框并填 input=2
    posted = ...  # 抓取 /config POST 的 JSON body
    ov = posted["pricing"]["overrides"]
    assert "alias" not in ov["m-alias"]
    assert ov["m-alias"]["prices"]["input"] == 2
    # 行 "m-empty" 清空 alias 且价格全空 -> 整个 key 不出现（replace 下 = 删除）
    assert "m-empty" not in ov
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_pe_overrides_jsdom.py -v`
Expected: FAIL（`ov["m-alias"]` 仍含 `alias`；`m-empty` 仍被发出）

- [ ] **Step 3: 实现**

(a) `peEff` 中 alias 回退行改为显式清空优先：

```javascript
  return {prices:{input:fb('input'),cached:fb('cached'),cache_write:fb('cache_write'),output:fb('output')},
          alias:(c.alias!==undefined&&c.alias!==null)?c.alias:b.alias,mult:(c.mult!==''&&c.mult!==null&&c.mult!==undefined)?c.mult:b.mult};
```

（`cur` 一旦由 `peEdit` 建立，`c.alias` 总是字符串：清空框 → `''` → 显式删除，不再回退 `b.alias`。）

(b) `collectOverrides` 的 alias 分支改为看 `cur` 的显式值；其后按价格/倍率/空分流：

```javascript
  Object.entries(STATE.pe.orig).forEach(([k,o])=>{
    if(o.cleared){return} // cleared rows now simply drop the key (replace-on-save: absent = deleted)
    const eff=peEff(o);
    if(!eff)return;
    const hasMult=eff.mult!==''&&eff.mult!==null&&eff.mult!==undefined&&!isNaN(eff.mult);
    const explicitAlias=o.cur&&o.cur.alias!==undefined&&o.cur.alias!==null?o.cur.alias:null;
    const aliasVal=explicitAlias!==null?explicitAlias:eff.alias;
    if(aliasVal){
      const out={alias:aliasVal};
      if(hasMult)out.multiplier=Number(eff.mult);
      ov[k]=out;
      return;
    }
    if(!o.cur)return; // untouched rows never emit (replace preserves via absent=delete only for edited rows; unedited manual rows must still emit)
    const cp=(o.cur&&o.cur.prices)||{};
    const userSetPrice=Object.values(cp).some(v=>v!==null&&v!==undefined);
    if(userSetPrice){
      const out={prices:eff.prices};
      if(hasMult)out.multiplier=Number(eff.mult);
      ov[k]=out;
      return;
    }
    if(hasMult){ov[k]={multiplier:Number(eff.mult)};return}
    // explicit clear of an alias with nothing else -> emit nothing: under
    // replace-on-save the absent key deletes the stored override.
    if(o.manual&&explicitAlias===''){return}
  });
```

注意保留旧行为：未编辑的 manual 行仍需发 `base`（否则 replace 会误删）。把 `if(!o.cur)return` 之前补一条：manual 且未编辑的行发 `base`：

```javascript
    if(!o.cur){
      if(o.manual){
        const b=o.base,out={};
        if(b.alias){out.alias=b.alias}
        else{const p={};['input','cached','cache_write','output'].forEach(f=>{if(b.prices[f]!==null&&b.prices[f]!==undefined)p[f]=b.prices[f]});if(Object.keys(p).length)out.prices=p}
        if(b.mult!==''&&b.mult!==null&&b.mult!==undefined)out.multiplier=Number(b.mult);
        if(Object.keys(out).length)ov[k]=out;
      }
      return;
    }
```

(c) alias 输入框（当前 3137 行）placeholder 改为提示可清空删除：

```javascript
   <td><input class="pe-alias" type="text" placeholder="alias key; empty+save = delete" data-pk="${esc(k)}" data-f="alias" value="${esc(cur.alias||'')}" ${adis} oninput="peEdit(this)"/></td>
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_pe_overrides_jsdom.py tests/test_config_api.py -v`
Expected: 全 PASS（含旧的 `test_overrides_save_preserves_non_visible_rows`，确认未破坏“不发该行 ≠ 丢隐藏行”的旧修复）

- [ ] **Step 5: Commit**

```bash
git add quota_keeper_admin.py tests/test_pe_overrides_jsdom.py
git commit -m "fix(pricing-editor): clearing an alias now deletes it / allows manual prices

peEff no longer falls back to the stored alias when the input is explicitly
emptied; collectOverrides reads the explicit cur value so a cleared alias
with manual prices saves {prices} (no alias key), and a cleared alias with
nothing else drops the key (replace-on-save deletes the stored override).
Fixes the alias/prices stacking in issue #2."
```

---

### Task 3: README「概念澄清」+ 价格覆盖小节更新

**Files:**
- Modify: `quota_keeper_README.md`

**Interfaces:** 无（纯文档）。

- [ ] **Step 1: 在「价格覆盖（Pricing Overrides）」小节后插入「概念澄清」小节**

在 `quota_keeper_README.md` 第 58 行（`## 管理台` 之前）插入：

```markdown
## 概念澄清（issue #2 / #3 常见误解）

- **两处 alias 是两码事**：
  - `Model aliases`（Pricing source 顶部，`model_aliases`）是**改名合并统计**——把上游回显的别名（如 `prx.gemini-flash`）归并到真实模型名下计费与统计。**它不做定价**；自建模型不在上游价格表里时，这里填 alias 也不会产生匹配，请改用 Pricing editor 的**直接填价**。
  - Pricing editor 行内的 **alias** 是**引用上游价格表已有 key 的价格**——`k3-256k → alias: kimi-k3 × 0.5` 表示按 kimi-k3 价打 5 折。**只影响计价，不改变统计归属**，且 alias 目标必须真实存在于上游价格表（用 Test match 验证）。
- **Rename/merge（模型表行尾）是修复工具，不是定价配置**：把某个名字（陈旧 alias、`unknown`）在 ledger 与 recent 里的**全部历史桶**物理合并到目标模型名下（请求/tokens/cost 求和，所有用户、所有历史），用于纠正历史记账错误；合并后若原行未定价会提示 reprice 回填成本。
- **Filter 是全局计量器，per-model 开关管不住它**：Filter 注入在 API 底层入口做计量，不按模型启停；Open WebUI 的 passthrough 直连模式不走标准 filter 的 inlet/outlet，也会被计量（这是设计使然，不是 per-model 设置失效）。
- **配置的保存语义**：`model_aliases`、各配额表、`pricing.overrides`、TOU 的 providers/models 均为**整体替换**——界面保存时按当前所见全量写入；清空某映射并保存即删除。直接调 `POST /config` 时同理：出现即替换，省略不动。
```

同时把「价格覆盖」小节中 `null = 清除该行覆盖` 一句更新为：

```markdown
- 覆盖值三种形态（`pricing.overrides`）：`{"prices": {...}}` 直接定价；`{"alias": "key", "multiplier": m}` 别名；裸价格 dict 为旧格式仍兼容。编辑器行尾 **clear** 或把 alias 框清空后保存即删除该行覆盖（整体替换语义）。
```

- [ ] **Step 2: 校验 markdown 无语法错误、链接锚点合理**

Run: `grep -n "概念澄清" quota_keeper_README.md`
Expected: 命中新小节标题

- [ ] **Step 3: Commit**

```bash
git add quota_keeper_README.md
git commit -m "docs(readme): clarify the two aliases, rename's purpose, Filter's global scope, replace-on-save semantics (issues #2/#3)"
```

---

### Task 4: UI 文案澄清（model_aliases 标签 + rename 提示）+ smoke 断言

**Files:**
- Modify: `quota_keeper_admin.py`（label 2294、rename `confirm` 文案 2899 区域）
- Test: `tests/test_smoke.py`

**Interfaces:** 无。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_smoke.py`）

```python
def test_alias_concept_hints_present():
    """UI hints pinning the issue #2/#3 clarifications stay in the page."""
    page = _page_source()
    assert "does not set prices" in page            # model_aliases label: naming map, not pricing
    assert "repair tool" in page                     # rename block: repair, not pricing config
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: FAIL（两个断言的文案尚不存在）

- [ ] **Step 3: 实现**

(a) `model_aliases` label（当前 2294 行）改为：

```html
  <label>Model aliases (upstream alias → real model name, JSON; e.g. {"prx.gemini-flash":"gemini-3.7-flash"} — merges the alias into the real model's stats/pricing. This is a naming map for stats: it does <b>not</b> set prices (use the Pricing editor's direct prices / alias for that). Saving writes the whole map — empty an entry and save to delete it.)</label>
```

（smoke 断言里的 `does not set prices` 来自 `<b>not</b> set prices` 去标签后的文本——断言直接查 `not</b> set prices` 更稳，见 Step 1 调整。）

修正 Step 1 断言为精确匹配标签文本：

```python
    assert "not</b> set prices" in page              # model_aliases label: naming map, not pricing
```

(b) rename 的 `confirm` 文案（当前 2899 行）开头补一句：

```javascript
  if(!confirm(`This is a REPAIR tool (not a pricing setting). Merge every ledger bucket and recent-activity entry named "${src}" into "${dst}"?\n\nRequests/tokens/cost are summed into "${dst}" (all history, every user); "${src}" disappears from the tables. If the merged rows were unpriced, you will be asked to reprice "${dst}" next to backfill their cost.`))return;
```

（`REPAIR tool` 小写化为 `repair tool` 不命中断言；把 Step 1 断言改为 `"REPAIR tool"`。）

修正 Step 1 第二条断言：

```python
    assert "REPAIR tool" in page                     # rename block: repair, not pricing config
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_smoke.py tests/test_pe_overrides_jsdom.py -v`
Expected: PASS（jsdom 的 `test_served_page_js_parses` 确认改过的 JS/HTML 仍解析）

- [ ] **Step 5: Commit**

```bash
git add quota_keeper_admin.py tests/test_smoke.py
git commit -m "docs(ui): label model_aliases as a naming map (not pricing), rename as a repair tool

Pins the issue #2/#3 clarifications in the page itself."
```

---

### Task 5: 版本号 + 全量回归

**Files:**
- Modify: `quota_keeper_admin.py`（frontmatter `version`）

**Interfaces:** 无。

- [ ] **Step 1: 版本号 `0.5.38` → `0.5.39`**

`quota_keeper_admin.py` 第 4 行：

```
version: 0.5.39
```

- [ ] **Step 2: 全量回归**

Run: `python -m py_compile quota_keeper_filter.py quota_keeper_admin.py && python -m pytest tests/ -v`
Expected: 全 PASS（jsdom 测试在无 node/jsdom 时会 skip，属预期）

- [ ] **Step 3: Commit**

```bash
git add quota_keeper_admin.py
git commit -m "chore: bump admin to 0.5.39 (alias delete fix + docs clarifications)"
```

---

## Self-Review 记录

- **Spec 覆盖**：spec §1→Task1、§2→Task2、§3（README+UI）→Task3/Task4、§5 测试→各 Task 内嵌、§6 版本→Task5。统计影响（spec §4）为审核结论，无需代码任务。
- **Placeholder**：各步骤均含完整代码/命令。
- **类型一致性**：`collectOverrides`/`peEff` 的 `cur.alias`（`''` = 显式清空）与 `rebuildPeOrig` 的 `base.alias` 字符串约定一致；replace key 清单在两处（后端 pop、README 语义段）一致。
- **风险点**：Task 2 的“未编辑 manual 行仍发 base”是为了在 replace 语义下保住既有 override（否则打开过编辑器后保存会误删未动过的行）——这是 replace 引入的必要配套，已在代码注释标明。
