# Hash Context for Codex 文档入口

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.2 |
| 最后更新 | 2026-05-22 |
| 状态 | 已拆分为正式 PRD 和技术实现文档 |

这份文件原来是 Codex Context Proxy 的单页方案稿。当前项目已经从早期 MVP 演进为包含 CLI shim、Desktop 实验支持、Electron 小窗、Responses 代理、上下文工作台、revision、compact 接管和用量统计的完整工具，所以旧文档不再作为核心说明使用。

请优先阅读：

- [核心功能 PRD](./核心功能PRD.md)：讲产品目标、用户场景、功能需求、里程碑和风险。
- [核心功能技术实现](./核心功能技术实现.md)：讲当前代码结构、启动链路、代理实现、工作台实现、API 和测试。

新文档已经按当前项目代码和本机当前 Codex 源码 `D:\opensource\codex` 重新核对。尤其是 provider 名称、HTTP Responses 字段、remote compact 触发条件和 hook 配置，均以当前代码为准。

## 当前一句话方案

> Codex 请求先经过本地 Responses 代理。未编辑时透明转发；用户在小窗编辑上下文后，代理从下一轮开始把 edited transcript 编译回 Responses `input`，并同步处理 compact、恢复和 usage 统计。

## 当前核心边界

- 不修改 Codex 源码。
- CLI 主路径使用 shim 或 `npm run codex` 注入本地 provider。
- Desktop 是实验路径，会托管写入 `.codex/config.toml` 的配置块。
- `backend/proxy_server.py` 是 Codex 请求边界的 source of truth。
- `backend/web_server.py` 和 React 小窗负责上下文地图、工作台、revision 和设置。
- Codex 原始 UI、history 和 session 文件仍由 Codex 自己维护。
