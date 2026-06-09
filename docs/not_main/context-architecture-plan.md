# hash-code 下一步架构计划

更新日期：2026-04-22

这份文档只写今晚我**不适合直接拍板实现**、但又必须尽快定下来的部分。基础客户端能力我已经先往前补了；这里留的是跟你产品核心强相关的那层。

## 今晚已经先落下来的安全项

- OpenAI 设置页和本地持久化
- 前后端设置接口
- 文件 / 图片附件上传链路
- 附件随消息入库
- 工具调用主消息区展示
- 流式聊天链路继续保留

这几块都属于“以后也会用到”的底层，不会浪费。

## 现在先不要急着定死的部分

### 1. 会话存储不能再只是 transcript 数组

当前为了让客户端先能用，消息还是按这种结构落盘：

```json
{
  "role": "user",
  "text": "帮我看附件",
  "attachments": [],
  "toolEvents": []
}
```

这足够支撑普通聊天、附件、工具回显，但**不够支撑你的核心产品方向**。  
因为后面你要做的是：

- 上下文地图
- 压缩节点
- 原文 / 摘要并存
- 一键恢复
- 自动上下文代理
- token 预算和缓存友好度

所以后面一定要从“消息数组”升级到“节点图”。

### 2. SDK 请求装配也不能永远直接拼 transcript

当前安全做法还是：

1. 系统指令
2. 全部历史消息
3. 当前用户输入
4. tools

这对于普通客户端没问题，但不适合你后面要做的“最终发送上下文可控”。

真正该有的是一层 **context compiler**：

```text
原始聊天记录 / 工具输出 / 压缩节点 / 固定节点
-> 上下文选择器
-> 上下文编译器
-> 最终发给 Responses API 的 input
```

也就是说，后面发给模型的内容不该直接等于聊天历史，而应该等于“上下文地图当前勾选后的编译结果”。

## 建议的数据模型

### Session

```ts
type Session = {
  id: string;
  title: string;
  scope: 'chat' | 'project';
  projectId: string | null;
  createdAt: string;
  updatedAt: string;
  nodeIds: string[];
}
```

### ContextNode

这是后面真正的核心。

```ts
type ContextNode = {
  id: string;
  sessionId: string;
  kind:
    | 'user_message'
    | 'assistant_message'
    | 'tool_call'
    | 'tool_result'
    | 'attachment'
    | 'summary'
    | 'system_memory'
    | 'working_memory';
  role?: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  tokenEstimate: number;
  sourceNodeIds: string[];
  derived: boolean;
  included: boolean;
  pinned: boolean;
  archived: boolean;
  createdAt: string;
  updatedAt: string;
  meta: Record<string, unknown>;
}
```

这里最关键的是这几个字段：

- `sourceNodeIds`
  用来表达“这个摘要是由哪些原节点压出来的”
- `derived`
  区分原始节点和派生节点
- `included`
  表示当前是否进入上下文
- `pinned`
  表示自动策略不能动它
- `meta`
  后面可挂 attachment、tool、model、cache 信息

### ContextRevision

这个是为了“一键恢复”。

```ts
type ContextRevision = {
  id: string;
  sessionId: string;
  label: string;
  nodeStates: Array<{
    nodeId: string;
    included: boolean;
    pinned: boolean;
  }>;
  createdAt: string;
}
```

只要有 revision，你右边改动页和恢复按钮就有根了。

## Responses API 这一层后面该怎么装

根据今晚重新确认过的官方能力，后面我们应该按这个思路去收：

### 第一层：稳定前缀

- system / developer instructions
- tool schemas
- skills / MCP / file search 这类长期稳定的配置
- 用户长期偏好

这层尽量少动。  
不是为了省钱，而是为了让缓存和行为都更稳定。

### 第二层：工作记忆

- 当前任务目标
- 已确认约束
- 压缩摘要
- 最近阶段结论

这层应该是你“上下文地图”的主要编辑对象。

### 第三层：实时尾部

- 最近几轮原始消息
- 最新工具结果
- 当前用户输入

这层变化最大，也最适合流式和自动整理。

## Responses API 里后面重点要吃透的字段

后面真做上下文控制时，重点不是“能不能请求成功”，而是这些字段要不要接进来：

- `model`
- `instructions`
- `input`
- `reasoning.effort`
- `tools`
- `tool_choice`
- `prompt_cache_key`
- `usage.input_tokens`
- `usage.input_tokens_details.cached_tokens`
- `output`
- `response.output_text.delta`
- `response.output_item.added`
- `response.function_call_arguments.delta`
- `response.function_call_arguments.done`

尤其是：

### `prompt_cache_key`

这不是必须，但后面如果你做“富豪模式 / 省钱模式”，它会很有用。  
因为它可以帮助相似请求更容易落到同一条缓存路径上。

### `usage.input_tokens_details.cached_tokens`

这个以后应该直接进 UI。  
你的产品既然要做上下文地图，就不该只显示 token 总数，还应该显示：

- 本轮总输入 token
- 缓存命中的 token
- 新增 token

## MCP / RAG / Skills 的接入优先级

今晚不建议硬做，是因为这三块都不只是“多加一个按钮”。

### 1. MCP

建议先做成**配置层 + 注册层**，别先做 UI 花活。

后面结构应该是：

```ts
type McpServerConfig = {
  id: string;
  name: string;
  serverUrl: string;
  requireApproval: boolean;
  enabled: boolean;
}
```

然后请求时再把启用的 MCP server 转成 Responses API 的 remote MCP tool 配置。

先别一上来就做成“用户随便加几十个 MCP”，会很乱。

### 2. RAG

建议第一版不要做成复杂知识库系统，先做成：

- 本地文件上传
- 分块
- embedding
- 本地索引
- 查询时召回
- 把召回结果作为 context node 进入上下文地图

注意：  
**RAG 返回的块不该直接偷偷塞给主模型**，而应该变成可见节点。  
这点特别符合你整个产品的气质。

### 3. Skills

如果继续走 OpenAI Responses API，skills 更适合被当成“稳定前缀能力”的一部分。  
也就是说它更像：

- 预定义工作流
- 预定义工具组
- 预定义系统偏好

而不是简单的“插件按钮”。

## 自动上下文代理应该分三阶段做

### 第一阶段：建议模式

它只提建议，不直接改：

- 建议压缩哪些节点
- 建议排除哪些重复工具输出
- 建议保留哪些固定事实

### 第二阶段：半自动模式

允许它自动压缩，但不允许静默删除关键节点。

### 第三阶段：全自动模式

这时候再做你说的“富豪模式”。

但要同时具备：

- 审计记录
- 一键恢复
- 用户自定义提示词
- 可固定 / 不可动节点

## 明天最值得你拍板的几个问题

### 1. 上下文地图里是否区分“原始消息节点”和“最终发送节点”

我建议：

- 聊天主区：展示原始历史
- 右侧地图：展示最终发送节点
- 压缩后在原文位置加标识，但原文不被改写

### 2. 第一版自动代理到底能自动做什么

我建议先允许它：

- 压缩大工具输出
- 提炼当前任务状态

先别允许它静默删原始消息。

### 3. 设置页里是否立刻暴露“策略模式”

我建议下一步就可以开始放：

- 省钱模式
- 平衡模式
- 富豪模式

但先只做 UI 和参数结构，不急着把后台代理一次性全实现。

## 我建议的下一步开发顺序

1. 先把当前 `sessions + transcript` 升级成 `sessions + nodes + revisions`
2. 然后把右侧上下文地图改成真正吃 `ContextNode`
3. 再把工具调用和附件也纳入 node 体系
4. 接着补 `usage` / `cached_tokens` 的记录和展示
5. 最后才做自动上下文代理

## 一句话收束

你这个项目后面最关键的，不是“支持多少模型”，而是：

> 把聊天记录、工具输出、摘要、RAG 召回、MCP 结果，全部统一成可见、可控、可恢复的上下文节点。

一旦这层收住了，后面的富豪模式、上下文代理、缓存友好度、自动压缩，都会顺着长出来。
