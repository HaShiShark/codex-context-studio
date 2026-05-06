from __future__ import annotations

import json
import subprocess
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


class ToolRegistry:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._tools = {
            tool.name: tool
            for tool in [
                self._build_get_current_time_tool(),
                self._build_list_project_files_tool(),
                self._build_read_project_file_tool(),
                self._build_shell_command_tool(),
            ]
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def describe(self) -> str:
        return "\n".join(f"- {tool.name}: {tool.description}" for tool in self._tools.values())

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecution(
                output_text=json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False),
                display_title=name,
                display_detail="未知工具",
                display_result="这个工具不存在。",
                status="error",
            )

        try:
            return tool.handler(arguments)
        except Exception as exc:  # noqa: BLE001
            return ToolExecution(
                output_text=json.dumps(
                    {
                        "error": str(exc),
                        "tool": name,
                    },
                    ensure_ascii=False,
                ),
                display_title=self._display_title(name),
                display_detail="执行失败",
                display_result=str(exc),
                status="error",
            )

    def _build_get_current_time_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            timezone_name = str(arguments.get("timezone", "Asia/Shanghai"))
            tz = self._resolve_timezone(timezone_name)
            now = datetime.now(tz)
            payload = {
                "timezone": timezone_name,
                "iso_time": now.isoformat(),
                "friendly_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
            return ToolExecution(
                output_text=json.dumps(payload, ensure_ascii=False),
                display_title="获取当前时间",
                display_detail=f"时区：{timezone_name}",
                display_result=payload["friendly_time"],
            )

        return ToolDefinition(
            name="get_current_time",
            description="获取指定时区的当前时间。",
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA 时区，例如 Asia/Shanghai 或 America/New_York。",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=handler,
        )

    def _build_list_project_files_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            relative_path = str(arguments.get("relative_path", "."))
            max_entries = int(arguments.get("max_entries", 20))
            max_entries = max(1, min(max_entries, 100))

            target = self._resolve_path(relative_path)
            if not target.exists():
                raise FileNotFoundError(f"path does not exist: {relative_path}")
            if not target.is_dir():
                raise NotADirectoryError(f"not a directory: {relative_path}")

            children = sorted(
                target.iterdir(),
                key=lambda path: (not path.is_dir(), path.name.lower()),
            )

            entries = []
            for child in children[:max_entries]:
                entries.append(
                    {
                        "name": child.name,
                        "type": "directory" if child.is_dir() else "file",
                    }
                )

            relative_display = str(target.relative_to(self.project_root) or ".")
            preview_names = "、".join(item["name"] for item in entries[:6])
            result_text = f"列出了 {relative_display} 下前 {len(entries)} 项"
            if preview_names:
                result_text = f"{result_text}：{preview_names}"

            return ToolExecution(
                output_text=json.dumps(
                    {
                        "relative_path": relative_display,
                        "count": len(entries),
                        "entries": entries,
                    },
                    ensure_ascii=False,
                ),
                display_title="列出项目文件",
                display_detail=f"目录：{relative_display}",
                display_result=result_text,
            )

        return ToolDefinition(
            name="list_project_files",
            description="列出本地项目工作区中的文件和文件夹。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "相对于项目根目录的文件夹路径。",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "最多返回多少条目录项。",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=handler,
        )

    def _build_read_project_file_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            relative_path = arguments.get("relative_path")
            if not relative_path:
                raise ValueError("relative_path is required")

            max_chars = int(arguments.get("max_chars", 4000))
            max_chars = max(200, min(max_chars, 12000))

            target = self._resolve_path(str(relative_path))
            if not target.exists():
                raise FileNotFoundError(f"file does not exist: {relative_path}")
            if not target.is_file():
                raise IsADirectoryError(f"not a file: {relative_path}")

            content = target.read_text(encoding="utf-8", errors="replace")
            truncated = len(content) > max_chars
            trimmed_content = content[:max_chars] if truncated else content
            preview = " ".join(trimmed_content.split())
            if len(preview) > 220:
                preview = f"{preview[:217]}..."

            return ToolExecution(
                output_text=json.dumps(
                    {
                        "relative_path": str(target.relative_to(self.project_root)),
                        "truncated": truncated,
                        "content": trimmed_content,
                    },
                    ensure_ascii=False,
                ),
                display_title="读取文件",
                display_detail=f"文件：{target.relative_to(self.project_root).as_posix()}",
                display_result=preview or "文件为空。",
            )

        return ToolDefinition(
            name="read_project_file",
            description="读取本地项目工作区中的文本文件。",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "相对于项目根目录的文件路径。",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回多少个字符。",
                    },
                },
                "required": ["relative_path"],
                "additionalProperties": False,
            },
            handler=handler,
        )

    def _build_shell_command_tool(self) -> ToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            command = str(arguments.get("command") or "").strip()
            if not command:
                raise ValueError("command is required")

            relative_cwd = str(arguments.get("cwd", ".") or ".")
            timeout_seconds = int(arguments.get("timeout_seconds", 20))
            timeout_seconds = max(1, min(timeout_seconds, 120))
            cwd = self._resolve_path(relative_cwd)
            if not cwd.is_dir():
                raise NotADirectoryError(f"not a directory: {relative_cwd}")

            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        command,
                    ],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                )
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
                payload = {
                    "cwd": str(cwd.relative_to(self.project_root) or "."),
                    "exit_code": completed.returncode,
                    "stdout": stdout[:12000],
                    "stderr": stderr[:12000],
                }
                result_parts = []
                if stdout.strip():
                    result_parts.append(stdout.strip())
                if stderr.strip():
                    result_parts.append(f"[stderr]\n{stderr.strip()}")
                if not result_parts:
                    result_parts.append(f"命令已执行，退出码 {completed.returncode}")
                result_text = "\n\n".join(result_parts)
                if len(result_text) > 1200:
                    result_text = f"{result_text[:1197]}..."

                return ToolExecution(
                    output_text=json.dumps(payload, ensure_ascii=False),
                    display_title="执行本地命令",
                    display_detail=command,
                    display_result=result_text,
                    status="completed" if completed.returncode == 0 else "error",
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                payload = {
                    "cwd": str(cwd.relative_to(self.project_root) or "."),
                    "timeout_seconds": timeout_seconds,
                    "stdout": stdout[:12000],
                    "stderr": stderr[:12000],
                    "timed_out": True,
                }
                result_text = (stdout or stderr or f"命令超过 {timeout_seconds} 秒后被中断。").strip()
                if len(result_text) > 1200:
                    result_text = f"{result_text[:1197]}..."

                return ToolExecution(
                    output_text=json.dumps(payload, ensure_ascii=False),
                    display_title="执行本地命令",
                    display_detail=command,
                    display_result=result_text,
                    status="error",
                )

        return ToolDefinition(
            name="shell_command",
            description="在用户本地项目目录中执行 PowerShell 命令，用于检查环境、运行构建、查看状态或执行调试命令。",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 PowerShell 命令。",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "相对于项目根目录的执行目录，默认是根目录。",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "命令超时时间，默认 20 秒，最大 120 秒。",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=handler,
        )

    def _resolve_path(self, relative_path: str) -> Path:
        target = (self.project_root / relative_path).resolve()
        target.relative_to(self.project_root)
        return target

    @staticmethod
    def _display_title(name: str) -> str:
        return name.replace("_", " ").replace(".", " ").strip() or "工具调用"

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
            "Etc/GMT-8": timezone(timedelta(hours=8), name="Etc/GMT-8"),
            "Etc/GMT+8": timezone(timedelta(hours=-8), name="Etc/GMT+8"),
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

        offset_text = value[4:]
        if ":" in offset_text:
            hour_text, minute_text = offset_text.split(":", 1)
        else:
            hour_text, minute_text = offset_text, "0"

        hours = int(hour_text)
        minutes = int(minute_text)
        delta = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            delta = -delta

        return timezone(delta, name=value)


from simple_agent.codex_tool_registry import (  # noqa: E402,F401
    CODEX_TOOL_CATALOG,
    ToolCatalogItem,
    ToolDefinition,
    ToolExecution,
    ToolRegistry,
    enabled_tool_names,
    normalize_tool_settings,
)
