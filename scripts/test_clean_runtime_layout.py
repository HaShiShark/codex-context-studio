from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from backend.proxy_session_storage import ProxySessionStorage
    from backend.proxy_store import DATA_DIR as PROXY_DATA_DIR
    from backend.web_constants import DEFAULT_DATA_DIR as WEB_DATA_DIR
    from simple_agent.config import DEFAULT_DATA_DIR as SETTINGS_DATA_DIR

    expected_data_dir = Path.home() / ".codex-context-studio" / "shared"
    assert WEB_DATA_DIR == expected_data_dir
    assert SETTINGS_DATA_DIR == expected_data_dir
    assert PROXY_DATA_DIR == expected_data_dir

    with tempfile.TemporaryDirectory(prefix="studio-session-restore-") as raw_temp_dir:
        shared_dir = Path(raw_temp_dir) / ".codex-context-studio" / "shared"
        session_id = "restored-session"
        session_dir = shared_dir / "sessions" / session_id
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "id": session_id,
                    "title": "Restored session",
                    "updated_at": "2026-07-14T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        (session_dir / "transcript.json").write_text(
            json.dumps({"version": 2, "nodes": []}),
            encoding="utf-8",
        )
        (session_dir / "cursor.json").write_text(
            json.dumps({"version": 2, "items": []}),
            encoding="utf-8",
        )
        (session_dir / "workbench.jsonl").write_text("", encoding="utf-8")

        storage = ProxySessionStorage(shared_dir)
        active_session_id, sessions = storage.load_all_sessions()
        assert active_session_id == ""
        assert [session.metadata["id"] for session in sessions] == [session_id]
        assert not storage.index_path.exists()

    print("ok - clean runtime layout and session restore tests passed")


if __name__ == "__main__":
    main()
