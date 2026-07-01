# Codex Context Proxy v1.0.0

大重构更新。

## Updates

- 重构后端代理架构，迁移到 `proxy_fastapi` 代理入口。
- 统一前后端 transcript / context contract，减少代理层和工作台之间的数据漂移。
- 拆分代理存储、SSE、session id、item registry 等核心模块，降低后续维护成本。
- 增加前端 contract 测试和后端代理核心测试，覆盖重构后的关键协议路径。
- 更新 Windows / macOS 打包配置，确保新共享模块随安装包发布。
