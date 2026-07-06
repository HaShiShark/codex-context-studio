<p align="center">
  <img src="electron/assets/hash-icon.ico" alt="Codex Context Studio icon" width="128" height="128">
</p>

<h1 align="center">Codex Context Studio</h1>

<p align="center">
  <strong>🧠 Context Control Panel for Codex</strong>
  <br>
  <sub>Make Codex's context visible, locatable, and precisely manageable — like memory.</sub>
</p>

<p align="center">
  <a href="#-core-features">Features</a> ·
  <a href="#-architecture">Architecture</a> ·
  <a href="#-quick-start">Install</a> ·
  <a href="#-faq">FAQ</a>
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">中文</a>
</p>

<p align="center">
  <img alt="Electron" src="https://img.shields.io/badge/electron-37-2f7f8f?style=flat-square&logo=electron&logoColor=white">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/react-19-149eca?style=flat-square&logo=react&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/typescript-5.9-3178c6?style=flat-square&logo=typescript&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-GPL--3.0-555?style=flat-square">
  <img alt="Status" src="https://img.shields.io/badge/status-alpha-ff7a1a?style=flat-square">
</p>


---

## ⚡ In One Line

This project lets you see Codex's context consumption, what's retained after compression, and edit it yourself. It also lets you replace Codex's system prompt to break through Codex's limitations, and replace Codex's compression prompt for better compressed output.

---

## 🎯 Core Features

### 🔬 Context Visualization — See Inside Codex's Brain

> View Codex's context by role, check token usage per turn, and see when prompts are injected.

### ✂️ Context Management — Take Over Codex's Memory

> Chat with a dedicated secondary model to analyze, edit, compress, and manage the main Codex's context. You can plug in a cheap secondary model.

### 📈 Usage Panel — How Much Did This Session Cost?

> View token consumption, cache hit rate, and estimated cost across your Codex session.

### 🔀 Prompt Replacement — Rewrite Codex's Default Prompts

> Replace Codex's system prompt and native compression prompt for jailbreaking and better compression quality.

### 🔌 Local Proxy — Transparent, Non-Invasive

> No Codex source code changes, no official tools replaced. Sits in the middle as a local proxy, compatible with all native features.

---

## 📸 Screenshots

### Context Map & Secondary Model Chat

![Context map and secondary model chat](docs/images/eng/1.png)

### Context Compression Result

![Context compression result](docs/images/eng/2.png)

### Prompt Replacement

![Prompt replacement](docs/images/eng/3.png)

### Usage Panel

![Usage panel](docs/images/eng/4.png)

---

## 🏗️ Architecture

```mermaid
flowchart LR
    CD["Codex CLI / Desktop"]
    PX["Responses Proxy\n:8787"]
    WEB["Web Backend\n:8765"]
    FE["React Workbench"]
    OAI["OpenAI API / Upstream Model"]

    CD -- "POST /v1/responses" --> PX
    PX -- "Rebuilt request" --> OAI
    OAI -- "SSE stream" --> PX
    PX -- "SSE stream" --> CD

    FE -- "HTTP edit/settings" --> WEB
    WEB -- "HTTP proxy control" --> PX
    PX -. "WebSocket realtime events" .-> FE
```

**How It Works:**

Every Codex request follows the same path — there is no "pass-through if unedited" branch:

```mermaid
flowchart LR
    A["Codex sends request"] --> B["cursor diff"] --> C["Update transcript"] --> D["Rebuild input"] --> E["Forward upstream"]
```

- **Transcript** is the single source of truth: what users see in the workbench, edit, and what ultimately gets sent upstream
- **Without edits**, the rebuilt input is naturally equivalent to the original Codex input
- **With edits**, upstream receives the edited transcript + new content from the current turn
- **During compression**, Codex's native compression prompt is replaced with a custom version, and the result is written back to the transcript

---

## 🚀 Quick Start

**1. Install**

Download and run the latest Windows installer from [Releases](https://github.com/nicobailon/codex-context-studio/releases).

**2. Enable CLI Proxy**

```powershell
codex ctx proxy on      # Enable proxy
codex                   # Use Codex normally
codex ctx proxy status  # Check status
codex ctx proxy off     # Disable proxy
```

**3. Desktop Support**

```powershell
codex ctx desktop on      # Enable Desktop mode
codex ctx desktop status  # Check status
codex ctx desktop off     # Disable
```

> [!NOTE]
> Desktop mode modifies local Codex provider configuration. CLI mode only adds a shim and does not affect any config files.

---

## 🛠️ Development

```powershell
# Install dependencies
npm install
npm run setup:python

# Run the full local flow
npm run codex

# Run only the context window
npm run window

# Run checks
npm run typecheck
npm test

# Build Windows installer
npm run dist:win
```

---

## ❓ FAQ

<details>
<summary><strong>Is this a Codex plugin?</strong></summary>

No. Codex Context Studio is a local context layer around Codex, implemented through proxy technology.

</details>

<details>
<summary><strong>Will it break the cache?</strong></summary>

Compression is not a frequent event. After compression, it only recalculates once, which is more cost-efficient than carrying useless context. In practice, cache hit rate only drops by 5–10%.

</details>

<details>
<summary><strong>Why not just rely on auto-compaction?</strong></summary>

Compatible with native compression. The project also helps replace compression prompts — we've built more precise, targeted compression features.

</details>

<details>
<summary><strong>Who is this for?</strong></summary>

People who want better control over context, have their own ideas about compression, or want to modify Codex's prompts.

</details>

---

<p align="center">
  <sub>GPL-3.0 · Made with ❤️ for Codex power users</sub>
</p>
