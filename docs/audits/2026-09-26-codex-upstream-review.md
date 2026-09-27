# Codex 上游变更兼容性审查

> 本文保留修复前审查证据。已落地修改、验证结果和剩余边界见[原生工作流修复记录](2026-09-26-native-workflow-fixes.md)。

> 后续补充：本报告最初聚焦上游时间区间内的协议变更，未完整覆盖项目原有接入缺陷。按“不能影响原生工作流”的新目标补查后，确认图片接口缺失、provider 能力改变、流式解析和响应状态等额外问题。当前问题总表以 [原生工作流问题清单](2026-09-26-native-workflow-issues.md) 为准。本报告的“HTTP 和本地压缩仍可用”不应理解为接入已经等价于原生。另：上游 `include_internal_metadata` 是 `serde(skip)` 的运行时字段，不能直接作为普通 TOML 配置开关开启。

审查日期：2026-09-26。范围：源码审查、现有代理测试、隔离的合成输入复现。没有修改应用代码、Codex 用户配置，也没有调用真实模型或中断现有会话。

## 比较基准

| 对象 | 已核实的提交 | 北京时间 |
| --- | --- | --- |
| 本项目 `origin/main`，本地 HEAD 与其一致 | `a53ee10846c96505ba8ed1275e6de7cd02a4496f`，Adapt proxy state to current Codex rollouts | 2026-08-15 13:25:08 |
| 上述时间之前 Codex main 的最后一个主线提交 | `53f3fa749659498fa24c81da8fde5440fa7bba7f`，Route permission requests through shared Guardian approvals (#38701) | 2026-08-15 13:09:19 |
| 本次抓取的 Codex `origin/main` / 本地 HEAD | `e72da2b53805894878023d01949a25a082e0a5cb`，Stabilize skill catalogs across executor availability changes (#48353) | 2026-09-26 12:19:46 |

Codex 仓库已在 `D:\opensource\codex` 原有干净 checkout 上完成 fetch 和 fast-forward。对比区间 `53f3fa7496..e72da2b538` 包含 2,134 个提交，主线计数也是 2,134。以 committer 时间确定节点，不将旧 checkout 的 8 月 15 日 11:00 提交误当作精确基准。

项目远端：[HaShiShark/codex-context-studio](https://github.com/HaShiShark/codex-context-studio/commit/a53ee10846c96505ba8ed1275e6de7cd02a4496f)。上游比较：[openai/codex](https://github.com/openai/codex/compare/53f3fa749659498fa24c81da8fde5440fa7bba7f...e72da2b53805894878023d01949a25a082e0a5cb)。

审查聚焦本项目实际耦合的协议、消息转换、压缩、请求生命周期、provider 配置和模型目录；没有逐行审计全部 2,134 个提交，也不把 Codex 的 TUI、执行器、权限系统内部重构当成本项目必须同步的代码。

## 结论与优先级

| 优先级 | 更新项 | 证据强度 | 产品影响 |
| --- | --- | --- | --- |
| P1 | 支持没有前置调用的独立工具结果 | 已复现校验失败和字段丢失 | 合法新版上下文无法提交编辑；事件来源可能丢失 |
| P1 | 取消请求的状态收尾，并接入 Interrupt 生命周期 | 已复现取消流未完成状态收尾；真实客户端并发尚未联调 | 主任务运行状态、自动整理和新请求之间存在状态残留风险 |
| P1 | 对齐本地压缩后的保留消息规则 | 已复现多段文本截断和媒体保留偏差 | 压缩后模拟历史与 Codex 的下一轮输入不一致 |
| P2 | 支持图片 `file_id` 的投影和展示 | 已复现转换函数丢失文件引用；普通原始 input 仍无损 | 特定响应转换路径会产生缺失图片引用的内容 |
| P2 | 更新模型目录获取和兜底列表 | 两端源码及目录已核实 | 新模型不在项目静态选择项中；兜底目录仍包含上游已移出的模型 |
| P3 | 展示配置变更、内容来源、工具调用元数据 | 新结构能够透传；展示尚无对应语义 | 可提升上下文可读性，不是全部请求失效 |

## 1. 独立工具结果：确定需要更新

上游 [763787d061 / #39782](https://github.com/openai/codex/commit/763787d061de3078c556ac32e3cc5454745dc8b9) 在 8 月 20 日允许 `function_call_output` 没有 `call_id`，以 `name` / `namespace` 标识事件来源。它可以通过 `thread/inject_items` 进入历史，不要求前面存在工具调用。上游自己的 JSON round-trip 测试在 `codex-rs/protocol/src/models.rs:3245`。

项目不匹配的地方：

- `backend/web_context.py:1807`：上下文编辑校验要求每个工具结果都找到对应调用；编辑提交路径在同文件约 2086 行调用它。
- `backend/web_context.py:1026`：缺少 `call_id` 时用消息自己的 `id` 代替。独立结果即使具有消息 ID，也会被误判为“找不到对应调用”。
- `backend/codex_input_cursor.py:32`：结果字段白名单没有 `name` / `namespace`。
- `backend/transcript_codec.py:200`：无调用 ID 的结果回退挂到最近的 assistant 节点，尚未表达独立事件语义。
- `react_app/src/types.ts:41`：`function_call_output.call_id` 仍声明为必填。

隔离复现输入：

```json
{"type":"function_call_output","name":"external_event","namespace":"events","output":"event data"}
```

调用当前校验得到 `tool output item #1 is missing call_id`；添加 `id: "fco_external"` 后得到 `has no matching call item`。当前响应投影结果变成：

```json
{"type":"function_call_output","output":"event data"}
```

建议统一消息协议定义中的“成对工具结果”和“独立命名结果”，让校验、分组、前端类型和响应投影共用该语义。保留真正成对调用的完整性检查，不能简单放开所有孤立结果。验收应包含上游相同形状的独立事件经过“接收 → 编辑保存 → 下一轮转发”的完整流程。

## 2. 中断与抢占：优先补全生命周期

上游 8 月 25 日新增 [Interrupt hook / #40511](https://github.com/openai/codex/commit/cbfd999db7)，9 月 25 日新增 [新用户输入抢占正在生成的响应 / #48141](https://github.com/openai/codex/commit/f92655d07f40c9999915cd9244291ad3acfc2dca)。当前 `codex-rs/core/src/session/turn.rs:2557`、`:2624` 明确会丢弃正在进行的响应流；这种流不应被假设一定以 `response.completed` 结束。

项目当前情况：

- `backend/proxy_fastapi.py:281` 的流处理在正常完成或一般异常时收尾，但 `asyncio.CancelledError` 不属于 `Exception`。`finally` 只关闭 HTTP 资源。
- 隔离模拟流先返回 `response.created`，再抛出 `CancelledError`，将 STORE 替换为 Mock。观测到 `complete_response=0`、`fail_response=0`，两个 HTTP 对象均已关闭。该测试没有写入真实会话。
- `scripts/codex-desktop-proxy.ps1:522` 和 CLI 启动配置目前只接 `UserPromptSubmit` 与旧 `notify`，没有接 `Interrupt`。
- `backend/proxy_store.py:561` 用 `main_turn_id` 是否为空判断主任务运行中；`backend/context_review_scheduler.py:285` 据此跳过自动整理。

已证明的是：取消异常路径没有业务状态收尾，且中断 hook 尚未接入。尚未证明的是：所有真实 Desktop 中断都会永久卡住，或已经发生了新旧请求互相覆盖。后两项必须通过真实客户端或并发集成测试验证。

建议建立统一的请求终态处理（完成、失败、取消），以请求标识校验收尾归属，防止旧流的结束回调清掉新流状态；再用正式的 Stop / Interrupt hook 驱动回合生命周期。按照项目要求，选定当前 Codex 最低版本后干净迁移，不再堆叠多套长期兼容分支。旧 notify 在当前上游仍然存在，不能声称它已经被移除。

## 3. 本地压缩模拟：需要与当前上游重新对齐

相关上游改动：

- [e21bc763a7 / #40273](https://github.com/openai/codex/commit/e21bc763a72adb982522032ce1be725b691dc342)：规范压缩后的用户消息标注。
- [bd3d4d1436 / #48115](https://github.com/openai/codex/commit/bd3d4d1436bb41b94fd38ba9bdd34d74524e7a9f)：完整保留未截断的多段纯文本；仅在截断或包含媒体时生成纯文本回退。
- [49e248d4c3 / #46541](https://github.com/openai/codex/commit/49e248d4c3ad76fc29519bd0aa9e532f448fa90f)：增加可选的最终响应后压缩。

当前上游规则在 `codex-rs/core/src/compact.rs:662`：纯文本、预算足够时保留全部原始分段；否则生成一段文本，清理被舍弃媒体，并同步 `content_item_kinds` 标注。

项目 `backend/compact_controller.py:642` 预算足够就深拷贝整条消息，包括媒体；预算不足时调用 `message_item_with_text`，只替换第一段文本，剩余段继续保留。

实测：两段各 24 字符的文本，预算设为 2 个近似 token，项目返回第一段 8 字符和第二段完整 24 字符，共 32 字符。另一个“短文本 + file_id 图片”输入在模拟压缩后仍含图片，而当前上游会构造纯文本回退。

这里有些偏差属于项目已有边界问题，并非全部由这六周的新提交引入；新上游规则提供了明确的对齐目标。正常情况下，项目下一轮的 cursor diff 会按真实输入重新对齐，所以不能把模拟偏差直接说成必然丢上下文。但它会造成额外 pop/append，并让压缩后的展示和随后编辑面对不准确的模拟状态。

建议重整为一组可独立验收的压缩规则：真实用户消息识别、分段和媒体处理、预算截断、来源标注、初始上下文重注入、摘要形状。重点验证“压缩后用户立刻编辑”和“紧接下一轮请求”。最终响应后压缩是 opt-in，当前项目没有证据表明默认受影响，应单独验证它与自动整理的互斥。

## 4. 图片文件引用：修复范围要精确

上游 [7b8b17b97a / #45794](https://github.com/openai/codex/commit/7b8b17b97a) 支持输入和工具输出中的图片文件 ID；`codex-rs/protocol/src/models.rs:901` 的 `ImageReference` 明确区分 `image_url` 和 `file_id`。

项目 `backend/codex_input_cursor.py:231` 对 `input_image` 只保留 `type/image_url/detail`。隔离输入：

```json
{"type":"message","role":"assistant","content":[{"type":"input_image","file_id":"file_123","detail":"original"}]}
```

经当前响应投影函数后，图片变为 `{"type":"input_image","detail":"original"}`，文件引用丢失。`react_app/src/utils.ts:188` 的图片摘要也只读取 URL。

边界：普通请求 input 经 transcript 读写仍保留原始 `file_id`；上述助手消息是验证转换函数的合成输入，不代表已在用户真实会话观察到这个服务端输出。不能据此声称所有图片上传都坏了。

建议按图片引用联合类型处理，保留 file_id，并展示“文件引用图片”的身份；未经授权取回文件时无需伪造 URL。验收包括 URL 图片、file_id 图片和工具输出内的图片无损往返。

## 5. 模型目录：更新获取方式和静态列表

上游当前 `codex-rs/models-manager/models.json` 包含 GPT-6 Astra / Sol / Luna；9 月 22 日 [#47332](https://github.com/openai/codex/commit/49e95cc73f) 加入 Sol/Luna，9 月 24 日 [#47932](https://github.com/openai/codex/commit/694d8d45bd) 从内置目录移除 GPT-5.4。目录移除本身不等于 API 对所有账号彻底下线。

项目 `simple_agent/config.py:38` 的 Codex 静态选择项和 `backend/proxy_routes_support.py:889` 的模型获取失败兜底列表仍停在旧模型集合。后者仍列 GPT-5.4 / GPT-5.3 Codex / GPT-5.2 等条目。

上游 9 月 19 日的 [model_catalog_url / #46561](https://github.com/openai/codex/commit/888be42a20c5a727214d898c4e335ac2e27161af) 已把模型目录 URL 与推理 base_url 分开。项目重新构造 Studio provider 时没有继承这个新字段。

建议优先复用当前 provider 的实际模型目录和能力信息，保留用户明确指定的目录地址；静态兜底应能识别过期。加入 GPT-6 选择项，但不凭本次源码审查擅自替用户切默认模型。源码内置目录也不证明用户账号拥有所有条目。

费用无需作为兼容性 bug 修改：`react_app/src/components/UsageSummaryCard.tsx:56` 已明确写明“按 GPT-5.6 Sol 参考”，是跨模型参考估算。如产品需要实际分模型成本，再单独设计价格表并核验官方价格；本次不猜测 GPT-6 价格。

## 6. 可选提升，不列为阻断升级

- `configuration_update` 是历史中的持久 reasoning 设置。上游 [#42328](https://github.com/openai/codex/commit/0d502a4230) 扩充了这条路线。项目已在隔离测试中证明可无损往返，但没有专门的展示分类。可添加清晰标签，并在上下文编辑中让其控制语义可见。
- `content_item_kinds` 能区分基础指令、环境、用户输入等来源；`cell_id`、`tool_calls_complete`、`executed_tool_calls` 有助于解释 Code Mode 调用。可逐步用这些正式字段改善展示，替代部分文本猜测。
- 9 月 26 日 [include_internal_metadata / #48344](https://github.com/openai/codex/commit/c9e25207073a88f1a3a4a885991b9143799a084f) 新增 provider 元数据能力。当前 Studio 使用自定义 provider 名称，上游另有 `is_openai` 条件清理 item metadata，不能简单打开一个开关就宣称完整支持。仅在需要元数据展示且明确上游目的地时再设计这部分，不应向任意第三方盲目转发内部字段。

## 已排除的“大改”理由

1. **不用为了 GPT-6 重写 Lite 识别。** 当前上游仍是 `additional_tools` 后跟 base developer。新增稳定 ID 和来源标注未改变顺序。合成带 ID/标注的 Lite 请求仍被项目正确识别。对应上游 [#40962](https://github.com/openai/codex/commit/e77773085c)、[#40296](https://github.com/openai/codex/commit/84c989acf9)。
2. **目前不必改造 WebSocket 代理。** 三条启动/配置路径都明确 `supports_websockets=false`，当前 Codex `core/src/client.rs:1024` 仍遵守它。若未来主动支持 WebSocket，需要额外实现增量 input、连接状态和编辑后的状态重建；官方 [WebSocket 说明](https://developers.openai.com/api/docs/guides/websocket-mode) 也定义了 `previous_response_id` 延续机制。
3. **远程 compact V2 变化不等于当前本地 compact 路径失效。** 上游 `model-provider/src/provider.rs:422` 仅对受支持 provider 启用远程压缩。项目生成的 provider 名为 `Codex Context Studio`、URL 为 loopback，仍落入不支持远程压缩的分支。不要只把 provider 名改成 OpenAI，否则可能引入项目未实现的压缩路线。
4. **Guardian 已有透传判断可以覆盖已检查的调用。** 新版 classifier 请求虽回到 `/responses`，仍发送 `x-openai-subagent: guardian`；项目会据此走透传。未发现必须按旧专用 URL 增加分支的证据。
5. **新扩展 API 暂不能替代本项目代理。** [#47679](https://github.com/openai/codex/commit/8de2d336b880bed7e6135fedfc2e65e722a744b3) 的 `ModelRequestContributor` 只能追加受限 client_metadata，`ModelResponseInterceptor` 处理响应流；接口未提供任意改写请求 input 的能力，也不是已证实可安装的外部用户插件入口。

## 验证结果和后续实施建议

执行 `node scripts/run-proxy-core-tests.mjs`，22 个测试入口全部通过。此次没有改动前端或应用代码，未额外执行前端构建。现有测试通过只能说明已有场景未失败，不能替代新版协议验证。

另外完成以下隔离探针：独立工具结果校验/投影、带消息 ID 的独立结果、file_id 图片投影、多段用户文本预算截断、媒体压缩回退、configuration_update 无损往返、带稳定 ID 的 Lite 提示词定位、模拟取消 SSE 流的状态收尾。

未验证：真实 Codex CLI/Desktop 经此代理的新版本联调、真实模型调用、真实附件取回、Stop/Interrupt 与自动整理并发、post-turn compaction。没有据源码差异直接修改业务行为。

建议按三个完整边界实施：

1. **协议语义统一**：独立工具结果、图片引用、控制项统一到同一套协议定义，更新转换、校验和展示，不在不同模块各加一套特例。
2. **请求与回合生命周期统一**：区分一次模型请求结束与整个回合结束，处理取消并防止旧请求收尾影响新请求，再接 Stop/Interrupt。
3. **压缩规则与模型目录更新**：用当前上游结构制作固定合约样本，对齐保留消息规则，再更新模型发现与兜底。

没有证据要求推翻整个项目。最值得重整的是散落在字段白名单、共享 registry、前端类型和编辑校验中的协议规则，以及当前响应完成/回合完成各自维护状态的边界。
