# Quota Keeper UI 增强设计（favicon / KPI 渠道拆分与曲线补全 / 个人页增强）

日期：2026-08-20
状态：已与用户确认（favicon 风格、sparkline 数据口径、WebUI 呈现方式、个人页范围、价目表复用 `/models?mine=1`）

## 背景与目标

`/quota` 单页（`quota_keeper_admin.py` 的 `QK_PAGE`）当前问题：

1. 浏览器 tab 无图标（默认空白页图标）。
2. admin 顶部 6 张 KPI 卡中只有 Cost/Credits 有 sparkline——因为 `/stats` 的 `series` 每桶只带 cost（HANDOFF §8.16 已记为 P2 待办）。
3. KPI 层没有 webui/api 渠道汇总（账本已按 day/model 记 `channels:{webui,api}`，用户排行表也有 WebUI 列，唯独 KPI 没有）。
4. 非 admin 个人页只有 4 张小卡 + 7 天 cost 曲线，没有 model 分布、recent 动态和模型价目表。

所有改动**只落在 `quota_keeper_admin.py`**；共享 helper（`==== shared helpers: keep in sync ====` 区块）不触碰，**无 `quota_keeper_filter.py` 同步负担**（channels/tokens 数据 filter 早已在记，本次只是聚合与展示）。

## §1 Favicon

- `<head>` 增加一行 `<link rel="icon" href="data:image/svg+xml,..."/>`。
- SVG：32×32 viewBox，圆角矩形底 `#0f172a`，天蓝 `#38bdf8` 仪表盘造型（半圆弧线 + 指针 + 中心点），16px 下可辨；URL-encoded 单行内联。
- 不新增静态路由、不引入二进制文件（保持"自包含单文件"约束）。

## §2 admin KPI 区：渠道拆分 + 曲线补全

### 后端

`qk_stats`（账本路径）：

- KPI 增加 `channels: {"webui": n, "api": n}` 汇总。day 级（无 model 过滤）与 model 过滤级两条路径都已有现成 per-day/per-model `channels` 计数，直接累加进 KPI（与 `requests` 同口径：model 过滤时只算该模型贡献）。
- `series` 每桶结构从 `{bucket, by_model:{m:cost}}` 扩为：
  ```json
  {"bucket": b, "cost": {m: v}, "requests": n, "tokens": t}
  ```
  - `cost` 即原 `by_model`（改名；`/stats` 的消费者只有本 SPA，无兼容承诺）。
  - `requests`：day 粒度从 day 记录（或 model 过滤时的 `mm.requests`）求和；hour 粒度从 hours 桶求和。
  - `tokens`：cached+input+output 之和，同口径。
  - **model 过滤 + hour 粒度**：hours 桶跨模型聚合、无法按模型拆（HANDOFF 已记）。此时 hours 桶**跳过**（series 为空），`requests`/`tokens` 只由 day 路径累计进 KPI——与现状"hours 桶在 model 过滤时跳过"的行为一致，不产出误导性曲线。

`qk_stats_window`（24h 滚动窗口，数据源 recent.json）：逐项聚合出同样的 `channels` KPI 与每桶 `cost`/`requests`/`tokens`（hours 键沿用本地时区 `%Y-%m-%dT%H`）。

### 前端

- `renderKpis()`：
  - Requests 卡：主值不变，下方小字 `webui X · api Y`（取自 `kpi.channels`）。
  - Requests 卡、Tokens 卡：各加 `sparkSvg()`，数据取自 series 每桶 `requests`/`tokens`。
  - Cache rate 卡：不画线（比率曲线无意义），加小字 `cached X / Y in`。
  - Unpriced 卡：不画线（理想态恒 0），加小字 `of N req`（N = span 内总请求）。
  - 结果：6 张卡中 4 张有曲线（Cost/Credits/Requests/Tokens），2 张有合计小字。
- `renderTrend()`：读 `b.cost` 代替 `b.by_model`。
- 卡片宽度不足时 sparkline 高度/宽度沿用现有 `sparkSvg(v,140,34,color)` 惯例。

## §3 个人页（非 admin）增强

### 新增端点 `GET /me/usage?span=7d|30d`（`_require_user`）

从账本取**本人** days，返回：

```json
{
  "span": "7d",
  "channels": {"webui": n, "api": n},
  "trend": [{"day": "YYYY-MM-DD", "requests": n, "cost_usd": x}],   // 按日，长度 = span
  "models": [{"model": m, "requests": n, "tokens": {"cached","input","output"}, "cost_usd": x}]  // 按 cost 降序
}
```

`trend` 逻辑是现有 `/me` 7 天 trend 的直接推广（参数化天数）。

### `/recent` 改造

- 依赖从 `_require_admin` 降为 `_require_user`，新增 query `mine=1`。
- 非 admin：强制按 `user_id == me.id` 过滤，截断 50 条，剥离 `email` 字段。
- admin（不带 `mine`）：行为完全不变（200 条全量，含 email）。
- admin 带 `mine=1`：同样按本人过滤（调试用，无副作用）。

### 价目表：复用 `/models`，加 `?mine=1`（方案 A）

- `/models` 依赖从 `_require_admin` 降为 `_require_user`，新增 query `mine=1`。
- 带 `mine=1`：账本聚合从"所有用户"换成"当前用户"的 days → `used`（requests/cost_usd 为本人合计）；价格匹配段（`qk_find_pricing` + override spec 提取）**原封不动复用**。
- 不带参数：admin 行为不变。
- 普通用户不开放全量原始价格表（`/pricing` 维持 admin-only），避免暴露上游价格源结构。

### 前端 `renderPersonal()`

现有 4 卡 + 7 天曲线保留，下方新增三个区块（仅非 admin 可见；admin 走 `loadAdmin`，`renderPersonal` 不触发）：

1. **By model**（7d/30d 切换按钮，localStorage 记住选择）：精简表 model / req / tokens / cost / 占比 bar，数据源 `/me/usage`。
2. **Recent activity**：精简表 time / model / channel / tokens / cost / tier（**去掉 user/email 列**），数据源 `/recent?mine=1`，手动 Refresh 按钮（沿用零轮询约定）。
3. **Model prices**：表 model / input $/M / output $/M / cached $/M / 徽标（`manual` = 有 override；`unmatched` = price null）。`price: null` 显示 `0`，表上方小字备注：
   > 价格为 0 的模型暂未匹配到价目；价格表由后台定期刷新，使用后会按最新价格自动计价（历史未计价记录由 admin reprice 回填）。
   - 数据源 `/models?mine=1`。
   - 无独立缓存——每次刷新实时跑 `qk_find_pricing`，价格表更新后用户刷新页面即见新价。

三个区块 lazy 加载（进个人页时各拉一次），渲染失败各自 toast、互不影响（沿用 admin 的 tolerant loader 模式）。

## §4 错误处理与测试

### 错误处理

- 新端点沿用现有 `HTTPException`（401/403）auth 依赖模式。
- `recent.json` / 账本缺失或损坏：`qk_load_json` 返回默认空，各端点自然返回空列表，无需新分支。
- 前端：沿用 `toast` / `showFatal`；`/recent?mine=1` 加载失败只 toast 不清空已渲染内容。

### 测试（`tests/`，pytest + pydantic stub）

新增/更新用例：

1. `qk_stats`：KPI `channels` 汇总（day 路径 + model 过滤路径）；series 新结构 `{cost, requests, tokens}`（day 粒度与 hour 粒度）。
2. `qk_stats_window`：三序列（cost/requests/tokens）+ channels 汇总，窗口裁剪（ts < wstart 不计）。
3. `/recent?mine=1`：非 admin 只含本人条目、≤50 条、无 email；admin 无参数行为不变（200 条、含 email）。
4. `/me/usage`：span=7d/30d 的 trend 长度与日期序列；models 按 cost 降序；channels 合计等于 trend requests 之和。
5. `/models?mine=1`：非 admin 只看到本人用过的模型；`price:null` 的模型 `matched:false`；override 模型 `override` 非空。
6. HANDOFF §7 回归清单中断言旧 `by_model` 结构的用例同步改为 `cost`。

### 验证

- `python -m py_compile quota_keeper_admin.py quota_keeper_filter.py`
- `pytest`
- 手动清单：admin 6 张卡（4 曲线 + 2 小字）、Requests 卡 webui/api 小字、tab 图标、个人页 7d/30d 切换、非 admin recent 只含本人、价目表 unmatched 显示 0 + 备注。

## 明确不做（YAGNI）

- 独立第 7 张 WebUI KPI 卡（已否决，用小字拆分）。
- Cache rate / Unpriced 的 sparkline（不适合画线，用小字合计）。
- 全量原始价格表对普通用户开放。
- `quota_keeper_filter.py` 任何改动（数据早已在记）。
- 自动刷新/轮询（沿用页面零轮询约定）。
- `/me` 的 `tou.current_tier` 接线（HANDOFF §8.17，独立于本次）。
