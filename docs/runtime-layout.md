# Codex Context Studio 运行目录与开发隔离

Codex Context Studio 只有一个产品名称，但有两套独立注册：`production` 对应安装后的 EXE，`development` 对应源码项目。两套通常只启用一套，不为同时运行设计额外的端口协调或自动接管逻辑。

两套写入 Codex 配置时使用同一个 provider ID：`codex-context-studio`。`dev` 只用于选择源码注册和隔离 shim、状态、日志与缓存，不进入 provider 名称。

## 控制命令

生产版命令不带后缀：

```powershell
codex ctx proxy on
codex ctx proxy off
codex ctx proxy status
codex ctx proxy uninstall

codex ctx desktop on
codex ctx desktop off
codex ctx desktop status
codex ctx desktop uninstall
```

开发版的每条控制命令都以 `dev` 结尾：

```powershell
npm run ctx:install:dev

codex ctx proxy on dev
codex ctx proxy off dev
codex ctx proxy status dev
codex ctx proxy uninstall dev

codex ctx desktop on dev
codex ctx desktop off dev
codex ctx desktop status dev
codex ctx desktop uninstall dev
```

`uninstall` 只删除所选 profile 的注册、状态和 shim。另一套仍存在时，公共 `codex` 路由和 PATH 保持不变；删除最后一套后才删除公共路由并移除 PATH 项。

## 目录结构

```text
~/.codex-context-studio/
├─ bin/                         公共 codex.cmd / codex.ps1 路由
├─ registrations/
│  ├─ production.json          EXE 安装根目录注册
│  └─ development.json         源码项目根目录注册
├─ production/
│  ├─ bin/                     生产 hook / notify shim
│  ├─ cache/electron/          生产 Electron 缓存
│  ├─ logs/                    生产日志
│  └─ state/                   生产开关与进程状态
├─ development/
│  ├─ bin/                     开发 hook / notify shim（文件名带 -dev）
│  ├─ cache/electron/          开发 Electron 缓存
│  ├─ logs/                    开发日志
│  └─ state/                   开发开关与进程状态
└─ shared/                     两套共用的会话、索引和设置
```

Windows 的 PATH 只能优先解析一份同名 `codex.cmd`，所以 `bin/` 是中立路由；它根据控制命令末尾是否有 `dev` 选择注册，不包含任一版本的业务实现。production 和 development 的实际 hook、notify、状态、缓存均可独立删除。

## 项目目录之外的写入

应用主动管理的范围只有：

- EXE 的安装根目录，默认由安装器放在 `%LOCALAPPDATA%\Programs\Codex Context Studio`，用户也可在安装时修改。
- 开发源码根目录，也就是运行 `npm run ctx:install:dev` 的仓库。
- `%USERPROFILE%\.codex-context-studio`，保存上述注册、shim、状态、缓存、日志和共享业务数据。
- `%USERPROFILE%\.codex\config.toml`，只在启用 Desktop/Proxy 时写入当前 provider、hook 和 notify 配置，执行对应 `off` 时恢复。
- 当前用户的 `PATH` 注册表项，用于加入或移除 `%USERPROFILE%\.codex-context-studio\bin`。
- 安装器创建的 Windows 标准快捷方式和卸载注册表项。

## 全新重建与会话恢复

需要完全清空并重建时，先退出 Codex Context Studio，再删除 `%USERPROFILE%\.codex-context-studio`。重新注册开发版：

```powershell
cd <源码项目目录>
npm run ctx:install:dev
codex ctx desktop on dev
```

如果需要保留历史会话，只复制各个 session 子目录，目标位置固定为：

```text
%USERPROFILE%\.codex-context-studio\shared\sessions\<session-id>\
```

每个有效 session 至少应包含 `session.json`；通常还包括 `transcript.json`、`cursor.json`、`workbench.jsonl` 和可选的 `attachments/`。不需要复制旧 `index.json`，应用重启后会扫描 `session.json` 并自动重建索引。旧日志、缓存、状态、shim 和设置文件都不应粘回。

## 运行来源约束

- development 强制使用源码 Python、Vite 前端和当前仓库代码。
- production 强制使用安装包内置 Python、已构建前端和 EXE；如果 EXE 不存在会直接报错，不回退到源码。
- 两套共享 `shared/` 数据和同一份 Codex 配置。按设计应先执行当前 profile 的 `off`，再启用另一套。
