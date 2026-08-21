# Quota Keeper for Open WebUI — 开发交接文档

> 版本：v0.2.0（2026-08）· 状态：核心逻辑已实现并通过单元测试，未经生产验证
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
│       ├── app.include_router(qk_router, prefix=valves.api_prefix)  # 默认 /api/v1/quota-keeper
│       ├── app.get(page_path)  → 内嵌单页 HTML（qk_build_page，约700行 JS），
│       │                        __QK_API_PREFIX__ 占位符在挂载时替换
│       └── self._pricing_task = create_task(_pricing_loop)  # 每600s检查，按 refresh_hours 刷新；
│                                                            # 强引用保存在实例上
└── $DATA_DIR/quota_keeper/          # DATA_DIR env，默认 /app/backend/data
    ├── config.json        # 全部配置（网页编辑产生）
    ├── ledger.json        # 记账账本（Filter 写，网页读）
    ├── pricing_cache.json # 价格表缓存（Event 写，Filter 读）
    ├── recent.json        # 最近 200 条响应环缓冲（Filter 在记账时顺带写，dashboard 读）
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
  },
  "tou": {                            // v0.2.0 分时计价（详见 §5.7）
    "enabled": false,
    "timezone": null,                 // null→schedule.timezone
    "tiers": {                        // 每档 {rate, windows[]}; normal 可无 windows
      "peak":    {"rate": 2.0, "windows": [{"days":[1,2,3,4,5],"start":"09:00","end":"12:00"},
                                           {"days":[1,2,3,4,5],"start":"14:00","end":"18:00"}]},
      "offpeak": {"rate": 0.5, "windows": [{"days":[0,1,2,3,4,5,6],"start":"00:30","end":"08:30"}]},
      "normal":  {"rate": 1.0}
    },
    "holidays": [],                   // "YYYY-MM-DD" → 全天强制 offpeak
    "default_policy": "off",          // off | normal；模型/提供方都未命中时
    "providers": {},                  // 首段: {"enabled":bool,"tiers":{name:{"rate":x}}}；裸模型名 → "_default"
    "models": {}                      // 精确模型 id: {"enabled":bool,"tiers":{...}}，优先级最高
  }
}
```

### ledger.json 结构

`users.<uid>.days.<YYYY-MM-DD> = {requests, cost_usd, tokens:{cached,input,output}, tou:{peak,offpeak,normal}, cost_saved_usd, hours:{<H>: {requests,cost_usd,tokens}}, models:{<model>:{requests,cost_usd,tokens,priced,unpriced_requests,tou,cost_saved_usd}}}`。
- 天 key 用**配置时区的本地日期**（qk_local_now）；`hours` 桶（按本地小时，v0.2.0 新增，dashboard `granularity=hour` 消费；仅天级、不按模型）。
- `priced` 由 `unpriced_requests`（每请求计数，v0.2.0 取代旧的 sticky "与"累积）派生：`mm["priced"] = mm.get("unpriced_requests",0) == 0`，语义向后兼容（出现过一次 unpriced 即 false）。`count_request=False` 的增量（Anthropic 部分 usage 合并的 topup）不递增任何请求计数、不递增 unpriced。
- `tou`：逐日/逐模型记录各档请求数（peak/offpeak/normal）；`cost_saved_usd` = 按 normal 价应计成本 − 实际成本（TOU rate 折扣额）。
- 旧账本缺这些字段时全部按 0/缺省读（向后兼容，无迁移）。
- v0.3.1 新增 `channels:{webui,api}`（天级 + 逐模型，仅请求计数）：`__metadata__.chat_id` 非空记为 webui，否则 api（直连 API 无 chat 上下文）；topup 不递增。recent.json 每条也带 `channel` 字段。dashboard 用户排行表 WebUI 列由此聚合，用于与 OWUI 自带 analytics 对账；24h 跨度为滚动窗口（`window_start_ts`，仅按 hours 桶计，边界天的 per-model 行因 hours 桶非 per-model 而跳过——见 qk_stats docstring）。

### recent.json 结构（v0.2.0，dashboard「最近动态」）

`{items: [{ts, user_id, name, email, model, tokens:{cached,input,output}, cost_usd, tou_tier, priced}]}`。
- 环形缓冲，**上限 200 条**，`del items[:-200]` 淘汰最旧；Filter 在记账的同一把锁内顺带追加（与 ledger 一致），O(1) 小文件写。
- `GET /recent` 返回 `items` **倒序**（最新在前）。文件不索引、不按请求留历史。

## 5. 核心算法

### 5.1 usage 归一化 qk_normalize_usage（filter + admin 各一份，逻辑必须一致；v0.5.1 起 admin 也持有——passthrough 摄入中间件复用）

兼容三种格式，输出 `{cached, input, output, cache_write}`：
- **OpenAI**: `prompt_tokens - prompt_tokens_details.cached_tokens` = input；cached = cached_tokens。
- **Anthropic**: `input_tokens`/`output_tokens` + `cache_read_input_tokens`/`cache_creation_input_tokens`（Anthropic 的 input_tokens **不含** cache_read，所以 cached 单独存，input 直接用）。
- 全零 → 返回 None（不记账）。

### 5.2 价格匹配 qk_find_pricing（filter + admin 各一份，逻辑必须一致）

输入统一 lower。变体集 `_qk_variants(m)`：`[原串, . → - 版本, 去日期后缀版, 去日期+点转杠版]`。
日期后缀正则：`r"[-:_.](20\d{2}[-_.]?\d{2}[-_.]?\d{2}|\d{6})$"`（剥 `-2024-08-06` / `-20241022`）。

匹配顺序（每个策略内部都对全部变体尝试；实现为 `resolve(mid, depth)` 递归，别名跳转入同一链）：
1. `override:<k>` — pricing.overrides 精确命中。覆盖值三种形态（v0.3.0）：
   - `{"prices": {...}}` 或裸价格 dict（旧格式）→ 直接使用
   - `{"alias": "<key>", "multiplier": m}` → 递归 resolve 目标 key（表内匹配与嵌套 override 都算），结果 per-1M 价 × `multiplier`（缺省 1）；**防环、最多 8 跳**，目标不可解析 → 无命中
   - `null` → 清除标记（跳过，不匹配）
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

### 5.4 ~~时段倍数 qk_time_multiplier~~（v0.4.0 已移除，恒 1）

**移除原因**：配额周期是天/月而倍数按小时切换——白天已用超闸的用户在 22:00 闸门降到一半时被立即断供（不是"夜间少给"，是"夜间断供"，8:00 又自动恢复）；且与 TOU 方向相反（同为 ×0.5，一个限量一个降价），极易混淆。决定删除：函数保留但恒返回 1.0（兼容 `/me`、`/stats`、inlet 的既有引用与响应字段），`DEFAULT_CONFIG.schedule` 只剩 `timezone`（账本日界 + TOU 兜底时区，v0.4.0 起默认 `Asia/Shanghai`）；validator 对遗留 night/weekend 键忽略不报错；inlet 的 `eff<=0` 死分支删除；UI 的 Time schedule 区块缩为单个 Timezone 输入。已验证：`test_schedule_multipliers_removed_always_one`。

### 5.5 价格拉取 qk_fetch_pricing（admin 独有）

自动识别两种格式：
- **LiteLLM** flat：检测任一 value 含 `input_cost_per_token`/`output_cost_per_token`；per-token × 1e6 → per-1M；映射 `cache_read_input_token_cost`→cached、`cache_creation_input_token_cost`→cache_write。
- **models.dev** nested：`{provider:{models:{name:{cost:{input,output,cache_read,cache_write}}}}}`，per-1M 直接用；同时写 `prov/name` 与裸 `name` 两个 key。
缓存 `_pricing_loop` 每 600s 唤醒，`refresh_hours` 内不重复拉；`POST /pricing/refresh {force:true}` 强刷。

### 5.6 并发与持久化

- 全部写操作走 `qk_lock()`（fcntl flock，跨 worker；Windows/异常退化为进程内 Lock——**多 worker + Windows 场景有丢写风险，见 §8**）。
- 原子写：tmp + fsync + os.replace。
- 读走 `_JsonCache`（mtime 失效缓存；v0.2.0 起键上文件 size，规避 mtime 粒度问题），记账读 ledger 用缓存，写时直读+加锁。

### 5.7 分时计价 qk_tou_rate / qk_tou_resolve_policy（filter + admin 各一份，逻辑必须一致）

- `qk_tou_resolve_policy(cfg, model_id)`：`models[精确 id]` → `models[* 通配]`（v0.3.0：键含 `*` 按 fnmatchcase 大小写不敏感匹配，**长 pattern 优先**，如 `*deepseek-v4-pro*` 优先于 `*deepseek*`）→ `providers[首段]`（裸模型名 → `_default`）→ `default_policy`（"off" 返回 None，不参与；"normal" 返回 `{}` 即 normal 档）。`enabled:false` 的策略返回 None。**先判 `tou.enabled`**，关闭时直接 None（rate 1.0）。
- `qk_tou_rate(cfg, model_id, now)` 返回 `(rate, tier)`，tier `"off"` = 不适用（rate 1.0）：
  1. 策略档级配置在全局 `tiers` 之上浅合并（`base.update(over)`，窗口与 rate 可部分覆盖）。
  2. 时区：`tou.timezone` → `schedule.timezone` → `$TZ` → UTC（`qk_tou_local_now`）；day/hour 均按此时区判定。
  3. 节假日：`holidays` 含当天 → 强制 offpeak；无 offpeak 档则取 peak/normal 中 rate 最低者。
  4. 窗口命中（`_hit`）：`days` 用 JS 星期号（`(now.weekday()+1) % 7`，0=周日）；`start/end` 解析 HH:MM 为分钟；`start<=end` 时 `s<=cur<e`，跨零点（s>e）时 `cur>=s or cur<e`；解析失败跳过该窗。
  5. 命中顺序 peak → offpeak → 兜底 normal。
- 应用点：`qk_record_usage` 内对**整单** `cost = base_cost * rate`（cached+input+cache_write+output 全部乘 rate，DeepSeek 风格）；与 `schedule` 配额倍数正交（一个改价格、一个改配额上限）。
- 已验证（tests/test_tou.py）：工作日 peak 窗 / offpeak 窗 / 窗外 normal / 周末无 peak / 未匹配模型 default off 不参与 / 模型覆盖优先于提供方 / 提供方档级 rate 覆盖 / 节假日强制 offpeak / 无 offpeak 档节假日取最低档 / 跨零点窗口 / 记账时 rate 应用与 `cost_saved_usd` 落账。

## 6. API 与页面

路由（prefix 由 Event Valves `api_prefix` 控制，默认 `/api/v1/quota-keeper`；**路由不再带 `/quota-keeper` 段**，v0.2.0 修复）：
```
GET  /config                        # 附加 _time_multiplier
POST /config                        # 网页保存：schema 校验(400 on violation) + 深合并进盘上 config（部分 POST 不再重置兄弟键）
GET  /users /groups /ledger         # 数据源（users/groups 来自 OWUI DB）
GET  /pricing                       # 默认只回摘要 {url, fetched_at_iso, models}；?full=1 回全表（编辑器）
GET  /recent                        # recent.json 倒序（最近 200 条，v0.2.0）
GET  /stats?from&to&user&model&granularity=hour|day   # 服务端聚合（v0.2.0，详见 qk_stats）
GET  /me                            # 自助（v0.2.0，仅 _require_user，强制本人数据）
POST /pricing/refresh               # {force}；asyncio.to_thread 包装，不阻塞事件循环
GET  /pricing/match?model=...       # {matched, how, price}
```
admin 路由全部经 `Depends(_require_admin)`（`open_webui.utils.auth.get_verified_user` + role==admin，失败 **raise HTTPException** 401/403，v0.2.0 修复）；`/me` 用 `_require_user`。挂载前经 `_mount_guard` 查 `__app__.routes` 防重复（v0.2.0 修复），页面路由另有 `path` 查重。

页面 `/quota`（Valves `route_prefix`）：单页内嵌 HTML（`QK_PAGE` 常量 + `__QK_API_PREFIX__` 占位符，挂载时替换），按角色分流——`/me` 返回 role==admin 渲染完整控制台，否则渲染个人卡片（配额 + 进度条 + 倍数提示 + 用量明细 + 7 天趋势）。admin 区块：General / 时段 / TOU 编辑器（档位 + 窗口 + 提供方 + 模型 + 节假日，节假日支持一键从 date.nager.at `GET /api/v3/PublicHolidays/{year}/{CC}` 拉取，无 key）/ 价格源（Test match + 覆盖编辑器：搜索、分页、行内编辑、manual 徽标）/ 组配额表 / 用户配额表（来源 tag + 服务端算的进度条）/ 6 张 KPI 卡（成本与 credits 带手写 SVG sparkline）/ 时间跨度 24h·7d·30d·90d·custom（localStorage 持久化，24h 内走小时粒度）/ 堆叠趋势图 / 用户排行表（搜索 + 排序 + 点击下钻 per-model）/ 模型表（混合 $/M、unpriced 徽标、匹配目标按钮）/ 最近动态（手动刷新）/ CSV 导出（前端生成）。**页面无任何自动刷新/轮询**。

Filter Valves：`enable_enforcement`(true)、`admins_bypass`(true)、`allow_background_tasks`(true, 后台任务放行但记账)、`estimate_unreported_tokens`(false, 按 chars/4 估算)、`block_message`(占位符 `{used}{quota}{source}{mult}`；模板渲染失败回退默认文案并 log.warning，v0.2.0 修复)。

### /me 响应结构

`{user:{id,name,email,role}, quota, quota_source(user|group|default|none), multiplier, effective_quota, used_credits(按 quota_period 折算，月=当月美元×credits_per_usd，日=今日), today:{cost_usd,requests}, trend:[7 天 {day,requests,cost_usd}], tou:{current_tier:null(预留, 见代码注释)}}`。

## 7. 已验证内容（Pyodide 沙箱 + pydantic stub，非真实 OWUI 环境）

- 两文件 ast 语法 OK。
- usage 归一化：OpenAI(cached 分离)/Anthropic(cache_read/creation)/全零→None。
- 匹配：上节 7 个用例全过；override 优先。
- 配额解析：user 覆盖 / 组取 max / default / 不限，四态正确。
- 时段倍数：四时间点正确（含跨零点、周末叠加）。
- 成本计算：`(input×2.5 + cached×1.25 + output×10)/1e6` 精确一致；cache_write 计入。
- 记账 round-trip：日聚合 + 模型聚合 + priced 标志 + retention 裁剪路径（TZ-aware 裁剪）。
- inlet 拦截：超限 raise QuotaBlocked（消息含 used/quota/source/mult）、admin bypass、无配额放行、夜间倍数收紧触发拦截而白天放行。
- stream()：SSE str 与 dict 事件、按响应 id 去重（重复同 id 不重记）。
- outlet()：非流式 body.usage、choices[0].usage 回退。

v0.2.0 追加验证（tests/ 全量 42 例，2026-08-18）：
- **auth**：`_require_admin`/`_require_user` 失败 raise HTTPException 401/403（不再返回 JSONResponse）。
- **前缀**：路由单前缀挂载（挂载路径恰为 `/api/v1/quota-keeper/*`）；页面 `__QK_API_PREFIX__` 占位符替换。
- **记账修复**：orphan 认领（stream 无 user → outlet 有 user+usage 恰好记一次；outlet 有 user 无 usage 也认领）、`block_message` 模板失败回退、`eff<=0` 有配额即拦截、bool 配额/倍数拒绝、`unpriced_requests` 逐请求计数可自愈、SSE 文本 `"usage"` 预筛、Anthropic 部分 usage 合并（`count_request=False` topup 不重复计数）。
- **config**：schema 校验（类型/范围违规 400）+ 深合并（部分 POST 保留兄弟键）；JS `isNaN` 显式检查（0 可保存）。
- **TOU**：见 §5.7 已验证列表。
- **stats/recent/me**：/stats 聚合（hour/day 粒度、筛选、缓存率、配额进度数）、/recent 环形缓冲与倒序、/me 只返回本人数据、模型筛选下 day 序列不重复计费、/pricing 摘要 vs `?full=1`。
- **运行时**：价格拉取走 `asyncio.to_thread` + `_pricing_task` 强引用；`_mount_guard` 防重复挂载。**v0.2.1 起** `_mount_guard` 改为把路由 splice 到 OWUI 的 `spa-static-files` 兜底 mount 之前、并按路径清陈旧路由（SPA 遮蔽与热更新残留均有回归测试，见 `tests/test_endpoints.py` 末尾）；挂载路径已在真实实例（main-slim 构建）验证。

**未验证 / 需在真实环境确认**（见下节）。

## 8. 已知限制与潜在 bug（后续开发重点）

按优先级排序（v0.2.0 更新：原 §8.2 已修复，原 §8.4 已修复，编号顺移）：

1. **真实 OWUI 集成部分未经测试**：已验证并修复——挂载顺序（§8.20）、auth 依赖签名（§8.21）、models async 化与 `__user__` 无 `group_ids`（§8.22）。**仍未验证**：`__metadata__.task` 后台任务标记、stream() 收到的 event 具体形状（字符串 SSE 还是 dict、终止 chunk 是否带 usage——官方文档明说"形状因 connector 而异"）、outlet 在 API 直连时是否被调用。
2. ~~**函数热重载重复注册**~~ — **v0.2.0 已修复**：挂载前 `_mount_guard` 查 `__app__.routes` 是否已含 prefix 再 include_router；页面路由保留 `path` 查重。**v0.2.1 进一步**：改为先按当前 `route_prefix`/`api_prefix` 路径清陈旧路由再 splice 重挂（热更新保存代码即换新手柄，无需重启），`_pricing_task` 改存 `__app__.state`、重挂载时 cancel 旧 task 再建（不再泄漏）。**残余**：改前缀 Valve 后**旧前缀**路由无法按路径找到、仍残留到重启（Valve 描述已注明）。
3. **usage 可能漏计**：若上游流式响应从不返回 usage 且未开模型 Usage capability → 不记账。`estimate_unreported_tokens` 是粗略兜底（chars/4）。改进方向：主动给请求注入 `stream_options:{include_usage:true}`（参照 open-webui PR #23556 对 Bedrock 的做法）。
4. ~~**orphan 认领路径窄**~~ — **v0.2.0 已修复**：outlet 有 user 时**无条件**认领同 id orphan（不再依赖 `estimate_unreported_tokens`）；无 user 的 stream 事件只占 orphan 槽不占 `_seen` 槽。**残余**：真实环境若 API 直连根本不进 filter 的 outlet，认领逻辑无效（预期内：直连计量主要靠 stream 的 usage + `__user__` 注入）。
5. **Windows/无 fcntl 多 worker**：锁退化为进程内，SQLite 式丢写风险（atomic replace 可缓解但 ledger 读改写非事务）。修复方向：改 SQLite 或加版本号重试。
6. **性能**：`qk_find_pricing` 每请求线性扫全表（LiteLLM 表 ~800 模型×4变体×5策略≈最坏 16k 次 endswith/in）；inlet 每次 JSON 全量读 ledger（JC 缓存 mtime+size 失效，但大实例日积月累后 days/models 字典会变大）。retention 400 天默认偏长。修复方向：匹配结果 LRU 缓存；ledger 按期分文件。
7. **quota_period 只有 daily/monthly**；无按年/按周；无"配额重置时点"自定义（now 本地自然日/月切换）。
8. **配额粒度**：只有"当期总 credits"一种维度；无 per-model 独立额度（dartmouth 的 sponsored allowance 模式）、无请求数限制。`model_rpm/tpm` 类限速未做。
9. **网页**：用户/组多时分页缺失；模型表/价格表分页为纯前端；用量明细只从 `/stats` 聚合出发。
10. **价格匹配风险**：`contains` 策略可能误命中（如 `gpt-4o` 会 contains 命中 `gpt-4o-mini` 的 id 之外，反过来 `chatgpt-4o-latest` 类长 key 与短 key 竞争时取最长子串，但语义相近模型家族（4o-mini vs 4o）价格差 16 倍，误配代价高）。改进方向：exact/suffix 失败后先查"去 provider 前缀+去日期"的族匹配，contains 只作最后手段并在 UI 显示 how 供人工核对（已显示）。
11. **安全**：页面与 API 仅靠 OWUI 会话 admin 校验；`/quota` 路径与 prune 一样无 CSP/额外防护，若实例暴露公网建议前置反代限流（OWUI 官方 hardening 建议）。
12. **credits 语义**：成本为 0 的模型（本地 ollama、无匹配）不消耗配额 → 配额形同虚设；可考虑给 unpriced 模型配 tokens 计数维度或强制 default_pricing。

v0.2.0 新增已知限制：

13. **topup 加法重复计费**：Anthropic 部分 usage 合并用 `count_request=False` 补差，tokens/成本是**加法**累积——对"累计式"用量上报的 connector（每次事件给的是累计值而非增量），会导致 tokens/cost 翻倍。当前实现面向增量式上报（OpenAI 终值 / Anthropic message_delta 增量），**未做增量 vs 累计检测**。（v0.2.0 收尾修复：`recent.json` **不再记录 topup 行**——`count_request=False` 是已记账响应的一次补充合并而非新响应，feed 只收整请求；`/recent` 的 200 条因此与 ledger 的 requests 计数一致。）
14. ~~**`_pricing_task` 生命周期**~~ — **v0.2.1 已修复**：task 改存 `__app__.state.quota_keeper_pricing_task`，每次（重）挂载先 cancel 旧 task 再按需重建；热更新/改 Valve 不再泄漏后台循环。
15. **`_mount_guard` 旧前缀残留**：改 `api_prefix` Valve 后重载，新前缀正常挂载，但旧前缀路由仍留在 `__app__.routes`（无功能影响，路由表脏；重启清除）。
16. **KPI sparkline 不全**：6 张 KPI 卡中仅成本与 credits 两张带 7 天 sparkline（手写 SVG），其余 4 张无（spec 原案 6 张全带，实现时收敛）。
17. **`/me` 的 `tou.current_tier` 为占位 null**：页面拿到的是配额/倍数/趋势；当前档位字段预留未接（页面无 per-user 模型列表无从展示，见代码注释）。
18. **TOU 时间窗 `days: []` = 每天**：`qk_tou_rate` 的 `_hit` 用 `w.get("days") or list(range(7))` 兜底，空列表（或缺省）等于全周命中；校验器 `qk_validate_config` 对空列表放行（合法值域 ints 0-6，允许空）。若未来想让"空列表 = 永不命中"，需同时改 `_hit` 与校验器与编辑器。
19. **TOU 时间窗 `start/end` 范围校验**：`qk_validate_config` 在 HH:MM 形状之外校验 0<=hh<=23、0<=mm<=59（错误信息形如 `tou.tiers.peak.windows[0].start must be HH:MM 00:00-23:59`）。

v0.2.1 修复记录（真实实例首验发现）：

20. ~~**SPA 兜底遮蔽插件路由（/quota 与全部 API 变 404）**~~ — **v0.2.1 已修复**。根因：OWUI 在 **import 时**就把 `SPAStaticFiles`（404 时回退 index.html）mount 到 `/`（`name="spa-static-files"`），v0.2.0 的 `_mount_guard` 在 `system.startup.completed` 事件里用 `include_router` **追加**路由，排在兜底之后永不命中——`GET /quota` 返回 SPA 外壳（HTTP 200，前端路由显示 404），API 返回 HTML，POST 返回 405。prune 插件之所以正常，是它的 `mount_routes` 在 `include_router` 后把新增路由 **splice 到 `spa-static-files` mount 之前**（其注释原话："routes appended during the startup event land after it and get shadowed"）。v0.2.1 采用同一方案，并把页面路由也并入同一个临时 router 一起 splice（顺带 `include_in_schema=False`，不再污染 /openapi.json）。同时补了 prune 的 **late-init 兜底**：任何事件到来时若本实例未挂载则挂载——热更新代码后下一个事件即完成清陈旧 + 重挂载，无需重启。诊断要点：HTTP 200 + HTML ≠ 路由活着，curl 看 body 或发 POST 看是否 405；`tests/test_endpoints.py` 末尾两个用例钉死该回归。
21. ~~**OWUI auth 签名漂移导致全端点 401**~~ — **v0.2.2 已修复**。`_require_user` 原来 `await get_verified_user(request)`；但 `get_verified_user` 是**依赖式**函数 `get_verified_user(user=Depends(get_current_user))`，收的是 user——把 request 传进去在 `user.role` 处 AttributeError，被兜底成 401，页面与 API 全灭（真机症状：日志正常、路由命中、全部 401）。v0.2.2 改为手动驱动真实链路：`bearer_security(request)` 取 token（`HTTPBearer(auto_error=False)`）→ `get_current_user(request, response, background_tasks, auth_token)`（按 TypeError 逐级减参兼容旧签名）→ `get_verified_user(user)`（sync/async 都兼容）；`_require_user/_require_admin` 声明 `response`/`background_tasks` 参数由 FastAPI 注入真对象，cookie 刷新不丢。教训：**OWUI 的 auth 工具是 FastAPI 依赖不是普通函数，接入前先读签名**；测试桩已改成真实依赖式签名（并断言 `get_verified_user` 收到的是 user），另增未认证 401 用例。
22. ~~**OWUI models 全 async 化：users/groups 表为空 + 组配额静默失效**~~ — **v0.2.3 已修复**。三处漂移：(a) `Users.get_users()` 变 async 且返回分页 dict（`{"users": [...], "total": n}`）；(b) `Groups.get_groups()` 变 async 且 `filter` 必填，`GroupResponse` 去掉 `user_ids` 只剩 `member_count`（成员 id 用 `get_group_user_ids_by_ids` 批量回填）；(c) 注入 filter 的 `__user__`（`UserModel.model_dump()`）**不含 group_ids**，而 `Groups.get_groups_by_member_id` 也变 async——`qk_user_group_ids` 的同步兜底在新版上 100% 走异常返回 []，**组配额静默不生效**（filter inlet、`/me`、`/stats` 三处全受影响）。v0.2.3：新增共享的 `qk_user_group_ids_async`（await 协程 + 5 分钟 TTL 缓存，双文件已同步），`qk_resolve_quota` 加可选 `group_ids` 参数；filter inlet 与 `/me` 先解析再传入；`/stats` 用 `get_groups_by_member_ids` 批量构建 uid→groups 映射传入 `qk_stats`。页面侧：init 的 4 路 `Promise.all` 全有或全无改为逐 endpoint 容错（一个失败只 toast 不空白整页）——该实例前置网关对突发请求限流返回 9 字节纯文本 `403 Forbidden`（实测突发约半数被拦），属用户侧网关配置，已在 README 排障表说明。教训：OWUI main 的 models 层正在全面 async + 分页化，**接内部 API 必须 await 并防签名/返回形状漂移**。
23. ~~**页面 JS 被 Python 转义吃掉：整页只剩标题**~~ — **v0.2.7 已修复**。`QK_PAGE` 是普通三引号串，里面 CSV 导出代码的 `/[",\n]/` 与 `lines.join('\n')` 两处 `\n` 被 **Python 求值成真实换行**：正则跨行、字符串跨行，浏览器直接 `Uncaught SyntaxError: Invalid regular expression: missing /`，脚本整体不执行——静态标题在、内容全无、无 toast（toast 也是脚本渲染的）。v0.2.0-0.2.6 的页面**从未在任何浏览器里真正能跑**。修复：`QK_PAGE = r"""...`（raw string；全页反斜杠序列仅 `\d`×4/`\n`×2，raw 化零副作用）。**测试方法学教训**：此前 jsdom 用例与人工回放都是**从源文件原始切片**取页面，绕过了 Python 转义求值，所以全绿；现已改为 AST 求值取值，并新增 `test_served_page_js_parses`（node --check 钉死整类回归）。同期还加了页面自诊断：`Cache-Control: no-store`（v0.2.4）、常驻 fatal 横幅 + window error/unhandledrejection 钩子（v0.2.5）、header booting… 标记（v0.2.6，区分"脚本没跑"与"脚本挂了"）。

v0.5.1 新增：

24. **passthrough 摄入中间件**（admin.py 新增，v0.5.1）：OWUI 把 `/api/v1/messages`（anthropic 直通）与 `/openai/responses` 当纯代理转发，**filter 的 inlet/stream/outlet 完全不触发**，直连 API 请求此前记不到账。v0.5.1 在 Event.event 挂载一个 `BaseHTTPMiddleware`（`qk_passthrough_middleware`），仅对 `QK_INGEST_PATHS` 里的 POST 生效：响应侧扫 usage——非流式 JSON（anthropic `usage` / responses `response.completed` / openai `choices[0].usage`）或流式 SSE（`message_start`+`message_delta` 合并、`response.completed`、顶层 `usage`）——用共享的 `qk_normalize_usage` 归一化后调 `qk_record_usage(channel="api")` 写入 ledger + recent。用户身份来自 `request.state.user`（OWUI AuthTokenMiddleware 在路由依赖前注入，`Depends(get_verified_user)` 同源），兜底 `X-OpenWebUI-User-*` 转发头（需开 `ENABLE_FORWARD_USER_INFO_HEADERS`）。**关键约束**：中间件必须在**首次请求前**挂（Starlette middleware 栈懒构建，Event on_startup/lifespan 阶段合法；旧版 starlette 无运行时守卫，新版有 `Cannot add middleware after an application has started`——OWUI 0.11 锁定版本栈未预构建，挂载合法）；用 `request.state._quota_keeper_ingested` mark 防热重载双实例重复记账。**流式 tee**：`StreamingResponse` 包一层，chunk 原样转发、流消费完后再扫描（不能在 tee 前扫描——chunks 还没收集）。**残余**：(a) 需要 OWUI 侧该路径确实经过 AuthTokenMiddleware（真机未验证 `request.state.user` 属性名，兜底 header 走转发）；(b) 流式 chunk 在消费后才记，若客户端中途断连则不记（与 filter 的流式出口行为一致）；(c) `/responses` 的 usage 在 `response.completed` 事件，若上游不返回该事件则漏计；(d) 响应体大（非流式）时全量缓冲——OWUI passthrough 非流式本来就整包返回，无额外风险。

v0.5.2 新增：

25. **phantom alias 行（prx.\*）**（filter v0.4.11 / admin v0.5.2 修复）：真机发现两类 `prx.*` 污染——(a) **stream-end outlet topup 落 alias 名**：`stream()` 按真实名记账后，stream-end 的 outlet 用 `metadata.model_name or body.model` 取名，metadata 缺 model_name 时 fallback 到 body.model（上游回显的 alias `prx.gemini-flash`）→ topup 落到 alias 行（req=0 但有 tokens，且 unpriced=1 永不消费）。修：`_seen`/`_seen_msgids` 改存 `(model, channel)` 对，topup 复用 stream() 首记的 model。(b) **outlet 整记用 alias**：metadata.model_name 缺失 + message_id 未匹配 `_seen_msgids`（stream 从未见过该 id）时 outlet 走 `_record`，body.model 是 alias → 整记 alias 行（req=1 unpriced=1）。修：outlet/stream 的 model fallback 剥离 `prx.` 前缀（cli-proxy-api prefix_id；硬编码该前缀，通用启发式太冒险）。**注意**：剥离后得到的是 `gemini-flash` 这类裸名，价格表里未必存在（真实模型是 `gemini-3.7-flash`）——治本靠 metadata.model_name（OWUI 注入真实名），剥离只是兜底；真机清理时用 alias→真实模型名映射表（`prx.gemini-flash→gemini-3.7-flash`、`prx.gemini-pro→gemini-3.1-pro-preview`、`prx.deepseek-flash→deepseek-v4-flash`）合并/改名 ledger 与 recent 的 prx 行，再跑 `qk_reprice_ledger` 补记成本并清 unpriced。真机教训：**metadata.model_name 并非总是注入**（15:01 案例就没有），alias 回显 + model_name 缺失 = phantom 行温床。



v0.5.3 新增：

26. **中间件在生产从未挂上（Claude Code 直连 0 记录根因）**（admin v0.5.3 修复）：生产 OWUI 0.11（fastapi 0.136.3 + **starlette 1.3.1**）在 **app startup 后已构建 `middleware_stack`**，Event 函数挂载时 `add_middleware` 抛 `Cannot add middleware after an application has started`（日志可见 `quota-keeper ingest middleware mount failed`）。本地测试环境是 starlette 0.36.3（懒构建、无守卫）→ 测试全绿但生产静默失效，**Claude Code 的 `/api/v1/messages` 直连 8347 次请求全没记到**。修复：不用 `add_middleware`，改 `__app__.user_middleware.append(Middleware(BaseHTTPMiddleware, dispatch=...))` + `__app__.middleware_stack = __app__.build_middleware_stack()`（starlette 1.3.1 下已单独验证可行，栈已构建也能重建）。**教训：测试环境 starlette 版本必须与生产一致**（本地 0.36.3 vs 生产 1.3.1 的差异正是这次坑的根源）。残余：`build_middleware_stack` 若在请求中途调用可能抛错（已 try/except，注册保留、下次访问重建）；中间件 append 到 user_middleware 末尾 = 最内层，AuthTokenMiddleware 外层先跑设 `state.token` → 路由 Depends(get_verified_user) 设 `state.user`（auth.py:360/411）→ 响应侧 `qk_ingest_parse_body_user` 读到 user。**不要**在中间件请求侧手动调 get_current_user（需要 Response()/BackgroundTasks() 实例化，在 1.3.1 下会 422 破坏路由）。


v0.5.4 新增：

27. **中间件破坏转发（body 被消费 + 流式 content-length）**（admin v0.5.4 修复）：v0.5.3 挂载成功后暴露两个转发 bug——(a) **请求体被 `await request.body()` 消费后没重新注入**：passthrough 路由读 body 拿到空体 → 转发模型 fallback 成默认值（实测 call `prx.gemini-flash` 变 `deepseek-pro`）+ 请求中断。修：读后 `request._body = body` 重新注入（已单独验证路由能读到原 model）。(b) **流式响应重建带 content-length**：`StreamingResponse(teed, headers=dict(response.headers))` 会把原始 content-length 带上，流式长度未知 → 客户端截断/中断。修：流式 tee 剥掉 `content-length`。**教训：中间件动 request/response 必须验证"转发完整性"**——读 body 必须回注，重建流必须剥长度头。


v0.5.5 新增：

28. **读 body 重新注入在生产仍破坏转发（499）**（admin v0.5.5 修复）：v0.5.4 的 `request._body = body` 重注入在**生产 fastapi 0.136.3** 无效——路由/转发侧用的是**新 Request 实例**（各自 body 缓存），中间件实例的回注到不了下游 → 转发仍是空体 → Claude Code 499。**根治：中间件完全不读请求体**（转发零干扰），model 改从**响应**提取（anthropic `message_start.message.model`、openai `data.model`、responses `response.model`）。`qk_ingest_scan_sse` 改返回 `(model, tok)`。教训：**读请求体的中间件在 passthrough 场景是自寻死路**——只读响应，模型名从响应回显拿。


v0.5.6 新增：

29. **model_aliases（alias → 真实模型名映射）**（filter v0.4.13 / admin v0.5.6）：passthrough 响应里的 `model` 是上游回显的 alias（`prx.gemini-flash`），价格表里没有 → unpriced；且和 webui 侧的真实名（`gemini-3.7-flash`）统计分裂。新增顶层配置 `model_aliases: {"prx.gemini-flash": "gemini-3.7-flash", ...}`，`qk_resolve_model_alias` 在**价格匹配前**（`qk_record_usage` 内）替换模型名，记账/计价/统计全用真实名。**与 `pricing.overrides` 的 `alias` 语义不同**：那是"价格引用"（glm-5.3 按 glm-5.2 价计，仍统计为 glm-5.3），这是"命名映射"（合并统计）；不配的模型原样通过。校验器对 `model_aliases` 做 dict[str,str] 检查。


v0.5.8 新增：

30. **流式 message_delta usage 提取修复**（admin v0.5.8）：真机 Claude Code 流式直连出现 `input=30362 output=0`——anthropic 流式 `message_delta` 的 usage 在**顶层**（`delta` 里只有 stop_reason），且是**累计值**（非增量）。原代码只查 `delta.usage`（拿不到）且按增量累加（double-count）。修：`message_delta` 先查顶层 `usage` 再查 `delta.usage`；scan 里 `message_start/message_delta/response.completed` 全部**覆盖** acc（累计语义）。真机抓包确认：非流式响应 model 已是真实名（`gemini-3.7-flash`），流式 message_start 也是真实名——**model_aliases 主要兜 filter 侧**，passthrough 响应自带真实名。


v0.5.9 新增：

31. **热重载后中间件仍是旧代码（output=0 持续）**（admin v0.5.9）：0.5.8 的 message_delta 顶层 usage 修复部署后 output 仍 0——原因：中间件挂载用 `app.state.quota_keeper_ingest_mw` 标志防重，热重载时标志为 True → **跳过重挂**，进程里跑的还是旧中间件（无顶层 usage 分支）。修：**每次 mount 都重挂**——从 `user_middleware` 移除旧实例（按 dispatch 是当前模块函数对象）再 append 新实例 + 重建栈；`QK_INGEST_MARK`（request.state）兜底防双记。**注意**：热重载后模块函数对象变了，旧实例靠 `is` 删不掉（不同对象），但新 append 的会先跑？不——中间件顺序是 append 顺序，旧的在内层先跑仍 output=0。**根治仍需重启 OWUI**（进程内旧模块无法替换）；v2 逻辑保证**重启后**热重载不再踩坑。**真机经验：改中间件代码后必须重启 OWUI，热重载不会替换已挂的中间件函数。**


v0.5.12 新增：

32. **24h KPI 卡 200（recent 环形缓冲上限）**（filter v0.4.14 / admin v0.5.12 修复）：24h 视图的 KPI 从 recent.json 统计，而 recent 是 200 条环形缓冲 → 24h 内请求超 200 时 KPI 卡在 200（`window_partial` 标记但数字假）。修：`qk_stats_window` 的 **KPI 改从 ledger 的 hours 桶聚合**（精确、无截断；hours 桶新增 `channels` 字段，filter + admin 记账时同步写入）。recent 仍驱动 per-user/per-model 明细表和序列（截断仍以 `window_partial` 标记）。**模型过滤时** hours 桶不可用（跨模型）→ 回退 recent 累加。unpriced 从 day 级 models 按"窗口内有 hours 桶的 day"聚合。**注意**：hours 桶是整小时聚合，滚动窗口的当前部分小时会被整算（近似，可接受）。

## 9. 后续开发路线（按用户早前需求延伸）

- **P0 真机验证**：部署到测试实例，跑网页对话 + curl 直连两种流量，核对 ledger 与 analytics 差值；抓 stream() 实际 event 形状。
- **P1 健壮性**：§8.1/3/13——注入 stream_options、补 per-connector usage 测试矩阵（OpenAI/Anthropic/Ollama/Bedrock/OpenRouter）、增量 vs 累计式用量检测；§8.14/15 `_pricing_task` cancel 与 `_mount_guard` 旧前缀清理。
- **P2 功能**：per-model 配额（config 加 `model_quotas`，解析时与总配额取 min 或独立账本）；请求数维度；quota 周期自定义；补齐 6 张 KPI 卡 sparkline（§8.16）；`/me` 当前 TOU 档位（§8.17）。
- **P3 性能/规模**：SQLite 后端（表：usage_day(user,model,day,cached,input,output,cost)）；匹配 LRU；大实例分页。
- **P4 对齐上游**：关注 open-webui #23558（CUNY per-group limit PR 是否合并——若合并 builtin 化，本插件可退化为计量/计费层）；#23926 系（API usage 追踪 builtin 化）。

## 10. 快速上手（给 coding agent）

1. 环境要求：Open WebUI ≥ 0.10.0（Event primitive + `__app__` 注入；Filter 部分兼容 0.6+）。
2. 复现测试：仓库 `tests/` 的 73 个用例可无 OWUI 依赖跑（`python3 -m pytest tests/ -v`；conftest 先导 fastapi 再用 pydantic stub 加载双模块）。§5.2 的 7 个匹配用例与 §5.7 TOU 用例都在其中；SPA 遮蔽/热更新重挂载回归用例在 `test_endpoints.py` 末尾；页面 JS 以 **AST 求值**（而非原始切片）取 QK_PAGE 后过 `node --check` 与 jsdom（v0.2.7 教训：Python 会吃掉 JS 里的 `\n` 转义）。
3. 修改共享算法（§5.1/5.2/5.3/5.4/5.5/5.6/5.7 中标注"两文件各一份"的）必须同步双文件，diff 检查。
4. 数据目录：`$DATA_DIR/quota_keeper/`；调试时直接 cat 四个 JSON（config/ledger/pricing_cache/recent）。
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
