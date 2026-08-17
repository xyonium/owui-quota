# Quota Keeper for Open WebUI

两个配套的 Function（安装到 Admin Panel -> Functions）：

| 文件 | 类型 | 职责 |
|------|------|------|
| `quota_keeper_filter.py` | Filter | 对每次 chat completion（网页 + 直接 API 调用）计量 cached/input/output token，按模型价格折算成本记账，并在配额超限时拦截请求 |
| `quota_keeper_admin.py` | Event | 在启动/启用时向 `__app__` 注册 API 与 `/quota` 管理网页，后台定时从上游拉取价格表 |

## 安装

1. 打开 Open WebUI -> Admin Panel -> Functions -> "+"，分别粘贴两个文件内容并保存。
2. 两个 Function 都要启用（Enabled）。
3. Filter 需在要计量的模型上挂载：Admin Panel -> Settings -> Models -> 选中模型 -> Filters 勾选 Quota Keeper；或设为全局 Filter（Functions 页开启 Global 开关）。
4. Event 函数要求 Open WebUI >= 0.10.0（Event primitive 从该版本引入）。

启用后访问 `https://你的实例/quota` 打开管理页（仅 admin 可见，会话与实例共享）。

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

## 数据与安全

- 所有数据存放在 `$DATA_DIR/quota_keeper/`（config.json / ledger.json / pricing_cache.json），原子写入 + 文件锁，不改动 Open WebUI 数据库。
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
