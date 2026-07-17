# 上下文地图中的 Exec 展示

## 目标

在上下文地图中将 Codex Code Mode 的 `exec` 调用呈现为易读的嵌套工具活动，同时保持规范的 Responses transcript 不变。

当前 Codex 在线协议中的结构是一个外层自定义工具调用：

```json
{
  "type": "custom_tool_call",
  "name": "exec",
  "input": "const results = await Promise.all([...]);"
}
```

`tools.shell_command(...)` 等嵌套调用是 `input` 内的 JavaScript 表达式，并不是 transcript 中独立的 provider item。

## 不变量

1. `providerItem`、`inputIndex` 和 transcript 节点分组保持不变。
2. Exec 展开仅是前端展示投影。
3. 解析使用 JavaScript 解析器，绝不执行模型生成的代码。
4. 未知、无效或动态程序回退到现有的外层 exec 展示，不报告猜测出来的命令数量。
5. 只有外层 exec item 已完成时，嵌套命令才显示为已完成。若 transcript 只有聚合的 exec 输出，就不虚构单条命令输出。

## 支持的静态结构

首个实现支持 Codex 常见的以下结构：

- 直接嵌套调用：`tools.shell_command({ command: "..." })`。
- 并行数组：`Promise.all([tools.shell_command(...), ...])`。
- 静态数组映射：

```js
const commands = ["rg --files", "git status --short"];
await Promise.all(commands.map(command => tools.shell_command({ command })));
```

- 静态元组映射：

```js
const tasks = [["files", "rg --files"], ["status", "git status --short"]];
await Promise.all(tasks.map(([name, command]) =>
  tools.shell_command({ command })
));
```

对于不支持的循环、分支、变量修改、动态计算的工具名和无法静态解析的值，不给出精确运行次数。

## 展示行为

- 只包含命令工具的 exec 使用现有的本地化命令组标签，例如 `已运行 3 条命令`。
- 混合类型的 exec 使用现有的通用工具组标签。
- 展开分组后，每个能静态解析的嵌套调用显示为一条简洁记录。
- 只有一个嵌套调用时，可以复用外层 exec 输出；有多个调用时显示如实的聚合结果提示，因为 Responses transcript 中没有单个调用的输出。
- 解析失败时保留当前的外层 exec 卡片。
- 运行中或失败的 exec 调用保留外层 exec 卡片，因为静态源码无法证明实际完成了多少个嵌套调用。
- 上下文地图渲染始终接收当前 UI 语言区域设置。

## 验证

- 契约测试覆盖直接调用、并行调用、静态映射、元组映射、混合工具、无效源码、动态分支、未调用函数、未完成的 exec 调用和动态回退。
- TypeScript 类型检查和 Vite 生产构建必须通过。
- 使用当前捕获到的 Codex exec item 验证生成的数量和命令预览。

## 2026-07-13 验证结果

- 当前真实 transcript 将静态 exec 组显示为 `已运行 3 条命令`和`已运行 4 条命令`。
- 展开的中文上下文地图节点中不再出现英文 `Used N tools` 或 `Ran N commands` 标签。
- 展开三命令分组后显示了全部三个命令预览。由于 transcript 只保存外层聚合输出，每一行都显示明确的聚合输出提示，而不是复制输出。
- `npm test` 已通过，包括类型检查、14 项 transcript-codec 测试、代理/后端测试套件和前端 exec-display 契约测试。
