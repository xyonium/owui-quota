# Quota Keeper for Open WebUI — 开发交接文档

> 版本：v0.1.1（2026-08）· 状态：核心逻辑已实现并通过单元测试，未经生产验证
> 用途：bug 修复 / 后续开发交接。请先读完全文再动代码。

---

## 1. 项目背景

Open WebUI（github.com/open-webui/open-webui，148k stars）原生 Analytics 有两个关键缺口：

1. **API 调用不记 token**：Analytics 流水线依赖 `event_emitter`（需要 `chat_id`/`session_id`/`message_id`，由 Web 前端生成）。直接 API 调用（curl / OpenCode / Continue.dev 等）没有这些参数，流式处理器进入直通分支，usage 数据在 SSE 流里但没人读。参考 issue #23926（closed as duplicate）、Discussion #23558。
2. **无配额强制**：Analytics 只展示不拦截。per-user/per-group limit 的 builtin 请求（#23323、#1430、#6692）长期未合并。CUNY 团队在 Discussion #23558 给出了未合并的 680 行实现方案（usage_ledger 表 + group permissions JSON 存限额 + soft/hard limit + 429）。

相关生态项目：
- **dartmouth/openwebui-token-tracking**：基于 pipes 的 credit 制配额库（1000 credits=$1，credit group / sponsored allowance），要求所有模型走 pipe。
- **LiteLLM proxy**：企业级方案（virtual key budget / per-model budget），多一个独立组件。
- **Classic298/open-webui-plugins 的 prune 插件**：Event Function 在启动时向 `__app__` 注册路由 + `/prune` 管理页。**本项目的网页模式即参照它。**
- **Willxup/cpa-usage-keeper**：从上游同步模型定价做成本估算。**本项目的价格拉取+匹配思路参照它。**

## 2. 需求（用户原始要求）

1. 用 **Filter** 实现（非 pipe、非外部组件），计量网页 + API 直连两种访问的 token 消耗。
2. 有 `/quota` 可访问的网页配置 quota（类似 prune 的 `/prune`）。
3. quota 可按 **group** 设置，也可按 **个人** 设置；**个人优先级高于 group；多个 group 时取最高**。
4. usage 细分 **cached / input / output** 分别计费（cache_write 也计入成本）。
5. 价格表**从上游 URL 拉取**，支持**模糊匹配（含后缀匹配）**（cpa-usage-keeper 风格）。

（另：早前讨论还提出过分时段昼夜/周末差异化配额——已以"时段倍数"形式实现：夜间/周末各一个 multiplier，相乘作用于生效配额。）

## 3. 交付文件（/mnt/uploads/）

| 文件 | 大小 | 角色 |
|------|------|------|
| `quota_keeper_filter.py` | ~20KB | **Filter Function**：计量 + 拦截。挂模型或 Global。 |
| `quota_keeper_admin.py` | ~31KB | **Event Function**：注册 `/quota` 页面 + `/api/v1/quota-keeper/*` + 后台价格刷新。需 OWUI ≥ 0.10.0。 |
| `quota_keeper_README.md` | ~3.5KB | 用户向安装说明。 |
| 本文档 | — | 开发交接。 |

两个 .py 均为**自包含单文件**；共享的 helper（路径/锁/缓存/定价解析/配额解析）在两文件中**故意重复**（Open WebUI Function 以单文件为单位加载，无法跨文件 import）。**修改共享逻辑时必须同步改两处**——这是最需要注意的坑。

## 4. 运行架构

```
Open WebUI 实例
├── Filter(quota_keeper_filter)  ← 挂在模型上或 Global
│   ├── inlet()   拦截点：解析配额(user>max group>default)×时段倍数，
│   │             查当期已用 credits，超出 raise QuotaBlocked
│   ├── stream()  流式：解析每个 event(str SSE 或 dict)，取 terminal chunk
│   │             的 usage → qk_record_usage()
│   └── outlet()  非流式：取 body.usage 或 choices[0].usage → 记账；
│                 兼做 stream 阶段缺 user 信息时的 orphan 认领
├── Event(quota_keeper_admin)
│   └── system.startup.completed / function.enable_started
│       ├── app.include_router(api, prefix="/api/v1/quota-keeper")  # Depends(_require_admin)
│       ├── app.get("/quota")  → 内嵌单页 HTML（QK_PAGE 常量，约200行 JS）
│       └── create_task(_pricing_loop)  # 每600s检查，按 refresh_hours 真正刷新
└── $DATA_DIR/quota_keeper/          # DATA_DIR env，默认 /app/backend/data
    ├── config.json        # 全部配置（网页编辑产生）
    ├── ledger.json        # 记账账本（Filter 写，网页读）
    ├── pricing_cache.json # 价格表缓存（Event 写，Filter 读）
    └── .lock              # fcntl 文件锁（Windows 退化为 threading.Lock）
```

### config.json 结构（DEFAULT_CONFIG 为权威 schema）

```jsonc
{
  "credits_per_usd": 1000.0,          // 1000 credits = $1
  "quota_period": "daily",            // daily | monthly（月=自然月，本地时区）
  "default_quota_credits": null,      // null=不限
  "user_quotas": {"<user_id>": 500.0},
  "group_quotas": {"<group_id>": 2000.0},
  "ledger_retention_days": 400,
  "pricing": {
    "url": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
    "refresh_hours": 24,
    "default_pricing": null,          // 全局兜底价 {"input":..,"cached":..,"cache_write":..,"output":..} 每1M
    "overrides": {"<model>": {...}}   // 人工覆盖，优先级最高；网页暂无编辑UI，只能手改JSON
  },
  "schedule": {
    "timezone": null,                 // null→$TZ→UTC
    "night_start_hour": 22, "night_end_hour": 8,   // 可跨零点
    "night_multiplier": 1.0, "weekend_multiplier": 1.0
  }
}
```

### ledger.json 结构

`users.<uid>.days.<YYYY-MM-DD> = {requests, cost_usd, tokens:{cached,input,output}, models:{<model>:{requests,cost_usd,tokens,priced}}}`。
- 天 key 用**配置时区的本地日期**（qk_local_now）。
- `priced=false`：该模型当期出现过无匹配且无兜底 → 记账 cost 0 + 网页打 unpriced 标签。注意它是"与"累积：一旦 false 不再翻回 true（`mm["priced"] = mm.get("priced",True) and priced`）。

## 5. 核心算法

### 5.1 usage 归一化 qk_normalize_usage（两文件各自有一份 in filter；admin 不需要）

兼容三种格式，输出 `{cached, input, output, cache_write}`：
- **OpenAI**: `prompt_tokens - prompt_tokens_details.cached_tokens` = input；cached = cached_tokens。
- **Anthropic**: `input_tokens`/`output_tokens` + `cache_read_input_tokens`/`cache_creation_input_tokens`（Anthropic 的 input_tokens **不含** cache_read，所以 cached 单独存，input 直接用）。
- 全零 → 返回 None（不记账）。

### 5.2 价格匹配 qk_find_pricing（filter + admin 各一份，逻辑必须一致）

输入统一 lower。变体集 `_qk_variants(m)`：`[原串, . → - 版本, 去日期后缀版, 去日期+点转杠版]`。
日期后缀正则：`r"[-:_.](20\d{2}[-_.]?\d{2}[-_.]?\d{2}|\d{6})$"`（剥 `-2024-08-06` / `-20241022`）。

匹配顺序（每个策略内部都对全部变体尝试）：
1. `override:<k>` — pricing.overrides 精确命中
2. `exact:<k>` — 表内精确
3. `suffix:<k>` — 模型 id 以 `/<key>` 结尾（`openai/gpt-4o`→`gpt-4o`），取最长 key
4. `segment:<k>` — 去掉前导路径段后命中
5. `contains:<k>` — key(len≥4) 是模型 id 子串，取最长（`myorg.proxy.gpt-4o`→`gpt-4o`）
6. 无命中 → default_pricing 兜底，否则 cost=0 且 priced=false

已验证用例（回归基准）：
```
gpt-4o→exact / gpt-4o-2024-08-06→exact(去日期) / openai/gpt-4o→suffix
gpt-4o.2024-08-06→exact(点转杠) / claude-3-5-haiku-20241022→exact
bedrock.us-east-1/anthropic.claude-3-5-haiku→contains / totally-unknown→None
```

### 5.3 配额解析 qk_resolve_quota

`user_quotas[uid] > 0` → user；否则所属 groups 在 group_quotas 中的正值取 **max** → group；否则 default > 0 → default；否则 None（不限）。
组列表来源：`user["group_ids"]`（Open WebUI 注入）优先，缺失时 fallback `open_webui.models.groups.Groups.get_groups_by_member_id(uid)`（**在 Filter 进程内可用；沙箱测试无此模块**）。

### 5.4 时段倍数 qk_time_multiplier

`mult = 1`；周六/周日(weekday≥5) 且 weekend_multiplier≠1 → ×w；处于 [night_start, night_end)（支持跨零点：ns>ne 时 `h>=ns or h<ne`）且 night_multiplier≠1 → ×n。
生效配额 = 解析配额 × mult。mult<1 表示收紧，>1 放宽。已验证：Sat23:00(0.5×0.5)=0.25、Mon23:00=0.5、Mon12:00=1.0。

### 5.5 价格拉取 qk_fetch_pricing（admin 独有）

自动识别两种格式：
- **LiteLLM** flat：检测任一 value 含 `input_cost_per_token`/`output_cost_per_token`；per-token × 1e6 → per-1M；映射 `cache_read_input_token_cost`→cached、`cache_creation_input_token_cost`→cache_write。
- **models.dev** nested：`{provider:{models:{name:{cost:{input,output,cache_read,cache_write}}}}}`，per-1M 直接用；同时写 `prov/name` 与裸 `name` 两个 key。
缓存 `_pricing_loop` 每 600s 唤醒，`refresh_hours` 内不重复拉；`POST /pricing/refresh {force:true}` 强刷。

### 5.6 并发与持久化

- 全部写操作走 `qk_lock()`（fcntl flock，跨 worker；Windows/异常退化为进程内 Lock——**多 worker + Windows 场景有丢写风险，见 §8**）。
- 原子写：tmp + fsync + os.replace。
- 读走 `_JsonCache`（mtime 失效缓存），记账读 ledger 用缓存，写时直读+加锁。

## 6. API 与页面

路由（prefix 由 Event Valves `api_prefix` 控制，默认如下）：
```
GET  /api/v1/quota-keeper/config           # 附加 _time_multiplier
POST /api/v1/quota-keeper/config           # 网页保存（merge 后全量写）
GET  /users /groups /ledger /pricing       # 数据源（users/groups 来自 OWUI DB）
POST /pricing/refresh                      # {force}
GET  /pricing/match?model=...              # {matched, how, price}
```
全部经 `Depends(_require_admin)`（`open_webui.utils.auth.get_verified_user` + role==admin，401/403 JSON）。

页面 `/quota`（Valves `route_prefix`）：单页内嵌 HTML，fetch 同源 API；区块：General / 时段 / 价格源(含 Test match) / 组配额表 / 用户配额表(来源 tag + 用量进度条) / 当月 per-model 用量表(unpriced 标记)。

Filter Valves：`enable_enforcement`(true)、`admins_bypass`(true)、`allow_background_tasks`(true, 后台任务放行但记账)、`estimate_unreported_tokens`(false, 按 chars/4 估算)、`block_message`(占位符 `{used}{quota}{source}{mult}`)。

## 7. 已验证内容（Pyodide 沙箱 + pydantic stub，非真实 OWUI 环境）

- 两文件 ast 语法 OK。
- usage 归一化：OpenAI(cached 分离)/Anthropic(cache_read/creation)/全零→None。
- 匹配：上节 7 个用例全过；override 优先。
- 配额解析：user 覆盖 / 组取 max / default / 不限，四态正确。
- 时段倍数：四时间点正确（含跨零点、周末叠加）。
- 成本计算：`(input×2.5 + cached×1.25 + output×10)/1e6` 精确一致；cache_write 计入。
- 记账 round-trip：日聚合 + 模型聚合 + priced 标志 + retention 裁剪路径。
- inlet 拦截：超限 raise QuotaBlocked（消息含 used/quota/source/mult）、admin bypass、无配额放行、夜间倍数收紧触发拦截而白天放行。
- stream()：SSE str 与 dict 事件、按响应 id 去重（重复同 id 不重记）。
- outlet()：非流式 body.usage、choices[0].usage 回退。

**未验证 / 需在真实环境确认**（见下节）。

## 8. 已知限制与潜在 bug（后续开发重点）

按优先级排序：

1. **真实 OWUI 集成未经测试**（最重要）：filter 的 `__user__` 注入形态（`group_ids` 是否存在）、`__metadata__.task` 后台任务标记、stream() 收到的 event 具体形状（字符串 SSE 还是 dict、终止 chunk 是否带 usage——官方文档明说"形状因 connector 而异"）、outlet 在 API 直连时是否被调用。**首先做一轮真机日志验证**（log.warning 已埋好）。
2. **函数热重载重复注册**：`_installed` 是实例标志，OWUI 重载 function 模块会新建实例，`include_router` 可能重复挂载（API 405/多路由）；页面路由有 `path` 查重保护但 API 没有。修复方向：挂载前查 `__app__.routes` 是否已含 prefix，或改用 startup-only + Redis/disk 标志。
3. **usage 可能漏计**：若上游流式响应从不返回 usage 且未开模型 Usage capability → 不记账。`estimate_unreported_tokens` 是粗略兜底（chars/4）。改进方向：主动给请求注入 `stream_options:{include_usage:true}`（参照 open-webui PR #23556 对 Bedrock 的做法）。
4. **orphan 认领路径窄**：stream 阶段无 user 时存 `_orphan`，只有 outlet 且 `estimate_unreported_tokens=True` 且响应带同 id 才认领。真实环境若 API 直连根本不进 filter 的 outlet，这段逻辑无效（预期内：直连计量主要靠 stream 的 usage + `__user__` 注入）。
5. **Windows/无 fcntl 多 worker**：锁退化为进程内，SQLite 式丢写风险（atomic replace 可缓解但 ledger 读改写非事务）。修复方向：改 SQLite 或加版本号重试。
6. **性能**：`qk_find_pricing` 每请求线性扫全表（LiteLLM 表 ~800 模型×4变体×5策略≈最坏 16k 次 endswith/in）；inlet 每次 JSON 全量读 ledger（ JC 缓存 mtime 失效，但大实例日积月累后 days/models 字典会变大）。retention 400 天默认偏长。修复方向：匹配结果 LRU 缓存；ledger 按期分文件。
7. **quota_period 只有 daily/monthly**；无按年/按周；无"配额重置时点"自定义（now 本地自然日/月切换）。
8. **配额粒度**：只有"当期总 credits"一种维度；无 per-model 独立额度（dartmouth 的 sponsored allowance 模式）、无请求数限制。`model_rpm/tpm` 类限速未做。
9. **网页**：overrides 无编辑 UI（只能手改 config.json）；用户/组多时分页缺失；用量表只看当月且截断 200 行；无 CSV 导出。
10. **价格匹配风险**：`contains` 策略可能误命中（如 `gpt-4o` 会 contains 命中 `gpt-4o-mini` 的 id 之外，反过来 `chatgpt-4o-latest` 类长 key 与短 key 竞争时取最长子串，但语义相近模型家族（4o-mini vs 4o）价格差 16 倍，误配代价高）。改进方向：exact/suffix 失败后先查"去 provider 前缀+去日期"的族匹配，contains 只作最后手段并在 UI 显示 how 供人工核对（现已显示）。
11. **安全**：页面与 API 仅靠 OWUI 会话 admin 校验；`/quota` 路径与 prune 一样无 CSP/额外防护，若实例暴露公网建议前置反代限流（OWUI 官方 hardening 建议）。
12. **credits 语义**：成本为 0 的模型（本地 ollama、无匹配）不消耗配额 → 配额形同虚设；可考虑给 unpriced 模型配 tokens 计数维度或强制 default_pricing。

## 9. 后续开发路线（按用户早前需求延伸）

- **P0 真机验证**：部署到测试实例，跑网页对话 + curl 直连两种流量，核对 ledger 与 analytics 差值；抓 stream() 实际 event 形状。
- **P1 健壮性**：§8.1/2/3——注入 stream_options、防重复挂载、补 per-connector usage 测试矩阵（OpenAI/Anthropic/Ollama/Bedrock/OpenRouter）。
- **P2 功能**：per-model 配额（config 加 `model_quotas`，解析时与总配额取 min 或独立账本）；请求数维度；quota 周期自定义；网页编辑 overrides；用量导出 CSV。
- **P3 性能/规模**：SQLite 后端（表：usage_day(user,model,day,cached,input,output,cost)）；匹配 LRU；大实例分页。
- **P4 对齐上游**：关注 open-webui #23558（CUNY per-group limit PR 是否合并——若合并 builtin 化，本插件可退化为计量/计费层）；#23926 系（API usage 追踪 builtin 化）。

## 10. 快速上手（给 coding agent）

1. 环境要求：Open WebUI ≥ 0.10.0（Event primitive + `__app__` 注入；Filter 部分兼容 0.6+）。
2. 复现测试：文档 §7 的用例可在无 OWUI 依赖下跑（stub pydantic 即可，见本次会话测试代码模式：`sys.modules['pydantic']` 注入 `_BM`/`Field`）。
3. 修改共享算法（§5.1/5.2/5.3/5.4/5.5/5.6 中标注"两文件各一份"的）必须同步双文件，diff 检查。
4. 数据目录：`$DATA_DIR/quota_keeper/`；调试时直接 cat 三个 JSON。
5. 日志：全部 `log.warning/log.info`，前缀 `quota-keeper`，`docker logs` 可过滤。

## 11. 参考资料

- prune 插件（页面模式范本）：github.com/Classic298/open-webui-plugins/tree/main/prune（event.py 7527 行，含 throttled 删除引擎 + /prune UI）
- Event Function 官方文档（`__app__` 路由注册、170+ 事件目录）：docs.openwebui.com/features/extensibility/plugin/functions/event
- Filter Function 官方文档（inlet/stream/outlet、`__metadata__`、usage 形态说明）：docs.openwebui.com/features/extensibility/plugin/functions/filter
- cpa-usage-keeper（上游定价同步思路）：github.com/Willxup/cpa-usage-keeper
- dartmouth/openwebui-token-tracking（credit 制配额参照）：github.com/dartmouth/openwebui-token-tracking
- Open WebUI 配额相关 issue：#23323、#23558(discussion)、#23926、#1430、#6692；PR #23556（stream_options 注入）
- LiteLLM 价格表：raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
- models.dev 价格（备选格式）：models.dev/api.json
- 管理页样式的社区插件目录：openwebui.com（搜 cost/usage/budget/rate limit）
