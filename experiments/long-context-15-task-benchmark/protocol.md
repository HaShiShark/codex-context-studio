# Long Context 15-Task Benchmark Protocol

这个实验用于轻量对比原生 Codex 与 Hash Context 手动压缩在长会话编码任务中的表现。目标不是做严肃论文级 benchmark，而是得到一组适合简历和公开说明的量化结果。

## Experiment Goal

连续发送 15 个编码任务，中途不逐个验收，也不把失败结果反馈给 Codex。全部任务完成后，统一运行最终验收脚本，比较两组的任务通过数和总成本。

## Groups

| Group | Description |
| --- | --- |
| Native Codex | 原生 Codex，连续执行 15 个任务，不进行手动上下文编辑。 |
| Hash Context | 通过 Hash Context Proxy 执行同样 15 个任务，并在 T05 和 T10 后进行手动上下文压缩。 |

## Shared Rules

- 两组使用相同起始 commit。
- 两组使用相同模型、reasoning 设置和任务顺序。
- 15 个任务在同一个 Codex 会话内按顺序发送。
- 中途不运行任务级验收，不向 Codex 反馈某个任务是否失败。
- 最终只在全部 15 个任务结束后运行一次统一验收。
- 最终成本直接从 usage 面板记录总成本即可，不记录每轮 token。

## Hash Context Compression Rules

Hash Context 组只在以下时间点进行手动压缩：

- T05 完成后压缩一次。
- T10 完成后压缩一次。

允许压缩或删除：

- 长工具输出。
- 失败尝试。
- 重复解释。
- 已经过时的文件读取结果。
- 对后续任务没有帮助的中间日志。

不允许修改或删除：

- 用户原始任务需求。
- 已确认的产品/技术决策。
- 当前最终实现结论。
- 会影响后续验收的关键文件路径、接口名、命令名。

## Final Metrics

| Metric | Meaning |
| --- | --- |
| Passed Tasks | 15 个任务中最终验收通过的数量。 |
| Failed Tasks | 15 个任务中最终验收失败的数量。 |
| Total Cost | usage 面板显示的总成本。 |
| Pass Rate | `Passed Tasks / 15`。 |
| Tasks Per Dollar | `Passed Tasks / Total Cost`，可选。 |

## Public Reporting Notes

公开到 GitHub 时建议只提交：

- 本协议。
- 15 个任务原文。
- 最终验收脚本。
- 最终评分表。

不建议直接公开完整 Codex transcript，因为可能包含本地路径、环境变量、账号信息或临时日志。
