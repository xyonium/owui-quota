# 设计：修复 alias 删除 bug + 消除两个 GitHub issue 的概念误解

日期：2026-08-28
来源 issue：#2「Model aliases won't affect pricing and can not be deleted」、#3「Filter tracks all API calls」

## 背景与根因

`POST /config` 用 `qk_deep_merge` 合并请求体：对 dict 递归合并、其余覆盖。结果对已存在的 map 类配置**只增不删**——body 里缺席的 key 原样保留，发空对象 `{}` 是 no-op。而前端 `saveConfig` 对一批 map 本来就发**全量**（意图是替换），深合并把它变成了合并，于是一整类删除全部失灵。

两处真 bug：

- **A. `model_aliases`（Pricing source 顶部 textarea）无法删除/改 key**——清空 textarea → 前端发 `{}` → 深合并 no-op → 旧映射全残留；想用 `null` 当墓碑 → 校验拒绝（`model_aliases.{k} must map to a non-empty model name`）；改 key = 旧 key 残留 + 新 key 加入（用户看到的“所有更改都是添加”）。
- **B. price editor 里已存 alias 的行无法切回手动价格/删掉 alias**——`peEff` 把显式清空的 `cur.alias=''` 回退到已存 `b.alias`（视为“没改”）；`collectOverrides` 的 alias 分支优先，只要 `eff.alias` 非空就发 `{alias}`、永不发 `{prices}` → 填手动价格时与已存 alias 深合并成 alias+prices 叠加。

## 决策（已与用户确认）

1. 范围：A、B 两处都修。
2. 删除机制：`POST /config` 对“整表编辑的 map”改为**出现即替换**（先从 `cur` pop 掉再深合并），其余 key 合并行为不变。
3. 文档：README + UI 双管齐下。

## 设计

### 1. 后端替换语义（仅 `quota_keeper_admin.py`，不碰 shared helpers）

`api_save_config` 在 `qk_deep_merge(cur, body)` 之前，对以下 key 执行「body 中出现即从 `cur` pop」：

- 顶层：`model_aliases`、`group_quotas`、`user_quotas`
- 嵌套：`pricing.overrides`、`tou.providers`、`tou.models`

语义：发 `model_aliases: {}` = 清空全部映射；省略该 key = 不动；直调 API 的调用者从“部分更新”变“整体替换”（GET→改→POST 即可，README 注明）。

合并后**剥离 `pricing.overrides` 里的空 spec**（`None` 墓碑与 `{}` 一并清除）：replace 语义下缺席即删除，null 墓碑退役（老 config.json 已存的 null 读取路径本就兼容，无需迁移）。空 spec 剥离同时修掉一个附带 bug：`api_models` 判 override 只查 `v is not None`，残留的 `{}` 会被误判成 manual/`matched ✓`。

校验、Filter 侧**零改动**（替换逻辑在 admin-only 路由里）。

### 2. Price editor：alias 可清除、可切手动价格

- `peEff`：`cur.alias===''`（显式清空）不再回退到 `b.alias`——清空输入框 = 删除意图。
- `collectOverrides`：alias 分支改为看 `cur`——显式清空后不再优先 alias；其后按：有手动价格 → `{prices}`（含 multiplier）；只有倍率 → `{multiplier}`；什么都没有 → **不发该行**（= replace 语义下删除整个 spec）。
- alias 输入框 placeholder/行提示注明：「清空并保存即删除 alias；alias 与手动价格互斥，alias 优先」。

### 3. 文档与界面文案

- **README（中文）**新增「概念澄清」小节：
  - 两处 alias 的区别：`model_aliases` = 改名合并统计（**不定价**，自建模型请用手动价格）；price editor 的 alias = 引用上游表已有价格（**只影响计价不影响统计归属**，目标必须在上游表）。
  - `/models/rename` 是**修复工具**：合并 ledger 历史桶（陈旧 alias / unknown 行），改完跑 reprice；不是定价配置。
  - Filter 是**全局计量器**：注入在 API 底层，per-model 开关管不住它；passthrough 直连模式不走标准 filter inlet/outlet 也会被计量。
  - 注明 config 的替换语义（发即替换）。
- **UI**：`model_aliases` 标签、price editor hint、rename 区块各加一句澄清。

### 4. 统计一致性影响（已审核）

- `model_aliases` 删除/变更：历史 ledger 永远只写一份（落在解析后的真名下），`qk_resolve_model_alias` 幂等（已归真的名字查不到映射则原样）。删映射最坏是 split（历史在旧名、未来在新名，**不重复**），可用 `/models/rename` 合并恢复；改映射同理 split。
- `pricing.overrides` 删除/变更：价格在**记录时**已换算成 `cost_usd` 落盘，改 override 不动历史聚合；仅删除后该模型变回 no-match（未来 `cost=0`、`unpriced_requests` 升），属预期且 editor 会显示。
- 结论：本次改动对历史统计向后兼容、无重复计数风险。

### 5. 测试与验证

- `tests/test_config_api.py`：替换语义——清空 `model_aliases` 生效、删一个 alias 生效、缺省不动、`overrides` 空 spec（null/`{}`）剥离、`group_quotas`/`user_quotas` 清空即删、`tou.providers`/`tou.models` 删行生效。
- `tests/test_pe_overrides_jsdom.py`：清空 alias → 发 `{prices}` 或不发该行；alias 行填价格不再叠加成 alias+prices。
- `tests/test_smoke.py`：新增 UI 文案存在性断言。
- `python -m py_compile quota_keeper_filter.py quota_keeper_admin.py` + 全测试跑通。

### 6. 版本与提交

两个 `.py` 的版本号按惯例递增；改动单独一笔 commit（CHANGELOG/commit message 说明 replace 语义为行为变更）。
