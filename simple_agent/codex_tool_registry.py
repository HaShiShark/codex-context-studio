from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import mimetypes
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


@dataclass(slots=True)
class ToolExecution:
    output_text: str
    display_title: str
    display_detail: str
    display_result: str
    status: str = "completed"


ToolHandler = Callable[[dict[str, Any]], ToolExecution]


@dataclass(frozen=True, slots=True)
class ToolCatalogItem:
    name: str
    label: str
    description: str
    default_enabled: bool = True


CODEX_TOOL_CATALOG: tuple[ToolCatalogItem, ...] = (
    ToolCatalogItem("parallel_tools", "parallel_tools", "Run multiple enabled tools concurrently and return their results together."),
    ToolCatalogItem("shell_command", "Shell 命令", "执行一次性 PowerShell 命令，适合检查环境、运行测试和调试。"),
    ToolCatalogItem("exec_command", "Exec 命令", "启动命令并返回输出；长时间运行的进程会给出 process_id。"),
    ToolCatalogItem("write_stdin", "写入 stdin", "向 exec_command 启动的仍在运行的进程写入输入。"),
    ToolCatalogItem("apply_patch", "Apply Patch", "使用 Codex 风格 patch 修改、创建、删除或移动工作区文件。"),
    ToolCatalogItem("list_dir", "列出目录", "按 Codex list_dir 风格列出目录内容，支持分页和递归深度。"),
    ToolCatalogItem("read_file", "读取文件", "读取工作区中的文本文件。"),
    ToolCatalogItem("view_image", "查看图片", "读取工作区图片并以 data URL 形式返回给模型。"),
    ToolCatalogItem("js_repl", "JS REPL", "在本地 Node.js kernel 中运行 JavaScript 片段。"),
    ToolCatalogItem("js_repl_reset", "重置 JS REPL", "重置本地 JavaScript kernel。"),
    ToolCatalogItem("get_current_time", "当前时间", "获取指定时区的当前时间。"),
)


def normalize_tool_settings(raw_settings: Any) -> list[dict[str, Any]]:
    raw_by_name: dict[str, Any] = {}
    if isinstance(raw_settings, list):
        for item in raw_settings:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                raw_by_name[item["name"]] = item

    normalized: list[dict[str, Any]] = []
    for item in CODEX_TOOL_CATALOG:
        raw = raw_by_name.get(item.name)
        enabled = item.default_enabled
        if isinstance(raw, dict) and "enabled" in raw:
            enabled = bool(raw.get("enabled"))
        normalized.append(
            {
                "name": item.name,
                "label": item.label,
                "description": item.description,
                "enabled": enabled,
            }
        )
    return normalized


def enabled_tool_names(tool_settings: Any) -> set[str]:
    return {
        item["name"]
        for item in normalize_tool_settings(tool_settings)
        if item.get("enabled")
    }


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass(slots=True)
class _ProcessSession:
    process: subprocess.Popen[str]
    command: str
    cwd: Path
    output: list[str]
    lock: threading.Lock


class ToolRegistry:
    def __init__(self, project_root: Path, tool_settings: Any = None) -> None:
        self.project_root = project_root.resolve()
        self._enabled_names = enabled_tool_names(tool_settings)
        self._processes: dict[int, _ProcessSession] = {}
        self._process_counter = 1000
        self._process_lock = threading.Lock()
        self._js_kernel: subprocess.Popen[str] | None = None
        self._js_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._js_lock = threading.Lock()
        all_tools = [
            self._build_parallel_tools_tool(),
            self._build_shell_command_tool(),
            self._build_exec_command_tool(),
            self._build_write_stdin_tool(),
            self._build_apply_patch_tool(),
            self._build_list_dir_tool(),
            self._build_read_file_tool(),
            self._build_view_image_tool(),
            self._build_js_repl_tool(),
            self._build_js_repl_reset_tool(),
            self._build_get_current_time_tool(),
        ]
        self._all_tools = {tool.name: tool for tool in all_tools}
        self._tools = {name: tool for name, tool in self._all_tools.items() if name in self._enabled_names}
        self._legacy_aliases = {"list_project_files": "list_dir", "read_project_file": "read_file"}

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def describe(self) -> str:
        return "\n".join(f"- {tool.name}: {tool.description}" for tool in self._tools.values())

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        canonical_name = self._legacy_aliases.get(name, name)
        tool = self._all_tools.get(canonical_name)
        if tool is None:
            return ToolExecution(json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False), name, "未知工具", "这个工具不存在。", "error")
        if canonical_name not in self._enabled_names:
            return ToolExecution(json.dumps({"error": f"tool disabled: {canonical_name}"}, ensure_ascii=False), self._display_title(canonical_name), "工具已关闭", "这个工具已在设置里关闭。", "error")
        try:
            return tool.handler(arguments)
        except Exception as exc:  # noqa: BLE001
            return ToolExecution(json.dumps({"error": str(exc), "tool": canonical_name}, ensure_ascii=False), self._display_title(canonical_name), "执行失败", str(exc), "error")

    def _build_parallel_tools_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            raw_tool_uses = arguments.get("tool_uses")
            if not isinstance(raw_tool_uses, list) or not raw_tool_uses:
                raise ValueError("tool_uses must be a non-empty array")

            max_calls = 8
            if len(raw_tool_uses) > max_calls:
                raise ValueError(f"parallel_tools supports at most {max_calls} tool calls")

            planned_calls: list[tuple[int, str, dict[str, Any]]] = []
            for index, raw_call in enumerate(raw_tool_uses):
                if not isinstance(raw_call, dict):
                    raise ValueError(f"tool_uses[{index}] must be an object")
                tool_name = str(raw_call.get("name") or raw_call.get("tool") or "").strip()
                if not tool_name:
                    raise ValueError(f"tool_uses[{index}].name is required")
                if self._legacy_aliases.get(tool_name, tool_name) == "parallel_tools":
                    raise ValueError("parallel_tools cannot call itself")
                raw_arguments = raw_call.get("arguments", raw_call.get("parameters", {}))
                if raw_arguments is None:
                    raw_arguments = {}
                if not isinstance(raw_arguments, dict):
                    raise ValueError(f"tool_uses[{index}].arguments must be an object")
                planned_calls.append((index, tool_name, raw_arguments))

            results: list[dict[str, Any] | None] = [None] * len(planned_calls)
            with ThreadPoolExecutor(max_workers=min(len(planned_calls), max_calls)) as executor:
                future_map = {
                    executor.submit(self.execute, tool_name, tool_arguments): (index, tool_name)
                    for index, tool_name, tool_arguments in planned_calls
                }
                for future in as_completed(future_map):
                    index, tool_name = future_map[future]
                    try:
                        execution = future.result()
                        results[index] = {
                            "index": index,
                            "name": tool_name,
                            "status": execution.status,
                            "display_title": execution.display_title,
                            "display_detail": execution.display_detail,
                            "display_result": execution.display_result,
                            "output_text": execution.output_text[:12000],
                        }
                    except Exception as exc:  # noqa: BLE001
                        results[index] = {
                            "index": index,
                            "name": tool_name,
                            "status": "error",
                            "error": str(exc),
                        }

            compact_results = [result for result in results if result is not None]
            has_error = any(result.get("status") == "error" for result in compact_results)
            payload = {"count": len(compact_results), "results": compact_results}
            summary = "\n".join(
                f"{result['index'] + 1}. {result['name']}: {result.get('status', 'completed')}"
                for result in compact_results
            )
            return ToolExecution(
                json.dumps(payload, ensure_ascii=False),
                "parallel_tools",
                f"{len(compact_results)} calls",
                summary or "并行工具调用完成。",
                "error" if has_error else "completed",
            )

        return ToolDefinition(
            "parallel_tools",
            "Run multiple independent enabled tool calls concurrently and return their results together. Each item needs name and arguments. Do not use this for dependent steps, interactive sessions, or nested parallel_tools calls.",
            {
                "type": "object",
                "properties": {
                    "tool_uses": {
                        "type": "array",
                        "description": "Tool calls to run concurrently. Maximum 8.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Enabled tool name to call."},
                                "arguments": {"type": "object", "description": "Arguments for that tool."},
                            },
                            "required": ["name", "arguments"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["tool_uses"],
                "additionalProperties": False,
            },
            handler,
        )

    def _build_shell_command_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            command = str(arguments.get("command") or arguments.get("cmd") or "").strip()
            if not command:
                raise ValueError("command is required")
            cwd = self._resolve_path(str(arguments.get("cwd") or arguments.get("workdir") or "."))
            raw_timeout = arguments.get("timeout_seconds")
            if raw_timeout is None and arguments.get("timeout_ms") is not None:
                raw_timeout = max(1, int(arguments.get("timeout_ms", 20000)) // 1000)
            timeout_seconds = max(1, min(int(raw_timeout or 20), 120))
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            return self._command_execution(command, cwd, completed.stdout or "", completed.stderr or "", completed.returncode)

        return ToolDefinition(
            "shell_command",
            "Runs a PowerShell command on Windows and returns its output. Use for short one-shot commands; prefer exec_command for long-running or interactive work.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "PowerShell command to execute."},
                    "cwd": {"type": "string", "description": "Working directory. Relative paths resolve from the project root; absolute paths are allowed."},
                    "workdir": {"type": "string", "description": "Alias for cwd, for Codex compatibility."},
                    "timeout_seconds": {"type": "integer", "description": "Command timeout in seconds. Defaults to 20, maximum 120."},
                    "timeout_ms": {"type": "integer", "description": "Alias timeout in milliseconds, for Codex compatibility."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler,
        )

    def _build_exec_command_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            command = str(arguments.get("cmd") or arguments.get("command") or "").strip()
            if not command:
                raise ValueError("cmd is required")
            cwd = self._resolve_path(str(arguments.get("workdir") or arguments.get("cwd") or "."))
            yield_time_ms = max(0, min(int(arguments.get("yield_time_ms", 1000)), 10000))
            max_output_chars = max(1000, min(int(arguments.get("max_output_tokens", 6000)) * 4, 40000))
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", command],
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            process_id = self._store_process(process, command, cwd)
            if yield_time_ms:
                time.sleep(yield_time_ms / 1000)
            return self._process_result(process_id, max_output_chars)

        return ToolDefinition(
            "exec_command",
            "Start a local command. If it keeps running, use write_stdin with the returned process_id.",
            {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Raw shell command to run."},
                    "workdir": {"type": "string", "description": "Working directory. Relative paths resolve from the project root; absolute paths are allowed."},
                    "yield_time_ms": {"type": "integer", "description": "How long to wait before returning output."},
                    "max_output_tokens": {"type": "integer", "description": "Approximate output budget."},
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
            handler,
        )

    def _build_write_stdin_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            raw_process_id = arguments.get("process_id", arguments.get("session_id"))
            if raw_process_id is None:
                raise ValueError("process_id is required")
            process_id = int(raw_process_id)
            session = self._processes.get(process_id)
            if session is None:
                raise ValueError(f"unknown process_id: {process_id}")
            if session.process.stdin is None:
                raise RuntimeError("process stdin is closed")
            session.process.stdin.write(str(arguments.get("chars", "")))
            session.process.stdin.flush()
            yield_time_ms = max(0, min(int(arguments.get("yield_time_ms", 250)), 10000))
            if yield_time_ms:
                time.sleep(yield_time_ms / 1000)
            max_output_chars = max(1000, min(int(arguments.get("max_output_tokens", 6000)) * 4, 40000))
            return self._process_result(process_id, max_output_chars)

        return ToolDefinition(
            "write_stdin",
            "Write characters to an existing exec_command process and return recent output.",
            {
                "type": "object",
                "properties": {
                    "process_id": {"type": "integer", "description": "process_id returned by exec_command."},
                    "session_id": {"type": "integer", "description": "Alias for process_id, for Codex compatibility."},
                    "chars": {"type": "string", "description": "Characters to write to stdin."},
                    "yield_time_ms": {"type": "integer", "description": "How long to wait for output after writing."},
                    "max_output_tokens": {"type": "integer", "description": "Approximate output budget."},
                },
                "required": ["chars"],
                "additionalProperties": False,
            },
            handler,
        )

    def _build_apply_patch_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            patch = str(arguments.get("patch") or arguments.get("input") or "")
            if not patch.strip():
                raise ValueError("patch is required")
            changed = self._apply_codex_patch(patch)
            return ToolExecution(json.dumps({"changed_files": changed, "count": len(changed)}, ensure_ascii=False), "Apply Patch", f"{len(changed)} 个文件", "已修改：" + "、".join(changed[:8]) if changed else "没有文件改动。")

        return ToolDefinition(
            "apply_patch",
            "Use the Codex apply_patch format to edit files. The patch must begin with *** Begin Patch and end with *** End Patch; use Add/Delete/Update file headers and relative paths only.",
            {
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "Codex apply_patch payload."},
                    "input": {"type": "string", "description": "Alias for patch, for Codex compatibility."},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler,
        )

    def _build_list_dir_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            target = self._resolve_path(str(arguments.get("dir_path") or arguments.get("relative_path") or "."))
            if not target.is_dir():
                raise NotADirectoryError(f"not a directory: {target}")
            offset = max(1, int(arguments.get("offset", 1)))
            limit = max(1, min(int(arguments.get("limit", arguments.get("max_entries", 25))), 200))
            depth = max(1, min(int(arguments.get("depth", 2)), 5))
            entries = self._walk_dir(target, depth)
            selected = entries[offset - 1 : offset - 1 + limit]
            payload = {"dir_path": self._relative_display(target), "offset": offset, "limit": limit, "depth": depth, "total_count": len(entries), "entries": selected}
            return ToolExecution(json.dumps(payload, ensure_ascii=False), "列出目录", f"{self._relative_display(target)} · {len(selected)}/{len(entries)}", "\n".join(entry["display"] for entry in selected[:40]) or "目录为空。")

        return ToolDefinition(
            "list_dir",
            "Lists entries in a local directory with 1-indexed entry numbers and simple type labels. Accepts dir_path, offset, limit, and depth.",
            {
                "type": "object",
                "properties": {
                    "dir_path": {"type": "string", "description": "Relative or absolute directory path."},
                    "offset": {"type": "integer", "description": "1-indexed entry offset."},
                    "limit": {"type": "integer", "description": "Maximum number of entries to return."},
                    "depth": {"type": "integer", "description": "Recursive depth, default 2."},
                },
                "required": ["dir_path"],
                "additionalProperties": False,
            },
            handler,
        )

    def _build_read_file_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            path_value = arguments.get("path", arguments.get("relative_path"))
            if not path_value:
                raise ValueError("path is required")
            target = self._resolve_path(str(path_value))
            if not target.is_file():
                raise FileNotFoundError(f"file does not exist: {path_value}")
            max_chars = max(200, min(int(arguments.get("max_chars", 12000)), 50000))
            content = target.read_text(encoding="utf-8", errors="replace")
            trimmed = content[:max_chars]
            preview = " ".join(trimmed.split())
            if len(preview) > 220:
                preview = f"{preview[:217]}..."
            return ToolExecution(json.dumps({"path": self._relative_display(target), "truncated": len(content) > max_chars, "content": trimmed}, ensure_ascii=False), "读取文件", self._relative_display(target), preview or "文件为空。")

        return ToolDefinition(
            "read_file",
            "Read a UTF-8 text file from the local filesystem.",
            {"type": "object", "properties": {"path": {"type": "string", "description": "Relative or absolute file path."}, "max_chars": {"type": "integer", "description": "Maximum number of characters to return."}}, "required": ["path"], "additionalProperties": False},
            handler,
        )

    def _build_view_image_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            path_text = str(arguments.get("path") or "").strip()
            if path_text.startswith("<<") and path_text.endswith(">>"):
                raise ValueError(
                    f"{path_text} is a placeholder, not a real image path. "
                    "Use the exact 'Local path for tools' shown with the uploaded attachment."
                )
            target = self._resolve_path(path_text)
            if not target.is_file():
                raise FileNotFoundError(f"image does not exist: {target}")
            detail = arguments.get("detail")
            if detail not in (None, "", "original"):
                raise ValueError("detail only supports original")
            raw = target.read_bytes()
            if len(raw) > 6 * 1024 * 1024:
                raise ValueError("image is too large for tool output")
            mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            data_url = f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"
            payload = {"path": self._relative_display(target), "mime_type": mime_type, "size_bytes": len(raw), "detail": "original" if detail == "original" else "default", "data_url": data_url}
            return ToolExecution(json.dumps(payload, ensure_ascii=False), "查看图片", self._relative_display(target), f"{mime_type} · {len(raw)} bytes")

        return ToolDefinition(
            "view_image",
            "View a local image from the filesystem. Use only with a real local image path; detail may be omitted or set to original.",
            {"type": "object", "properties": {"path": {"type": "string", "description": "Relative or absolute image path."}, "detail": {"type": "string", "description": "Only original is supported; omit for default."}}, "required": ["path"], "additionalProperties": False},
            handler,
        )

    def _build_js_repl_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            code = str(arguments.get("code") or arguments.get("javascript") or "")
            if not code.strip():
                raise ValueError("code is required")
            timeout_ms = max(100, min(int(arguments.get("timeout_ms", 5000)), 30000))
            result = self._run_js_repl(code, timeout_ms)
            output = "\n".join(str(item) for item in result.get("logs", []))
            if result.get("result") is not None:
                output = f"{output}\n{json.dumps(result.get('result'), ensure_ascii=False)}".strip()
            if result.get("error"):
                output = str(result.get("error"))
            return ToolExecution(json.dumps(result, ensure_ascii=False), "JS REPL", f"{timeout_ms} ms", output[:1200] if output else "执行完成，无输出。", "completed" if result.get("ok") else "error")

        return ToolDefinition(
            "js_repl",
            "Run JavaScript in a persistent local Node.js kernel with top-level await semantics. Use return for a final value; globalThis values persist until js_repl_reset.",
            {"type": "object", "properties": {"code": {"type": "string", "description": "JavaScript source. Use return to emit a final value."}, "timeout_ms": {"type": "integer", "description": "Execution timeout in milliseconds."}}, "required": ["code"], "additionalProperties": False},
            handler,
        )

    def _build_js_repl_reset_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            del arguments
            self._reset_js_kernel()
            return ToolExecution(json.dumps({"reset": True}, ensure_ascii=False), "重置 JS REPL", "kernel reset", "JavaScript kernel 已重置。")

        return ToolDefinition("js_repl_reset", "Reset the persistent JavaScript kernel used by js_repl.", {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, handler)

    def _build_get_current_time_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            timezone_name = str(arguments.get("timezone", "Asia/Shanghai"))
            now = datetime.now(self._resolve_timezone(timezone_name))
            payload = {"timezone": timezone_name, "iso_time": now.isoformat(), "friendly_time": now.strftime("%Y-%m-%d %H:%M:%S")}
            return ToolExecution(json.dumps(payload, ensure_ascii=False), "获取当前时间", f"时区：{timezone_name}", payload["friendly_time"])

        return ToolDefinition(
            "get_current_time",
            "获取指定时区的当前时间。",
            {"type": "object", "properties": {"timezone": {"type": "string", "description": "IANA 时区，例如 Asia/Shanghai 或 America/New_York。"}}, "required": [], "additionalProperties": False},
            handler,
        )

    def _command_execution(self, command: str, cwd: Path, stdout: str, stderr: str, exit_code: int) -> ToolExecution:
        payload = {"cwd": self._relative_display(cwd), "exit_code": exit_code, "stdout": stdout[:12000], "stderr": stderr[:12000]}
        parts = []
        if stdout.strip():
            parts.append(stdout.strip())
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")
        result = "\n\n".join(parts) or f"命令已执行，退出码 {exit_code}"
        return ToolExecution(json.dumps(payload, ensure_ascii=False), "执行本地命令", command, result[:1200], "completed" if exit_code == 0 else "error")

    def _store_process(self, process: subprocess.Popen[str], command: str, cwd: Path) -> int:
        with self._process_lock:
            self._process_counter += 1
            process_id = self._process_counter
            session = _ProcessSession(process, command, cwd, [], threading.Lock())
            self._processes[process_id] = session

        def reader() -> None:
            if process.stdout is None:
                return
            for chunk in iter(process.stdout.readline, ""):
                if not chunk:
                    break
                with session.lock:
                    session.output.append(chunk)

        threading.Thread(target=reader, daemon=True).start()
        return process_id

    def _process_result(self, process_id: int, max_output_chars: int) -> ToolExecution:
        session = self._processes.get(process_id)
        if session is None:
            raise ValueError(f"unknown process_id: {process_id}")
        exit_code = session.process.poll()
        with session.lock:
            output = "".join(session.output)
            session.output.clear()
        if len(output) > max_output_chars:
            output = output[-max_output_chars:]
        payload: dict[str, Any] = {"process_id": process_id, "cwd": self._relative_display(session.cwd), "output": output}
        if exit_code is None:
            payload["running"] = True
            result = output or f"进程仍在运行，process_id={process_id}"
            status = "completed"
        else:
            payload.update({"running": False, "exit_code": exit_code})
            result = output or f"进程已退出，退出码 {exit_code}"
            status = "completed" if exit_code == 0 else "error"
            self._processes.pop(process_id, None)
        return ToolExecution(json.dumps(payload, ensure_ascii=False), "Exec 命令", session.command, result[:1200], status)

    def _walk_dir(self, root: Path, depth: int) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []

        def visit(path: Path, level: int) -> None:
            if level > depth:
                return
            for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                entries.append({"path": self._relative_display(child), "name": child.name, "type": "directory" if child.is_dir() else "file", "display": f"{'  ' * (level - 1)}{'[D]' if child.is_dir() else '[F]'} {child.name}"})
                if child.is_dir():
                    visit(child, level + 1)

        visit(root, 1)
        return entries

    def _apply_codex_patch(self, patch: str) -> list[str]:
        lines = patch.replace("\r\n", "\n").split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise ValueError("patch must start with *** Begin Patch and end with *** End Patch")
        changed: list[str] = []
        index = 1
        while index < len(lines) - 1:
            line = lines[index]
            if line.startswith("*** Add File: "):
                target = self._resolve_patch_path(line.removeprefix("*** Add File: ").strip())
                index += 1
                content_lines = []
                while index < len(lines) - 1 and not lines[index].startswith("*** "):
                    if not lines[index].startswith("+"):
                        raise ValueError("add file lines must start with +")
                    content_lines.append(lines[index][1:])
                    index += 1
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(content_lines) + ("\n" if content_lines else ""), encoding="utf-8")
                changed.append(self._relative_display(target))
                continue
            if line.startswith("*** Delete File: "):
                target = self._resolve_patch_path(line.removeprefix("*** Delete File: ").strip())
                if not target.is_file():
                    raise FileNotFoundError(f"delete target is not a file: {target}")
                target.unlink()
                changed.append(self._relative_display(target))
                index += 1
                continue
            if line.startswith("*** Update File: "):
                target = self._resolve_patch_path(line.removeprefix("*** Update File: ").strip())
                index += 1
                move_to = None
                if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
                    move_to = self._resolve_patch_path(lines[index].removeprefix("*** Move to: ").strip())
                    index += 1
                hunks: list[list[str]] = []
                while index < len(lines) - 1 and not lines[index].startswith("*** "):
                    if not lines[index].startswith("@@"):
                        raise ValueError("update hunk must start with @@")
                    index += 1
                    hunk: list[str] = []
                    while index < len(lines) - 1:
                        if lines[index] == "*** End of File":
                            index += 1
                            break
                        if lines[index].startswith("@@") or lines[index].startswith("*** "):
                            break
                        if not lines[index].startswith((" ", "-", "+")):
                            raise ValueError(f"invalid hunk line: {lines[index]}")
                        hunk.append(lines[index])
                        index += 1
                    hunks.append(hunk)
                self._apply_update_hunks(target, hunks, move_to)
                changed.append(self._relative_display(move_to or target))
                continue
            raise ValueError(f"unsupported patch header: {line}")
        return changed

    def _apply_update_hunks(self, target: Path, hunks: list[list[str]], move_to: Path | None) -> None:
        if not target.is_file():
            raise FileNotFoundError(f"update target is not a file: {target}")
        content = target.read_text(encoding="utf-8", errors="replace")
        had_trailing_newline = content.endswith("\n")
        content_lines = content.splitlines()
        cursor = 0
        for hunk in hunks:
            old_block = [line[1:] for line in hunk if line.startswith((" ", "-"))]
            new_block = [line[1:] for line in hunk if line.startswith((" ", "+"))]
            match_at = self._find_block(content_lines, old_block, cursor)
            if match_at < 0:
                raise ValueError("patch context was not found")
            content_lines[match_at : match_at + len(old_block)] = new_block
            cursor = match_at + len(new_block)
        next_content = "\n".join(content_lines)
        if had_trailing_newline or content_lines:
            next_content += "\n"
        destination = move_to or target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(next_content, encoding="utf-8")
        if move_to is not None and move_to != target:
            target.unlink()

    @staticmethod
    def _find_block(lines: list[str], block: list[str], start: int) -> int:
        if not block:
            return start
        for index in range(start, len(lines) - len(block) + 1):
            if lines[index : index + len(block)] == block:
                return index
        return -1

    def _run_js_repl(self, code: str, timeout_ms: int) -> dict[str, Any]:
        with self._js_lock:
            kernel = self._ensure_js_kernel()
            request_id = uuid.uuid4().hex
            if kernel.stdin is None:
                raise RuntimeError("js_repl stdin is unavailable")
            kernel.stdin.write(json.dumps({"id": request_id, "code": code, "timeout_ms": timeout_ms}) + "\n")
            kernel.stdin.flush()
            deadline = time.time() + (timeout_ms / 1000) + 2
            while time.time() < deadline:
                try:
                    response = self._js_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if response.get("id") == request_id:
                    return response
            self._reset_js_kernel()
            return {"id": request_id, "ok": False, "error": "js_repl execution timed out; kernel reset"}

    def _ensure_js_kernel(self) -> subprocess.Popen[str]:
        if self._js_kernel is not None and self._js_kernel.poll() is None:
            return self._js_kernel
        script = r"""
const readline = require('readline');
const vm = require('vm');
const util = require('util');
const sandbox = { setTimeout, clearTimeout, setInterval, clearInterval, Buffer, URL, URLSearchParams, require };
sandbox.globalThis = sandbox;
const context = vm.createContext(sandbox);
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  let req;
  try { req = JSON.parse(line); } catch (error) { process.stdout.write(JSON.stringify({ ok: false, error: String(error) }) + '\n'); return; }
  const logs = [];
  context.console = { log: (...args) => logs.push(args.map((arg) => util.inspect(arg, { depth: 4 })).join(' ')), error: (...args) => logs.push(args.map((arg) => util.inspect(arg, { depth: 4 })).join(' ')) };
  try {
    const result = await vm.runInContext(`(async () => {\n${req.code}\n})()`, context, { timeout: req.timeout_ms || 5000 });
    process.stdout.write(JSON.stringify({ id: req.id, ok: true, result, logs }) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ id: req.id, ok: false, error: error && error.stack ? error.stack : String(error), logs }) + '\n');
  }
});
"""
        self._js_kernel = subprocess.Popen(["node", "-e", script], cwd=self.project_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)

        def reader() -> None:
            if self._js_kernel is None or self._js_kernel.stdout is None:
                return
            for line in iter(self._js_kernel.stdout.readline, ""):
                try:
                    self._js_queue.put(json.loads(line))
                except json.JSONDecodeError:
                    self._js_queue.put({"ok": False, "error": line.strip()})

        threading.Thread(target=reader, daemon=True).start()
        return self._js_kernel

    def _reset_js_kernel(self) -> None:
        if self._js_kernel is not None and self._js_kernel.poll() is None:
            self._js_kernel.kill()
        self._js_kernel = None
        while not self._js_queue.empty():
            try:
                self._js_queue.get_nowait()
            except queue.Empty:
                break

    def _resolve_path(self, path_text: str) -> Path:
        raw_path = Path(path_text or ".").expanduser()
        target = raw_path if raw_path.is_absolute() else self.project_root / raw_path
        return target.resolve()

    def _resolve_patch_path(self, path_text: str) -> Path:
        if not path_text:
            raise ValueError("patch paths must not be empty")
        return self._resolve_path(path_text)

    def _relative_display(self, target: Path) -> str:
        try:
            return target.relative_to(self.project_root).as_posix() or "."
        except ValueError:
            return str(target)

    @staticmethod
    def _display_title(name: str) -> str:
        labels = {item.name: item.label for item in CODEX_TOOL_CATALOG}
        return labels.get(name, name.replace("_", " ").strip() or "工具调用")

    def _resolve_timezone(self, timezone_name: str) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            pass
        normalized = timezone_name.strip()
        alias_map = {
            "UTC": timezone.utc,
            "Etc/UTC": timezone.utc,
            "GMT": timezone.utc,
            "Etc/GMT": timezone.utc,
            "Asia/Shanghai": timezone(timedelta(hours=8), name="Asia/Shanghai"),
            "Asia/Chongqing": timezone(timedelta(hours=8), name="Asia/Chongqing"),
            "Asia/Beijing": timezone(timedelta(hours=8), name="Asia/Beijing"),
            "America/New_York": timezone(timedelta(hours=-5), name="America/New_York"),
            "America/Los_Angeles": timezone(timedelta(hours=-8), name="America/Los_Angeles"),
            "Europe/London": timezone(timedelta(hours=0), name="Europe/London"),
            "Europe/Paris": timezone(timedelta(hours=1), name="Europe/Paris"),
            "Asia/Tokyo": timezone(timedelta(hours=9), name="Asia/Tokyo"),
        }
        if normalized in alias_map:
            return alias_map[normalized]
        if normalized.startswith("UTC") and len(normalized) > 3:
            return self._parse_utc_offset(normalized)
        raise ValueError(f"unsupported timezone: {timezone_name}")

    @staticmethod
    def _parse_utc_offset(value: str) -> timezone:
        sign = value[3]
        if sign not in {"+", "-"}:
            raise ValueError(f"unsupported timezone: {value}")
        hour_text, _, minute_text = value[4:].partition(":")
        delta = timedelta(hours=int(hour_text), minutes=int(minute_text or "0"))
        if sign == "-":
            delta = -delta
        return timezone(delta, name=value)
