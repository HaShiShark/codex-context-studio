# Codex Context Studio 文档

本目录按“产品原则 → 系统架构 → 功能设计 → 运行维护”组织。文档描述当前有效设计，不保留旧路径兼容文件。

## 阅读入口

第一次了解项目：

1. [产品原则](product/principles.md)
2. [系统架构](architecture/overview.md)
3. [代理核心设计](architecture/proxy-core.md)

修改具体功能时，直接阅读对应功能文档及其决策记录。安装、开发环境和会话迁移问题见[运行目录与开发隔离](operations/runtime-layout.md)。

## 文档地图

### 产品

- [产品原则](product/principles.md)：产品目标、业务边界和开发纪律。

### 架构

- [系统架构](architecture/overview.md)：代理、Web 后端、React 工作台和上游模型的数据流。
- [代理核心设计](architecture/proxy-core.md)：transcript、cursor、请求处理、响应处理、local compact 和 Workbench 的技术准绳。
- [Responses Lite 协议](architecture/protocols/responses-lite.md)：标准 Responses 与 Lite 的识别、提示词位置和 compact 前缀规则。

### 功能

- [上下文模型与工具循环](features/context-workbench/context-model.md)：上下文模型输入、节点工具、单轮草稿、循环提交和异常边界。
- [节点锁定](features/context-workbench/node-locking.md)：上下文地图锁定语义与主模型/上下文模型互斥。
- [上下文智能建议](features/context-suggestions/design.md)：自动建议、预览、应用、丢弃和调度器。
- [智能建议决策](features/context-suggestions/decisions.md)：持久化、取消、进程识别和文案边界。
- [Exec 展示](features/context-map/exec-display.md)及其[决策记录](features/context-map/exec-display-decisions.md)。
- [Additional Tools 展示](features/context-map/additional-tools.md)及其[决策记录](features/context-map/additional-tools-decisions.md)。
- [Assistant 活动与 Compact](features/context-map/assistant-activity.md)及其[决策记录](features/context-map/assistant-activity-decisions.md)。

### 运行维护

- [运行目录与开发隔离](operations/runtime-layout.md)：production/development 注册、外部写入和会话恢复。

### 图片

- `assets/screenshots/cn/`：中文界面截图。
- `assets/screenshots/eng/`：英文界面截图。

## 事实来源优先级

文档发生冲突时按以下顺序判断：

1. `architecture/proxy-core.md`：代理主链路和状态模型。
2. `architecture/protocols/`：具体线上协议。
3. `features/**/design.md` 或具体功能文档：功能行为。
4. `features/**/*-decisions.md` 和 `decisions.md`：为什么采用当前方案。
5. `product/principles.md`：产品方向和工程约束，不承担协议细节。

测试数量、一次性排查过程和发布验证结果不写入长期设计文档，应记录在 PR、提交或 Release Notes 中。
