# 原生工作流缺陷、兼容性风险与既定设计核对

> 本文保留修复前审查证据。已落地修改、验证结果和剩余边界见[原生工作流修复记录](2026-09-26-native-workflow-fixes.md)。

日期：2026-09-26。项目：`a53ee10846c96505ba8ed1275e6de7cd02a4496f`。Codex：`e72da2b53805894878023d01949a25a082e0a5cb`。

## 统计口径

**更正：撤回此前“11 类已确认问题”的总数。** 第 10 项是用户刻意设计、且项目文档明确要求的互斥锁，不是 bug，不进入修复范围。其余发现也必须区分实现缺陷、条件性兼容风险与尚未核清设计意图的配置差异，不能把“与原生不同”直接当作错误。

当前保留原编号便于追踪：1、4、5、7 有接口/数据完整性或合法协议校验的缺陷证据；6、8、9 有隔离边界复现，但实际触发与完整影响仍需逐项验证；2、3、11 是能力/配置/目录核对项，其中配置改动尤其不能未经设计核对就全部恢复成原生。此分组不代表剩余十项都是十个应直接修改的 bug。

判断标准：只有实现违反已确认的产品设计，或造成该设计之外的非预期原生能力损失，才进入修复范围。这里统计的是本次已经检查的范围，不承诺覆盖所有未来 Codex 版本或所有原生功能。

有些问题早于上次项目提交就已存在。这次目标是检查接入是否干扰原生工作流，因此没有只统计最近六周引入的问题。

没有修改应用代码、用户 Codex 配置或运行中的代理。隔离测试使用合成数据、临时目录或 Mock；未使用真实模型调用复现用户的图片报错。

## 发现项及设计核对总表（第 10 项已排除）

| 编号 | 优先级 | 问题 | 确认方式 | 影响及触发条件 |
| --- | --- | --- | --- | --- |
| 1 | P1 | provider 地址整体改到代理，但原生接口未完整承接 | 隔离 HTTP 测试 + 上游路由源码 | 图片生成、图片编辑到代理返回 404；独立搜索路径也未实现 |
| 2 | P1 | 换成 Studio provider 后，原生能力判断改变 | 两端源码 | 独立 web.run 注册条件失去 OpenAI 身份与能力标记；特定 history notes 功能也依赖原 provider 身份 |
| 3 | 待核对设计 | 重建 provider 配置与原配置有差异 | 配置生成源码 | 自定义 headers、query_params、模型目录等不继承；部分模式设为 200000 上下文；UserPromptSubmit 被替换。须分别核对设计意图，不能整体认定为 bug |
| 4 | P1 | SSE 解析不能无损处理所有合法分块/换行 | 隔离输入测试 | UTF-8 字符跨 chunk 时丢字；CRLF 事件分隔不被解析，可能漏掉完成事件 |
| 5 | P1 | 响应入库前丢掉请求对应的投影状态 | 临时 ProxyStore 测试 | 即使本轮输入带 ID 和元数据，响应入库仍丢失它们，工作台与下一轮 Codex 历史不一致 |
| 6 | P1 | 取消及交错请求的收尾没有完整隔离 | 模拟取消流 + 临时 ProxyStore 测试 | 取消流只关闭连接；旧响应完成可清掉新请求运行状态、追加旧输出 |
| 7 | P1 | 合法独立工具结果无法通过编辑校验 | 合成协议输入测试 | 无 call_id 的命名输出被判非法；响应投影丢掉 name/namespace |
| 8 | P1 | 本地压缩模拟与当前 Codex 保留规则有偏差 | 合成压缩输入 + 上游源码 | 多段文本截断超出预算；图片仍被保留，与上游纯文本回退不一致 |
| 9 | P2 | 图片 file_id 在特定响应投影中丢失 | 合成转换输入测试 | input_image 只保留 URL 字段；普通原始请求 input 透传不等于也有此问题 |
| 10 | 保留，不修 | 副模型运行期间阻塞主 Codex 的互斥锁 | 用户明确确认 + 原有设计文档 | 确保 transcript 只有一个有效写入者；等待、提交后释放是设计要求 |
| 11 | P2 | 模型选择与获取失败兜底列表过时 | 两端模型目录源码 | 静态项缺 GPT-6；兜底仍列上游内置目录已移出的部分旧模型 |

P1 表示应优先处理，而非所有用户都已经触发故障。

## 1. 缺失原生接口

项目三个启动/配置入口都把 model provider base_url 指向 `http://loopback:port/v1`。代理注册的 provider 路由只有：

```text
GET  /v1/models
POST /v1/responses
POST /v1/responses/compact  （主动拒绝）
```

当前 Codex 的 `ext/image-generation/src/backend.rs:84` 使用当前 provider 构造图片客户端；`codex-api/src/endpoint/images.rs:68`、`:81` 分别追加 `images/generations`、`images/edits`。

隔离调用当前 FastAPI app，结果：

```text
POST /v1/images/generations -> 404
POST /v1/images/edits       -> 404
POST /v1/alpha/search       -> 404
```

第三条来自 `codex-api/src/endpoint/search.rs:15` 的原生独立搜索地址。它当前还受下一项能力注册条件影响，不应声称每次搜索都会走到这个 404。

这是已证实的项目缺陷。用户反馈的那次图片故障仍缺少具体日志，不能直接画等号。只修图片内容字段不能解决这个接口缺失问题。

## 2. provider 身份与能力变化

项目 `scripts/codex-desktop-proxy.ps1:514` 写入 `name = "Codex Context Studio"`，CLI 两条启动路径采用同样身份。

当前 Codex：

- `model-provider-info/src/lib.rs:606`：`is_openai()` 按 provider name 判断。
- `ext/web-search/src/extension.rs:47`：独立搜索扩展要求 OpenAI 身份、actor authorization 或 `supports_standalone_web_search`。Studio 生成配置未保留原能力标记，默认 false。
- `ext/history-notes/src/extension.rs:51`：启用该扩展的 token budget 配置还需 OpenAI 身份与 Codex backend 登录。

所以接入可能在请求产生之前就改变模型能看到的工具。独立 web.run 不注册并不代表所有 hosted web search 形式都不可用；history notes 也仅在原本满足其启用条件时构成能力损失。

原生图片扩展还允许 `requires_openai_auth`，不能仅凭改名就声称订阅用户图片工具一定消失。图片路径缺失有更直接的证据，见第 1 项。

## 3. 原 provider 配置未完整保留

`scripts/codex-desktop-proxy.ps1:278` 起只提取部分字段，`:513` 起重新生成一个极简 provider；`scripts/codex-ctx-proxy.ps1:620`、`scripts/codex-with-context.ps1:235` 也重建配置。

已确认的差异（差异本身不等于 bug，必须核对其设计目的和非预期影响）：

- 原 provider 的 `http_headers`、`env_http_headers`、`query_params`、`model_catalog_url`、超时/重试等没有整体继承。受影响的是原本依赖这些设置的用户，不能推断默认登录全部失败。
- `requires_openai_auth=false` 分支将 `model_context_window` 写成 **200000**，覆盖原模型/用户的上下文长度选择。
- 原 `UserPromptSubmit` 设置被删除并写为 Studio hook，不能保证用户已有 hook 的行为保留。旧 notify 有转发原命令的逻辑，不能说所有通知都被丢掉。

另一个潜在边界是同一机器有订阅与 API key 时的身份选择，但未在隔离配置场景验证，因此没有另计一项确定故障。

## 4. 流解析丢字与漏事件

`backend/proxy_fastapi.py` 对每个网络 chunk 单独执行 `decode("utf-8", errors="ignore")`，没有增量解码器。

测试把合法 `response.completed` JSON 中的“中”字 UTF-8 三字节拆到两个 chunk，按当前代码解码并调用解析器：

```text
expected: \u4e2d\u6587   （中文）
actual:   \u6587         （文）
```

`backend/proxy_routes_support.py:962` 只用 `\n\n` 分割事件。将同一个合法事件改为 `\r\n` 换行，结果 `completed_count=0`，整个事件仍留在 buffer。

转发给 Codex 的原始响应字节可能仍是正确的，损坏的是代理解析结果；它随后进入 transcript，又参与下一轮重建，因此仍然会影响实际模型输入。CRLF 场景还可能被记录为“没有 response.completed”。

## 5. 入库投影状态丢失

`backend/proxy_core.py` 根据请求设置 `request_item_ids`、`request_item_metadata`、`request_turn_id`。但 `backend/proxy_store.py:970` 的完成逻辑重建 ProxyState 时，只复制部分字段，没有复制这些投影选项。

临时存储复现：请求 user 带 `id=msg_u` 和 `turn_id=turn_A`，begin_request 后确认 `request_item_ids=true`；返回 assistant 带 `id=msg_a` 和相同元数据。complete_response 后存储项只剩 type/role/content，ID 与元数据消失。

这是第 7、9 项字段白名单问题之外的另一层缺陷。下一轮未编辑历史可能通过 diff 纠正，不能声称每次必然丢对话；但当前状态已不等价，且会产生额外重建。

## 6. 取消与交错收尾

两条隔离复现：

1. 假响应流输出 `response.created` 后抛出 `asyncio.CancelledError`。HTTP 对象均关闭，STORE 的 complete_response 与 fail_response 调用数都是 0。
2. 同一临时 session 连续 begin 请求 A、B；B 尚未完成时调用 A 对应的完成回调。由于接口只有 session_id，没有请求归属校验，状态从 running 变 mirror，当前 inflight 被清空，旧输出追加到新状态。

第二条是按指定时序复现 store 行为，没有声称真实客户端必然产生这个顺序。上游现有输入抢占机制使这类交错值得专门验收。项目也尚未接入新的 Interrupt hook。

## 7—9. 工具结果、压缩、图片引用

详见 [上游协议审查](2026-09-26-codex-upstream-review.md) 的第 1、3、4 节，其中包含输入与实测结果：

- 独立工具结果：缺 call_id 报错；加消息 id 后又被误当调用 id；name/namespace 在响应投影丢失。
- 压缩：两段各 24 字符文本，2 个近似 token 预算后仍保留 32 字符；媒体回退也不匹配。
- 图片引用：特定响应 message 中 input_image 的 file_id 经转换被丢弃。该输出形状为合成探针；原始请求 input 无损往返能力仍存在。

## 10. 副模型与主 Codex 互斥：既定设计，不是 bug

`backend/proxy_fastapi.py:756` 在主请求开始前等待 context idle；`backend/proxy_store.py:1346` 默认超时 **900 秒**。只要同一 session 的 context_request_id 尚未清空，主请求就在这里等待；超时后返回 409。

用户已明确确认：这是特意设计的锁。原有 `docs/features/context-workbench/context-model.md:314` 的“与主 Codex 互斥”章节，以及 `docs/features/context-workbench/node-locking.md:15` 均明确要求上下文模型运行期间，普通主 Codex 请求等待直到上下文模型结束，目的是确保主 transcript 只有一个有效写入者。

此前建议将锁缩小为“仅提交时锁定”是审查者未核对设计便作出的错误建议，现撤回。保留整个副模型运行期间的互斥，不以“原生兼容”为理由解除、缩短或绕过它。

只有结束/取消后未按设计释放锁、错误会话被锁住等违反锁契约的行为，才可能属于 bug；本次没有证明这些锁故障，不能从正常等待或超时设置本身推断出来。第 6 项检查的是主请求响应流及其交错收尾，不能将它混同于这里的副模型互斥设计。

## 11. 模型目录

`simple_agent/config.py:38` 和 `backend/proxy_routes_support.py:889` 保留旧静态模型集合；上游最新目录已有 GPT-6 Astra/Sol/Luna，且内置目录移除了部分旧模型。会影响选择和获取失败兜底，不代表所有旧模型 API 都已经不可用。

费用面板明确标注 GPT-5.6 Sol 参考价，属于当前产品定义，不计入计费 bug。

## 其他既有设计选择，不计入 bug

1. **强制关闭 WebSocket，走 HTTP。** 这是现有主动配置，不能称为新故障，但传输方式与原生并非完全相同，性能也未经等价验证。
2. **主动使用本地压缩，拒绝远程压缩。** 这是用户原有明确产品选择，用于可见、可编辑的摘要和压缩提示词替换，不能借“兼容”擅自删除。它依赖 provider 能力差异；因此不能只将 provider 名称改回 OpenAI 就认为问题解决，否则可能切入项目未实现的远程压缩路线。

这两项连同第 10 项互斥锁，都不能仅因区别于原生默认行为就列为 bug。兼容性工作应在保留项目既定能力与约束的前提下消除非预期影响；涉及改变产品设计时须先单独说明，不能在修复中顺带迁移。

## 已排除或尚不能下结论的范围

- 检查到的 MCP OpenAI 文件上传用 `config.chatgpt_base_url`，见上游 `core/src/mcp_openai_file.rs:248`；不是当前替换的 model provider base_url。不能因为本地缺 `/files` 就认定所有附件上传失败。
- Lite 基础指令位置仍能被识别，configuration_update 可无损透传。
- 没有证据说所有 MCP、代码执行、子代理、浏览器、语音都已被破坏；这些不能列为“确定坏了”。
- 没有针对真实账号完成原生工作流全量联调，不能承诺本表已经穷尽所有问题。

## 修复顺序的依据

先修有明确非预期失败证据的接入缺口与数据完整性问题（1、4、5、7）。对 2、3 先核对能力损失及配置设计意图；对 6、8、9 补齐相应场景的契约和影响验证；11 按模型目录维护处理。第 10 项互斥锁不进入修复计划，也不因优化请求生命周期而被顺带改变。

保留 transcript 作为唯一业务真相、cursor 作为对齐基线的核心设计。将通用网络代理职责与上下文编辑职责分清楚，避免每发现一个原生工具就添加一套业务特例。
