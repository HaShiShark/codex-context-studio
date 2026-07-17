# 产品原则

## 产品定位

Codex Context Studio 是 Codex 的本地上下文代理和维护工作台，不替代 Codex，也不承担主编码任务。

它拦截 Codex 发往上游模型的 Responses `input`，将其无损映射为可视、可编辑的 transcript。用户可以查看真实上下文结构，并让独立的上下文模型帮助整理主 Codex 的上下文，实现“AI 编辑 AI 的上下文”。

## 核心业务原则

### Transcript 是唯一业务真相

- 上下文地图展示 Codex `input` 的真实结构，不是聊天摘要或筛选后的历史。
- Provider item 即使未知、奇怪或暂时无法语义化展示，也必须保留。
- 用户和上下文模型的编辑直接作用于 transcript。
- 不建立 override、restore、edited transcript 或其他互相竞争的上下文状态。

具体请求、响应和 compact 规则以[代理核心设计](../architecture/proxy-core.md)为准。

### Cursor 只承担协议对齐

Cursor 是代理与 Codex 下一轮 input 计算差异的机器锚点。它需要持久化，但不是第二份 transcript，不向用户展示，也不允许上下文模型编辑。

### 请求处理只有一条主路径

代理每轮都从 transcript 重建上游 `input`。未编辑时，重建结果应自然等价于 Codex 原始输入；编辑后，上游看到编辑后的 transcript 和本轮新增内容。不得维护“未编辑时透传、编辑后重组”两套业务路径。

### Compact 只走可验证的本地路径

- 使用 Codex local compact，不以 remote compact 作为产品路径。
- 总结请求读取 compact 前的完整有效上下文。
- Compact 成功后立即模拟 Codex 确定会写入 replacement history 的规范内容，同时更新 transcript 和 cursor。
- 当前 user 保留；压缩前的 assistant/tool items 不进入 replacement history。
- 模拟之外的 developer、context 或 world-state 由下一轮真实 Codex input 决定，代理不猜测。

## 上下文工作台

上下文模型是独立的维护 agent，负责检查、压缩、替换、插入或删除 transcript 节点，不继续主 Codex 的编码任务。

工作方式：

- 每轮先获得轻量节点快照，而不是完整上下文全文。
- 需要细节时通过 `get_nodes` 读取指定节点。
- 主要使用节点级 `write_nodes` 一次完成一组编辑。
- 只有 assistant 节点内部确实需要细粒度操作时才使用 item 级编辑。
- 单轮工具循环可以维护内存 draft；循环结束后统一提交到唯一 transcript。
- 工具结果只在当前循环累计，下一轮只保留必要的用户/assistant 文本历史并重新生成快照。

工作台不能创建第二份持久 transcript，也不能修改 cursor。

## 上下文智能建议

自动建议是待审查的临时提案，不是后台自动改写：

- 对话空闲后，上下文模型可以生成保守的整理建议。
- 用户可以预览、应用或丢弃。
- 只有明确应用后才修改规范 transcript。
- 新主请求会使旧建议失效。
- 建议必须保护当前任务、约束、决策、未完成工作和未来需要的证据。
- 信心不足时保留原内容，不为了缩短而压缩。

完整规则见[上下文智能建议](../features/context-suggestions/design.md)。

## 前端原则

- 前端围绕当前 transcript 数据模型设计，不兼容已移除的旧状态 API。
- 上下文地图必须忠实展示消息、reasoning、工具调用、工具输出、additional tools 和未知 provider item。
- 为可读性生成的分组、摘要和结构化视图只是展示投影，绝不写回 provider item。
- Token 权重以原始 provider item 为依据，不以展示摘要为依据。

## 启动、配置和存储

- CLI 和 Desktop 通过明确的 `on`、`off`、`status`、`uninstall` 命令管理。
- `on` 临时写入所需 Codex provider、hook 和 notify 配置；`off` 恢复原配置。
- 输入 `ctx` 打开工作台时，hook 拦截是首选路径，代理拦截只作为 fallback。
- 会话按 session 独立落盘到用户级数据目录，文件结构保持少而清晰。
- 开发版和安装版的状态、日志、缓存与 shim 隔离，共享规范会话数据。

运行边界见[运行目录与开发隔离](../operations/runtime-layout.md)。

## 明确禁止恢复的旧逻辑

除非产品重新设计并明确引入，否则不得恢复：

- `override`、`override_transcript`、`edited_transcript`、`has_override`
- restore flow、`pending_restore`、恢复历史摘要
- remote compact 主路径
- 多份互相竞争的 transcript
- 为旧前端保留的兼容状态和旧 API

## 排障和开发纪律

- Bug 修改必须对应明确现象、日志、复现路径或代码证据。
- 启动慢要定位耗时点，不通过扩大 timeout 掩盖问题。
- 白屏优先检查前端错误、接口状态和 payload 形状。
- 请求卡住优先检查请求、SSE 流和状态闸门。
- 有成熟外围实现可参考时，可以复用启动、鉴权和展示经验，但不能带回旧状态模型。
- 新功能先确认属于产品原则、代理核心、协议还是具体功能层，再修改对应事实来源文档。

## 修改前检查清单

- Transcript 是否仍能无损重建 provider input？
- Cursor 是否仍只承担 diff 锚点？
- 请求是否仍只有一条主路径？
- Compact 是否仍使用 local compact 和规范模拟？
- 是否重新引入了 override、restore 或第二份 transcript？
- 前端展示投影是否仍不修改原始 provider item？
- 上下文模型是否仍通过轻量快照和工具操作唯一 transcript？
- Bug 是否有足够证据再动手？
