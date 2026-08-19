# Quota Keeper for Open WebUI

两个配套的 Function（安装到 Admin Panel -> Functions）：

| 文件 | 类型 | 职责 |
|------|------|------|
| `quota_keeper_filter.py` | Filter | 对每次 chat completion（网页 + 直接 API 调用）计量 cached/input/output token，按模型价格折算成本记账，并在配额超限时拦截请求 |
| `quota_keeper_admin.py` | Event | 在启动/启用时向 `__app__` 注册 API 与 `/quota` 网页（admin 控制台 / 普通用户自助卡片，按角色分流），后台定时从上游拉取价格表 |

## 安装

1. 打开 Open WebUI -> Admin Panel -> Functions -> "+"，分别粘贴两个文件内容并保存。
2. 两个 Function 都要启用（Enabled）。
3. Filter 需在要计量的模型上挂载：Admin Panel -> Settings -> Models -> 选中模型 -> Filters 勾选 Quota Keeper；或设为全局 Filter（Functions 页开启 Global 开关）。
4. Event 函数要求 Open WebUI >= 0.10.0（Event primitive 从该版本引入）。

启用后访问 `https://你的实例/quota`：admin 打开完整管理台，普通用户看到自己的配额卡片（数据来自 `/me`，只能看自己）。API 前缀（Valve `api_prefix`）与页面路径（Valve `route_prefix`）由 Event 函数控制，默认 `/api/v1/quota-keeper` 与 `/quota`；两者都要求登录会话。

## 配额解析（优先级）

```
个人 user quota  >  所属 groups 中最高者  >  default quota  >  不限制
```

- 用户页每行可单独覆盖（留空 = 继承）。
- 组配额取用户所属各组的最大值。

## 计费

- 成本按 token 细分：`cached / input / output / cache_write`，全部计入 cost（USD）。
- `credits_per_usd` 默认 1000，即 1000 credits = $1。
- 价格来源默认 LiteLLM 的 `model_prices_and_context_window.json`（每 token 价格自动换算为每 1M），也支持 models.dev 嵌套格式；可改 URL。**v0.3.4 起支持多数据源**：`pricing.url` 可填多个（每行一个 URL 或 JSON list），按顺序合并、同一模型 id 冲突时**第一个源生效**。例：默认 LiteLLM 表没有 moonshotai/zai 官方裸键（kimi-k3、glm-5.2 等只在 azure_ai/ 等第三方前缀下），可追加 `https://models.dev/api.json` 补官方价。
- 模糊匹配顺序：override → exact → 去日期后缀 → 路径后缀（`openai/gpt-4o` → `gpt-4o`）→ 尾段 → 包含子串；`.` 与 `-` 归一化，自动剥离 `-2024-08-06` / `-20241022` 等日期尾巴。管理页有 "Test match" 输入框实时验证。
- 无匹配且未配置 fallback pricing 时该模型计 0 成本，并在用量表打 unpriced 标记。

## 分时段

- `night_start_hour` ~ `night_end_hour`（可跨零点，默认 22~8）为夜间时段；`night_multiplier` 生效。
- 周六/周日 `weekend_multiplier` 生效；与夜间相乘。
- 实际生效配额 = 解析出的配额 × 当前时段倍数（例：夜间 ×0.5、周末 ×0.5，周六深夜即 ×0.25）。

## 分时计价（TOU，DeepSeek 式峰谷价格）

- 配置 `tou.enabled` 开启；`tou` 下分 `peak / offpeak / normal` 三档，每档一个 `rate` 倍数和若干时间窗 `windows`（`days` 用 JS 星期号 0=周日…6=周六，`start/end` 为 HH:MM，可跨零点）。
- 命中规则：`models[精确 id]` → `models[* 通配]`（键含 `*` 时按 fnmatch 匹配，如 `*deepseek*` 匹配任何包含它的模型 id；精确键优先，长 pattern 优先）→ `providers[模型 id 首段]` → `default_policy`。`holidays` 里的日期全天强制 offpeak（无 offpeak 档则取最低 rate 档）。
- `default_policy` 语义（针对未在 `providers`/`models` 里配置的模型）：
  - `"off"`：未配置 provider 的模型不参与峰谷计费（rate 恒为 1，账本标记 tier=off）。**建议保持 off**。
  - `"normal"`：未配置 provider 的模型按全局 `tiers` 窗口参与计费（仍会命中 peak/offpeak 窗口，只是无 provider 级覆盖）。
- 时间窗 `days` 为空列表（或缺省）表示每天。**normal 档不配时间窗 = 全天候兜底**：未命中 peak/offpeak 任何窗口的时间一律走 normal 的 rate；`rate=1` 即原价（不折不加）。
- 命中档位的 rate 乘以**整单价格**（cached+input+cache_write+output，DeepSeek 风格），与配额倍数（`schedule`）互不影响——前者改价格、后者改配额上限。
- 账本逐日/逐模型记录各档请求数 `tou` 与折扣金额 `cost_saved_usd`（= 按 normal 价算的成本 − 实际成本）；`/me` 与管理台显示当前用户所处档位。

## 价格覆盖（Pricing Overrides）

- 管理台「Pricing editor」列出的是**你实际使用的模型**（账本 ∪ Open WebUI 已配置模型），不是 2.5k 行的上游全表：每行显示解析出的价格与命中方式（exact/suffix/segment/contains/override/alias）、未计价标记，未匹配/错价的行排在最前。
- 每行两种修法：**直接填价**（input/cached/cache_write/output 每 1M），或**别名 + 系数**——`kimi-k3-256k → alias: kimi-k3 × 0.5` 即按 kimi-k3 价格打 5 折（别名可链式嵌套，防环，最多 8 跳；系数不写 = 1）。
- 覆盖值三种形态（`pricing.overrides`）：`{"prices": {...}}` 直接定价；`{"alias": "key", "multiplier": m}` 别名；裸价格 dict 为旧格式仍兼容。`null` = 清除该行覆盖（编辑器行尾 clear 按钮）。
- 覆盖优先级最高，且**永不被上游刷新覆盖**；来自 overrides 的行带「manual」徽标。用量表与 Test match 框显示实际命中的目标（`how` 字段）。

## 管理台（admin 视角 /quota）

- 6 张 KPI 卡（请求数 / tokens / 成本 USD / credits / 缓存率 / unpriced 请求），其中成本与 credits 带 7 天 sparkline；时间跨度选择 24h / 7d / 30d / 90d / 自定义（存 localStorage，24h 走小时粒度且为**滚动 24 小时**——从当前时刻往前推 24h，不是自然日 0 点起算）。用户排行表带 **WebUI 列**（按 `__metadata__.chat_id` 区分 web UI 与直接 API 请求，可与 Open WebUI 自带 analytics 对账；v0.3.1 前的历史数据无渠道标记，记为 0）。
- 堆叠趋势图（按模型分色，≤8 色 + Others）、用户排行表（搜索 + 可排序列 + 点击下钻该用户的 per-model 明细）、模型表（混合 $/M、unpriced 徽标、TOU 徽标、匹配目标按钮）。
- 最近动态（`/recent`，最近 200 条响应，手动刷新）、TOU 编辑器（档位 / 时间窗 / 提供方 / 模型 exact-or-glob / 节假日，节假日支持一键从 date.nager.at 拉取）、CSV 导出（前端生成）。
- **无任何自动刷新/轮询**：数据在打开页面或点手动刷新/改筛选时加载，聚合在服务端 `/stats` 按需完成，页面不下载原始 ledger。

## 个人自助（普通用户视角 /quota）

- 普通用户访问 `/quota` 看到自己的卡片：解析出的配额与来源（user/group/default/none）、当前时段倍数、生效配额、本期（日/月）已用 credits 与剩余、今日 token 构成与成本、7 天趋势。数据全部来自 `GET /me`，服务端强制只返回会话用户本人的数据。

## 数据与安全

- 所有数据存放在 `$DATA_DIR/quota_keeper/`（config.json / ledger.json / pricing_cache.json / recent.json），原子写入 + 文件锁，不改动 Open WebUI 数据库。
- API 挂载在 `/api/v1/quota-keeper/*`，页面在 `/quota`（两者都经 Event 函数 Valves 可改前缀），全部要求 admin 会话。
- Filter 默认 fail-open（计量/查询出错不阻断对话），admin 默认豁免，后台任务（标题/标签生成）不拦截但仍记账。
- 流式响应在终止 chunk 上读取 usage（OpenAI 风格 `prompt_tokens_details.cached_tokens` 与 Anthropic 风格 `cache_read_input_tokens` 均支持），非流式在 outlet 读取；按响应 id 去重防重复记账。

## 主要 Valves（Filter）

| Valve | 默认 | 说明 |
|-------|------|------|
| enable_enforcement | true | 关闭后只记账不拦截 |
| admins_bypass | true | admin 跳过检查 |
| allow_background_tasks | true | 后台任务放行（仍记账） |
| estimate_unreported_tokens | false | 上游不报 usage 时按字符/4 估算（非账单级） |
| block_message | … | 拦截提示文案，可用 {used} {quota} {source} {mult} 占位符 |

## 调试与排障

### 不打开页面，怎么确认插件在运行？

1. **看容器日志**（最直接）：

   ```bash
   docker logs open-webui 2>&1 | grep quota-keeper
   ```

   启动后应出现两行健康标志：

   - `quota-keeper API mounted at /api/v1/quota-keeper`
   - `quota-keeper admin page at /quota`

   保存/更新函数代码后重挂载会多一行 `quota-keeper refreshed N stale route(s)`；出现 `quota-keeper setup failed: ...` 说明挂载失败（带上该行报错排查）。**一条 quota-keeper 日志都没有** = Event 函数根本没运行：到 Admin Panel -> Functions 确认它是 Enabled，且 Open WebUI >= 0.10.0（Event 类型从该版本引入，旧版本的函数类型列表里没有 Event）。

2. **API 探针**（Admin Panel -> Settings -> Account 生成 API Key 后）：

   ```bash
   curl -sS -o /tmp/qk.out -w "%{http_code}\n" \
     -H "Authorization: Bearer <API_KEY>" \
     https://你的实例/api/v1/quota-keeper/me
   head -c 120 /tmp/qk.out
   ```

   | 结果 | 含义 |
   |------|------|
   | `200` + JSON（有 `"quota"` 等字段） | 插件正常 |
   | `401/403` + JSON | 路由活着，是凭证/权限问题（换 admin key 重试；`/me` 任何登录用户可用，其余端点要 admin） |
   | `200` 但内容以 `<!doctype html>` 开头 | **请求没到插件**。结合日志区分：日志里有 "API mounted" 却返回 HTML = 被前端 SPA 兜底路由遮蔽（v0.2.0 的 bug，见下表）；日志里没有 = Event 函数没运行（未启用 / OWUI 版本过低） |

3. **看数据文件**（验证 Filter 侧在记账）：

   ```bash
   docker exec open-webui ls -l /app/backend/data/quota_keeper/
   ```

   发一条测试消息后 `ledger.json` / `recent.json` 的 mtime 应更新；`pricing_cache.json` 的 `fetched_at_iso` 新鲜说明价格表后台刷新在跑。

4. **页面判别**：`curl -s https://你的实例/quota | grep -c quota-keeper` —— 大于 0 是本插件页面；为 0 则是 OWUI 的 SPA 外壳（浏览器里会被前端路由渲染成它的 404 页）。

### 404 / 页面打不开对照表

| 现象 | 根因 | 处理 |
|------|------|------|
| 浏览器显示 404，但 curl 看到 `200` + HTML | **SPA 遮蔽**（v0.2.0 的 bug）：OWUI 在 import 时就把前端兜底挂到 `/`（`spa-static-files`），插件启动时 append 的路由排在其后永不命中；`/quota` 返回 SPA 外壳，由前端路由显示 404 | admin 函数升级到 >= 0.2.2（路由改为插入 `spa-static-files` mount 之前，方案同 prune 插件）。保存代码即自动重挂载，无需重启；也可以重启容器 |
| 路由通了但页面/接口全部 `401`，detail 为 `auth failed: 'Request' object has no attribute 'role'` | v0.2.1 的 auth 兼容 bug：OWUI 的 `get_verified_user` 是 FastAPI 依赖函数（收 **user** 不是 request），按 request 调用导致全端点 401 | admin 函数升级到 >= 0.2.2（改为手动驱动 `bearer_security` -> `get_current_user` -> `get_verified_user` 真实链路，兼容新旧签名） |
| 保存新代码后页面/接口还是旧行为 | Starlette 不能原地替换 handler，旧路由残留在路由表 | >= 0.2.1 保存后自动按路径清陈旧路由再重挂载；更旧的版本只能重启容器 |
| 更新插件后页面仍空白/仍旧行为 | 浏览器缓存了旧版页面 HTML（旧 JS 是全有或全无加载，任一接口失败即空白） | **硬刷新**（Ctrl/Cmd+Shift+R）；>= 0.2.4 起页面带 `Cache-Control: no-store` 杜绝此问题 |
| 页面只有标题，DevTools Console 报 `Uncaught SyntaxError: Invalid regular expression: missing /` | v0.2.0-0.2.6 的 bug：页面内嵌 JS 的两处 `\n`（CSV 导出的正则与 join）被 Python 三引号串求值成真实换行，脚本整体 SyntaxError 不执行 | admin 函数升级到 >= 0.2.7（`QK_PAGE` 改 raw string，测试已钉死） |
| 页面只有标题、正文空白；网络面板里部分 `/api/v1/quota-keeper/*` 返回 9 字节纯文本 `403 Forbidden` | **前置网关拦截**（非插件问题）：实测该域名按 IP/限流策略拦截，突发 8 连请求约一半 403；页面加载是 4 路并发，任一被拦旧版会整页空白 | 调整网关的 IP 白名单/限流规则；或绕过域名直连容器/LAN 地址访问 `/quota`；>= 0.2.3 起单个接口失败只弹 toast 提示，其余区块照常渲染 |
| 管理台「用户」「组」列表为空，或组配额不生效；日志有 `users fetch failed: 'coroutine' object is not iterable` | OWUI >= 0.10 的 models 层全面 async 化（`get_users` 变协程且返回分页 dict、`get_groups` 必填 `filter`、`UserModel` 不再有 `group_ids`），v0.2.0-0.2.2 的同步调用全部落空 | **两个函数文件都**升级到 >= 0.2.3（filter 的组配额解析也在此修复） |
| 改了 `route_prefix` / `api_prefix` Valve | 保存 Valve 即触发重挂载，**新路径立即生效**；旧前缀路由找不到归属、残留在路由表直到重启（无害空壳） | 重启容器清掉旧前缀 |
| 重启容器后全部 404 且日志没有 quota-keeper 行 | Event 函数未启用，或 OWUI < 0.10 不支持 Event 类型 | 启用函数 / 升级 OWUI |
| `/quota` 返回 401/403 | 未登录或会话失效；页面要求 admin 会话，普通用户的自助卡片数据走 `/me` | 先登录 OWUI；API 用 `Authorization: Bearer` |
| 用 `:main` / `:main-slim` 滚动镜像，重启后行为变了 | 滚动 tag 每次拉新构建，行为可能变化 | `docker exec open-webui env \| grep WEBUI_BUILD_VERSION` 记录当前构建；生产建议锁定 release tag |

### 端到端验证计量

1. 用一个普通用户发一条短消息；
2. `recent.json` 里出现新条目（admin 也可调 `/api/v1/quota-keeper/recent` 查看）；
3. 该用户打开 `/quota` 卡片，今日用量 +1。
