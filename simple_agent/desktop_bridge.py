from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from simple_agent.agent import SimpleAgent, ToolEvent
from simple_agent.config import Settings, load_settings


def sanitize_text(value: str) -> str:
    # 彻底过滤掉导致 surrogates 错误的“半个”字符（U+D800 到 U+DFFF）
    # 这些字符在 UTF-8 编码时是非法的，必须替换掉
    return "".join(c if not (0xD800 <= ord(c) <= 0xDFFF) else "\ufffd" for c in value)


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {
            sanitize_json_value(key): sanitize_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_value(item) for item in value]
    return value


def emit_response(request_id: str, ok: bool, *, data: dict[str, Any] | None = None, error: str | None = None) -> None:
    payload = {
        "id": request_id,
        "ok": ok,
    }
    if data is not None:
        payload["data"] = data
    if error is not None:
        payload["error"] = error

    safe_payload = sanitize_json_value(payload)
    # 尝试序列化，如果还是失败，则进行最后的兜底
    try:
        json_str = json.dumps(safe_payload, ensure_ascii=False)
    except Exception:
        # 最后的兜底：只保留基本信息，剔除可能出错的 data
        json_str = json.dumps({"id": request_id, "ok": False, "error": "Internal JSON serialization error"}, ensure_ascii=False)
        
    sys.stdout.write(json_str + "\n")
    sys.stdout.flush()


def serialize_tool_event(event: ToolEvent) -> dict[str, Any]:
    return {
        "name": event.name,
        "arguments": event.arguments,
        "output_preview": event.output_preview,
    }


def project_name_from_path(project_root: Path) -> str:
    return project_root.name or str(project_root)


def build_agent(settings: Settings) -> SimpleAgent:
    return SimpleAgent(settings)


def get_session_id(payload: dict[str, Any]) -> str:
    session_id = str(payload.get("session_id", "default")).strip()
    if not session_id:
        raise ValueError("session_id is required")
    return session_id


def get_agent_for_session(agents: dict[str, SimpleAgent], settings: Settings, session_id: str) -> SimpleAgent:
    agent = agents.get(session_id)
    if agent is None:
        agent = build_agent(settings)
        agents[session_id] = agent
    return agent


def handle_command(settings: Settings, agents: dict[str, SimpleAgent], command: str, payload: dict[str, Any]) -> dict[str, Any]:
    if command == "init":
        agent = build_agent(settings)
        return {
            "model": settings.model,
            "project_root": str(settings.project_root),
            "project_name": project_name_from_path(settings.project_root),
            "tools": agent.tools.schemas,
        }

    if command == "send_message":
        session_id = get_session_id(payload)
        agent = get_agent_for_session(agents, settings, session_id)
        message = str(payload.get("message", "")).strip()
        if not message:
            raise ValueError("message is required")

        answer, tool_events = agent.run_turn(message)
        return {
            "answer": answer,
            "tool_events": [serialize_tool_event(event) for event in tool_events],
        }

    if command == "reset":
        session_id = get_session_id(payload)
        agent = get_agent_for_session(agents, settings, session_id)
        agent.reset()
        return {"ok": True}

    if command == "describe_tools":
        agent = build_agent(settings)
        return {"tools": agent.describe_tools()}

    raise ValueError(f"unknown command: {command}")


def main() -> None:
    # 强制让 stdout 使用 UTF-8 编码，并自动替换掉无法编码的坏字符
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    
    load_dotenv(REPO_ROOT / ".env")
    settings = load_settings()
    agents: dict[str, SimpleAgent] = {}

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        request_id = "unknown"
        try:
            request = json.loads(line)
            request_id = str(request.get("id", "unknown"))
            command = str(request.get("command", "")).strip()
            payload = request.get("payload") or {}
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")

            data = handle_command(settings, agents, command, payload)
            emit_response(request_id, True, data=data)
        except Exception as exc:
            error_message = str(exc)
            if not error_message:
                error_message = traceback.format_exc(limit=1).strip()
            emit_response(request_id, False, error=error_message)


if __name__ == "__main__":
    main()
