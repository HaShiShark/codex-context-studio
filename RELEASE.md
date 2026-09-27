# Codex Context Studio v1.1.4

## 本次更新

- 增加图片等原生接口的统一转发；修正代理擅自请求压缩响应导致原生客户端可能无法解码的问题。
- 完整继承原服务的配置，修复流式中文和表情跨分块时的记录问题。
- 修复取消、重试和迟到响应的状态收尾，保留主副模型互斥锁与上下文核心算法。
- 跟进原生命名工具结果、图片文件引用和 GPT-6 模型候选；修正本地压缩后的文本保留范围。
- 用量面板统一按 GPT-6 Sol 参考价格估算，保留原始 Token 计数和历史记录。
- 发布流程改用当前 Windows 打包入口，安装包内置 Python，无需用户另装运行环境。

图片转发和压缩协商缺陷已通过隔离回归测试；此前反馈的真实图片生成失败仍待用户复测。本版本不宣称所有原生能力已完成端到端验证。

详细记录见 `docs/audits/` 中的兼容性修复、图片解码复查及参考价格更新说明。


这是给官方 Codex 使用的本地代理和上下文窗口，不是 Codex 的替代品。

它提供：
- Codex CLI 代理入口：`http://127.0.0.1:8787/v1`
- 本地上下文窗口：`http://127.0.0.1:8765/react/`
- Electron 桌面窗口：`Codex Context Studio`

## 打包

```powershell
npm install
npm run package:win
```

生成的安装包在：

```text
release/Codex Context Studio Setup 1.1.4.exe
```

安装包会带上 Electron 前端、React 构建产物、`backend/web_server.py`、`backend/proxy_fastapi.py` 以及打包后的 Python exe。用户不需要自己安装 Node 或 Python。

## 用户怎么用

用户安装 `Codex Context Studio Setup 1.1.4.exe` 后，安装器会自动安装 `codex ctx proxy ...` 控制命令，但不会默认打开代理。

安装完成后，重新打开一个终端，然后使用：

```powershell
codex ctx proxy on
```

之后正常运行：

```powershell
codex
```

关闭代理：

```powershell
codex ctx proxy off
```

查看状态：

```powershell
codex ctx proxy status
```

移除这个代理 shim：

```powershell
codex ctx proxy uninstall
```

也就是说，面向用户的主要流程就是：

```text
安装 -> 新开终端 -> codex ctx proxy on -> codex
```

## 如果命令没有生效

通常是因为当前终端还没有刷新 PATH。先关闭终端，重新打开一个终端再试。

如果用户是在安装官方 Codex CLI 之前安装了这个代理，可以在装好官方 Codex CLI 后重新运行安装包，或者执行一次兜底命令：

```powershell
& "$env:LOCALAPPDATA\Programs\Codex Context Studio\resources\app\scripts\codex-ctx-proxy.ps1" install
```

然后重新打开终端，再运行：

```powershell
codex ctx proxy on
```

## Codex Desktop

Codex Desktop 的配置修改比 CLI 更敏感，所以安装器不会自动改 Desktop 配置。需要时手动执行：

```powershell
codex ctx desktop on
```

恢复：

```powershell
codex ctx desktop off
```

查看状态：

```powershell
codex ctx desktop status
```

## 发布给别人

把下面这个文件上传到 GitHub Releases、网盘或下载页即可：

```text
release/Codex Context Studio Setup 1.1.4.exe
```

安装器文件名必须以 `Codex Context Studio` 开头，其他名称不要发布。
