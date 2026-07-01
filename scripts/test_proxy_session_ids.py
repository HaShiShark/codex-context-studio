from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.proxy_routes_support import codex_existing_thread_ids, codex_passthrough_reason, session_id_for_request  # noqa: E402


def fallback_session_id(body: dict[str, Any]) -> str:
    digest = hashlib.sha1(json.dumps(body.get("input", []), sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"session-{digest[:16]}"


def request_body(client_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": "hello",
            }
        ]
    }
    if client_metadata is not None:
        body["client_metadata"] = client_metadata
    return body


def test_client_metadata_random_string_does_not_become_session_id() -> None:
    body = request_body({"random_label": "abc"})

    assert session_id_for_request(body, {}) == fallback_session_id(body)


def test_client_metadata_whitelisted_ids_are_used() -> None:
    for key in (
        "session_id",
        "conversation_id",
        "thread_id",
        "codex_session_id",
        "codex_conversation_id",
    ):
        body = request_body({key: f"{key}-abc"})

        assert session_id_for_request(body, {}) == f"{key}-abc"


def test_codex_metadata_prefers_thread_id_for_proxy_session() -> None:
    body = request_body()
    metadata = {
        "session_id": "root-session",
        "thread_id": "forked-thread",
        "request_kind": "turn",
    }

    assert session_id_for_request(body, {"x-codex-turn-metadata": json.dumps(metadata)}) == "forked-thread"


def test_prompt_cache_key_still_precedes_client_metadata() -> None:
    body = request_body({"session_id": "metadata-session"})
    body["prompt_cache_key"] = "prompt-session"

    assert session_id_for_request(body, {}) == "prompt-session"


def test_codex_metadata_uses_explicit_or_session_scoped_ids_only() -> None:
    body = request_body()
    assert (
        session_id_for_request(
            body,
            {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction", "random_label": "abc"})},
        )
        == fallback_session_id(body)
    )
    assert (
        session_id_for_request(body, {"x-codex-turn-metadata": json.dumps({"conversation": {"id": "conv-abc"}})})
        == "conv-abc"
    )
    assert (
        session_id_for_request(body, {"x-codex-turn-metadata": json.dumps({"thread_label": "not-a-session"})})
        == fallback_session_id(body)
    )
    assert (
        session_id_for_request(body, {"x-codex-turn-state": json.dumps({"state": {"session_id": "too-deep"}})})
        == fallback_session_id(body)
    )


def test_codex_passthrough_request_classification() -> None:
    body = request_body()

    assert (
        codex_passthrough_reason(
            {},
            {"client_metadata": {"x-codex-turn-metadata": json.dumps({"request_kind": "prewarm"})}},
        )
        == "prewarm"
    )
    assert codex_passthrough_reason({"x-codex-turn-metadata": json.dumps({"request_kind": "memory"})}, body) == "memory"
    assert codex_passthrough_reason({"x-openai-subagent": "review"}, body) == "subagent"
    assert (
        codex_passthrough_reason(
            {},
            {"client_metadata": {"x-codex-turn-metadata": json.dumps({"request_kind": "turn", "subagent_kind": "review"})}},
        )
        == "subagent"
    )
    assert codex_passthrough_reason({"x-codex-turn-metadata": json.dumps({"request_kind": "turn"})}, body) == ""
    assert codex_passthrough_reason({"x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})}, body) == ""


def test_codex_existing_thread_ids_scans_active_and_archived_rollouts() -> None:
    active_thread_id = "019f1c86-0574-73a0-8f5e-b40cb6184de9"
    archived_thread_id = "019f1c7c-a3ed-7b52-9258-e9f577da3d55"

    with tempfile.TemporaryDirectory() as temp_dir:
        codex_home = Path(temp_dir)
        active_path = codex_home / "sessions" / "2026" / "07" / "01"
        archived_path = codex_home / "archived_sessions" / "2026" / "06" / "30"
        active_path.mkdir(parents=True)
        archived_path.mkdir(parents=True)
        (active_path / f"rollout-2026-07-01T00-00-00-{active_thread_id}.jsonl").write_text("", encoding="utf-8")
        (archived_path / f"rollout-2026-06-30T00-00-00-{archived_thread_id}.jsonl").write_text("", encoding="utf-8")

        thread_ids, scan_info = codex_existing_thread_ids(codex_home)

    assert thread_ids == {active_thread_id, archived_thread_id}
    assert scan_info["scanned_files"] == 2
    assert scan_info["unreadable_files"] == 0


def main() -> None:
    tests = [
        test_client_metadata_random_string_does_not_become_session_id,
        test_client_metadata_whitelisted_ids_are_used,
        test_codex_metadata_prefers_thread_id_for_proxy_session,
        test_prompt_cache_key_still_precedes_client_metadata,
        test_codex_metadata_uses_explicit_or_session_scoped_ids_only,
        test_codex_passthrough_request_classification,
        test_codex_existing_thread_ids_scans_active_and_archived_rollouts,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} proxy session id tests passed")


if __name__ == "__main__":
    main()
