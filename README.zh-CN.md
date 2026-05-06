# Codex Context Proxy

Codex 的可视化、可编辑上下文层。让 AI 像手术刀一样精准地编辑 AI 的上下文，让你更自由地维护 Codex 正在看见的内容。

## 我们做了什么

Codex Context Proxy 给官方 Codex 加上了一层可视化、可编辑的上下文。

Codex 很适合长时间的代码任务，但它的上下文会逐渐变得难以查看、也难以维护。工具日志、失败尝试、过期假设、重复的对话片段都会不断累积。到后面，你通常很难知道 Codex 下一次回答前到底会读到什么，也很难在下一轮之前精准移除噪声上下文。

这个项目在 Codex 前面加了一个本地上下文编辑器。你仍然可以继续使用正常的 Codex 工作流，同时代理会捕获 Codex 的实时上下文，并打开一个工作台，让你在后续回复前可视化、编辑、压缩、删除上下文。

简单说：

```text
Codex 负责写代码。
Codex Context Proxy 负责维护 Codex 的上下文。
```

## Codex 获得了什么能力

- 上下文可视化：查看 Codex 即将使用的对话、工具历史和上下文节点。
- 上下文可编辑：压缩、删除或重写选中的上下文内容。
- AI 编辑 AI 的上下文：用另一个 AI 编辑主 AI 的上下文。
- 更精准的压缩：用定向上下文手术替代粗暴的自动压缩。
- CLI 和桌面端支持：支持 Codex CLI，也实验性支持 Codex Desktop。
- 保持原有工作流：继续从终端或桌面端使用 Codex。

上下文编辑工作台适配自 HashCode。原项目更详细地解释了 “AI edits AI's context” 这个想法：

https://github.com/HaShiShark/context-editor-agent

## 截图

### 可视化 Codex 上下文和 Token 使用情况

![Visualize Codex Context](docs/images/context-map.png)

### 让 AI 检查当前上下文

![Edit Context With AI](docs/images/context-workbench.png)

### 压缩噪声工具上下文

![Compress Context](docs/images/context-compress.png)

## 功能

### 实时上下文图

Codex Context Proxy 会把 Codex 会话转换成结构化的上下文图。它不会把 transcript 当成一整堵文本墙，而是把用户消息、assistant 回复、工具调用、工具结果、编辑后的上下文节点拆成独立项目展示。

### AI 辅助上下文编辑

你可以选中噪声较大或已经过期的上下文，让一个编辑模型压缩、重写或清理它。这样可以保留有用意图，同时移除日志、失败尝试和重复信息里的大量冗余。

### 手动上下文控制

不是所有上下文编辑都需要 AI。你也可以手动删除选中的节点，或者查看原始内容。

### Codex CLI 和桌面端

Codex CLI 是主要使用路径。启用后，普通的 `codex` 命令会先启动本地代理和上下文窗口，然后再启动真正的 Codex CLI。

Codex Desktop 也包含适配支持。它可以把 Codex Desktop 的 model provider 配置指向本地代理，让桌面端对话也能使用同一层可编辑上下文。桌面端支持会修改本地 Codex 配置，所以它和 CLI 开关分开控制。

### 透明工作流

代理关闭时，`codex` 会直接透传到官方 Codex CLI。代理开启时，同一个 `codex` 命令会启动本地代理、打开上下文窗口，再进入真正的 Codex CLI。

## 原理

Codex Context Proxy 会运行一个本地的、兼容 Responses API 的代理。

当 Codex 发送请求时，代理会捕获请求体和响应流，并为上下文工作台构建一份规范化 transcript。如果你没有编辑任何内容，请求会被透明转发，Codex 的行为应当和原生使用一致。

当你编辑上下文后，代理会把当前 session 标记为 overridden。下一轮 Codex 请求时，它会从编辑后的 transcript 重新构建 Responses `input`，并移除可能绕过本地编辑的服务端链式上下文引用。

整体流程：

```text
codex
  -> 本地 shim
  -> Codex Context Proxy
  -> 官方 Codex 请求
  -> OpenAI / ChatGPT Codex backend

context window
  -> 可视化 transcript
  -> 编辑选中的节点
  -> 保存编辑后的上下文
  -> 下一轮 Codex 使用编辑后的上下文
```

## 快速开始

下载并运行 Windows 安装包：

```text
Codex Context Proxy Setup 0.1.0.exe
```

安装完成后，重新打开一个终端，然后启用代理：

```powershell
codex ctx proxy on
```

正常使用 Codex：

```powershell
codex
```

随时关闭代理：

```powershell
codex ctx proxy off
```

查看状态：

```powershell
codex ctx proxy status
```

移除 shim：

```powershell
codex ctx proxy uninstall
```

### Codex Desktop

桌面端支持单独控制：

```powershell
codex ctx desktop on
```

查看桌面端代理状态：

```powershell
codex ctx desktop status
```

关闭桌面端代理：

```powershell
codex ctx desktop off
```

桌面端支持比 CLI 支持更实验一些，因为它修改的是本地 Codex 配置，而不只是添加一个命令 shim。

## 开发

安装依赖：

```powershell
npm install
```

运行本地 Codex 流程：

```powershell
npm run codex
```

只运行上下文窗口：

```powershell
npm run window
```

运行类型检查：

```powershell
npm run typecheck
```

构建 Windows 安装包：

```powershell
npm run dist:win
```

安装包会生成在：

```text
release/Codex Context Proxy Setup 0.1.0.exe
```

## 说明

- 这个项目不是 Codex 的替代品。
- 它不需要修改官方 Codex CLI 源码。
- 它是在 Codex 前面加了一层本地、可编辑的上下文层。
- Codex Desktop 支持比 Codex CLI 支持更实验一些。
