# Responses Lite 代理适配实施记录

## 1. 目标

本次调整让代理同时正确处理标准 Responses 和 Codex Responses Lite，并保持项目原有核心约束：

- transcript 继续是 provider input 的无损结构化投影。
- cursor 继续是下一轮 diff 的机器锚点。
- 未知 provider item、顶层字段和工具定义不丢失、不擅自改写。
- compact 模拟只提前构造 Codex 确定会出现的 canonical items。
- 模拟不准确时，原有最长公共前缀和保守 pop 仍是最终恢复机制。

## 2. Codex 源码结论

核对源码环境：

- 本地源码：`D:\opensource\codex`
- 分支：`main`，与 `origin/main` 同步
- 核对提交：`2f7d89b1419bf7064346855b0acde23514b1ebc5`
- 全局 CLI：`codex-cli 0.144.2`

`codex-rs/core/src/client.rs` 当前按模型配置 `use_responses_lite` 构造请求：

```text
Lite:
input[0] = AdditionalTools(role=developer, tools=...)
input[1] = Message(role=developer, content=base_instructions)
top-level instructions = ""
top-level tools = None

Standard:
top-level instructions = base_instructions
top-level tools = tools
input = formatted history
```

当前模型目录中 `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna` 的 `use_responses_lite=true`。这说明“5.6 使用 Lite”是当前模型配置事实，而 `additional_tools` 首项才是代理收到的可验证协议事实。

## 3. 协议识别决定

实现没有写死 `model.startsWith("gpt-5.6")`，而是使用严格请求形状：

```text
input 是非空数组
且 input[0] 是对象
且 input[0].type == "additional_tools"
```

原因：

- 同一模型名可能由 Codex 配置或服务端能力切换协议。
- Codex 源码计划逐步扩大 Lite 使用范围，模型白名单会很快过时。
- 请求形状直接决定字段在哪，代理按形状处理更接近无损透传原则。

该识别故意足够窄。中段偶然出现一个 `additional_tools` 不会被当成 Lite；只有规范首项触发。

## 4. 基础提示词识别与覆盖

### 4.1 标准模式

- 读取已经存在的顶层 `instructions`。
- 用户设置了自定义系统提示词时，只替换这个字段。
- 原请求没有 `instructions` 时不创建。

### 4.2 Lite 模式

- `input[0]` 必须是 `additional_tools`。
- 只把紧邻的 `input[1]`、`type=message`、`role=developer` 识别为 base instructions。
- 设置覆盖只替换这一个 message 的文本。
- 后续 developer 节点可能是 permissions、skills、协作模式或运行环境，全部保持原样。
- Lite 请求不创建顶层 `instructions`。

这个位置规则来自 Codex 当前构造代码，不通过文本内容猜测“哪条看起来像系统提示词”，因此不会误改多个 developer 中的其他节点。

### 4.3 首次提示词采集

首次采集逻辑也使用同一定位规则。若某次请求没有标准 `instructions` 或 Lite base developer，不会永久结束扫描；后续出现支持的请求位置时仍能采集默认提示词。

## 5. 原始请求与 effective request

系统提示词覆盖在进入 proxy core 前完成：

```text
raw body
-> 在原协议位置替换 base instructions
-> effective body
-> cursor diff / transcript 吸收
-> transcript 重组 forwarded body
```

存储边界：

- request log 的 `body` 保留 Codex 发来的原始请求，便于审计。
- request log 的 `forwarded_body` 记录实际转发内容。
- transcript 和 cursor 吸收 effective input，因为这是模型实际看到、代理下一轮会再次构造的结构。

若 Lite cursor 记录原始基础 developer、transcript 却记录覆盖后的 developer，下一轮会产生代理自己制造的假 diff。让两者都使用 effective input 可以从根源避免这个问题。

## 6. 顶层字段与工具边界

代理没有增加“工具兼容转换层”：

- 标准模式已有顶层 `tools` 时原样保留。
- Lite 的工具定义只保留在 `additional_tools` provider item 中。
- Codex 没有发送顶层 `tools` 或 `instructions` 时，代理不会补字段。
- `previous_response_id`、`tool_choice`、reasoning 配置及未知顶层字段继续由整包深拷贝透传。

因此本次不是两套后端存储逻辑，只是在进入同一条 transcript/cursor 主路径前识别基础提示词所在位置。

## 7. Lite compact 模拟

标准模式保持原结构：

```text
retained user messages
compact summary (role=user)
```

Lite 模式在前面保留 compact 请求中已经存在的 canonical prefix：

```text
additional_tools
连续的前置 developer/system messages
retained user messages
compact summary (role=user)
```

边界决定：

- 前缀来自 compact 请求的 effective cursor，逐项深拷贝，不重新生成。
- base developer 因而是用户设置覆盖后的实际版本。
- 只收集索引 0 后连续出现的 developer/system。
- 对话中段的 developer/context 不进入模拟，避免把动态历史错误提前到前缀。
- summary 仍为普通 user role，不改 compaction 的 role 约定。
- retained user 的 token 预算和“不要重复保留旧 summary”规则不变。

## 8. Pop 最终兜底

本次没有修改 `compute_diff` 或 `TranscriptDeltaApplier.pop`：

1. cursor 与下一轮 effective input 计算最长公共前缀。
2. cursor 旧后缀成为 pop，真实请求新后缀成为 append。
3. pop 从 transcript 尾部逐项比对 fingerprint，匹配才删除。
4. 尾部被用户编辑时立即停止并标记 `tail_conflict`。
5. 若 Lite 的 `additional_tools` 在索引 0 就变化，公共前缀为 0；当 transcript 未被编辑时，允许完整 pop 后按真实请求重建。

测试同时覆盖了两条关键路径：

- Lite 前缀完全稳定时，下一轮不产生 transcript 变化。
- `additional_tools` 从索引 0 改变时，旧模拟内容完整 pop，再无冲突重建。

这意味着 Lite 前缀模拟只是提高匹配率，不是新的正确性依赖；原有 pop 仍承担最强兜底。

## 9. 前端 additional_tools 展示

前端只增加 display projection：

- 显示 `additional_tools` 的工具总数。
- 显示顶层工具名称与类型。
- namespace 工具显示其子工具名称。
- 不展开大段 description、schema 或原始 JSON。
- 保留名称中的下划线，例如 `request_user_input`。
- 展开文本保留换行。

原始 `providerItems` 没有改变，token 统计和 transcript 回写仍使用原 provider item。展示优化不会进入后端请求、持久化或编解码。

设置页在“Codex 系统提示词”下增加说明：

```text
GPT-5.6 的系统提示词以 developer 节点传递。
```

## 10. 验证范围

新增或扩展的自动测试覆盖：

- 标准和 Lite 基础提示词采集。
- Lite 只替换 base developer，其他 developer 不变。
- 缺失字段时不合成顶层 `instructions` / `tools`。
- raw request 日志与 effective transcript/cursor 的边界。
- Lite compact canonical prefix。
- 稳定前缀零变化和索引 0 变化的完整 pop。
- `additional_tools` 可读展示、下划线、namespace 和 provider item 保留。

运行时注意：当前正在运行的代理不会自动加载 Python 代码变化。完成测试后需要在合适时机重启代理，再用真实 GPT-5.6 新会话验证一次；本次实施过程中不主动中断正在运行的 Codex 会话。
