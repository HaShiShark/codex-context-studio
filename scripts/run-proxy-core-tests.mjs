import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const venvPython = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
const command = existsSync(venvPython) ? venvPython : process.platform === 'win32' ? 'py' : 'python3';
const pythonPrefix = existsSync(venvPython) ? [] : process.platform === 'win32' ? ['-3'] : [];

const tests = [
  'scripts/test_transcript_codec.py',
  'scripts/test_cursor_delta.py',
  'scripts/test_compact_controller.py',
  'scripts/test_proxy_core.py',
  'scripts/test_context_hook.py',
  'scripts/test_workbench_transcript_commit.py',
  'scripts/test_proxy_session_ids.py',
  'scripts/test_agent_runtime_contract.py',
  'scripts/test_proxy_models.py',
  'scripts/test_proxy_sse.py',
  'scripts/test_proxy_store_core_integration.py',
  'scripts/test_proxy_remote_compact_disabled.py',
  'scripts/test_web_restore_disabled.py',
];

for (const test of tests) {
  const result = spawnSync(command, [...pythonPrefix, test], { stdio: 'inherit', shell: false });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if ((result.status ?? 1) !== 0) {
    process.exit(result.status ?? 1);
  }
}
