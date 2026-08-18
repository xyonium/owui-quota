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
- 价格来源默认 LiteLLM 的 `model_prices_and_context_window.json`（每 token 价格自动换算为每 1M），也支持 models.dev 嵌套格式；可改 URL。
- 模糊匹配顺序：override → exact → 去日期后缀 → 路径后缀（`openai/gpt-4o` → `gpt-4o`）→ 尾段 → 包含子串；`.` 与 `-` 归一化，自动剥离 `-2024-08-06` / `-20241022` 等日期尾巴。管理页有 "Test match" 输入框实时验证。
- 无匹配且未配置 fallback pricing 时该模型计 0 成本，并在用量表打 unpriced 标记。

## 分时段

- `night_start_hour` ~ `night_end_hour`（可跨零点，默认 22~8）为夜间时段；`night_multiplier` 生效。
- 周六/周日 `weekend_multiplier` 生效；与夜间相乘。
- 实际生效配额 = 解析出的配额 × 当前时段倍数（例：夜间 ×0.5、周末 ×0.5，周六深夜即 ×0.25）。

## 分时计价（TOU，DeepSeek 式峰谷价格）

- 配置 `tou.enabled` 开启；`tou` 下分 `peak / offpeak / normal` 三档，每档一个 `rate` 倍数和若干时间窗 `windows`（`days` 用 JS 星期号 0=周日…6=周六，`start/end` 为 HH:MM，可跨零点）。
- 命中规则：`models[精确模型 id]` → `providers[模型 id 首段]` → `default_policy`。`holidays` 里的日期全天强制 offpeak（无 offpeak 档则取最低 rate 档）。
- `default_policy` 语义（针对未在 `providers`/`models` 里配置的模型）：
  - `"off"`：未配置 provider 的模型不参与峰谷计费（rate 恒为 1，账本标记 tier=off）。**建议保持 off**。
  - `"normal"`：未配置 provider 的模型按全局 `tiers` 窗口参与计费（仍会命中 peak/offpeak 窗口，只是无 provider 级覆盖）。
- 时间窗 `days` 为空列表（或缺省）表示每天。
- 命中档位的 rate 乘以**整单价格**（cached+input+cache_write+output，DeepSeek 风格），与配额倍数（`schedule`）互不影响——前者改价格、后者改配额上限。
- 账本逐日/逐模型记录各档请求数 `tou` 与折扣金额 `cost_saved_usd`（= 按 normal 价算的成本 − 实际成本）；`/me` 与管理台显示当前用户所处档位。

## 价格覆盖（Pricing Overrides）

- 管理台「价格」区是缓存价格表的可搜索、分页表格：每行显示匹配目标 key、per-1M 四价（input/cached/cache_write/output），支持行内编辑保存到 `pricing.overrides`。
- 覆盖优先级最高，且**永不被上游刷新覆盖**；来自 overrides 的行带「manual」徽标。用量表与 Test match 框显示实际命中的目标（`how` 字段）。

## 管理台（admin 视角 /quota）

- 6 张 KPI 卡（请求数 / tokens / 成本 USD / credits / 缓存率 / unpriced 请求），其中成本与 credits 带 7 天 sparkline；时间跨度选择 24h / 7d / 30d / 90d / 自定义（存 localStorage，24h 内走小时粒度）。
- 堆叠趋势图（按模型分色，≤8 色 + Others）、用户排行表（搜索 + 可排序列 + 点击下钻该用户的 per-model 明细）、模型表（混合 $/M、unpriced 徽标、TOU 徽标、匹配目标按钮）。
- 最近动态（`/recent`，最近 200 条响应，手动刷新）、TOU 编辑器（档位 / 时间窗 / 提供方 / 模型 / 节假日，节假日支持一键从 date.nager.at 拉取）、CSV 导出（前端生成）。
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
