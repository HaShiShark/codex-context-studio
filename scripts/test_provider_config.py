from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from codex_provider_config import provider_projection

ROOT = Path(__file__).resolve().parents[1]


class ProviderConfigTests(unittest.TestCase):
    def test_native_provider_options_survive_projection(self):
        source = '''
model_provider = 'custom'
[model_providers.custom]
name = 'Custom'
base_url = 'https://provider.test/v1'
env_key = 'TEST_PROVIDER_KEY'
requires_openai_auth = false
supports_websockets = true
supports_standalone_web_search = true
request_max_retries = 7
stream_idle_timeout_ms = 120000
model_catalog_url = 'https://provider.test/catalog'
[model_providers.custom.http_headers]
'x-project' = '测试 project'
[model_providers.custom.env_http_headers]
'x-key' = 'TEST_HEADER_KEY'
[model_providers.custom.query_params]
'api-version' = '2026-09-26'
[unrelated]
name = 'OpenAI'
base_url = 'must-not-leak'
'''
        projected = provider_projection(source)
        options = tomllib.loads("\n".join(projected["provider_options"]))
        self.assertEqual(tomllib.loads("\n".join(projected["provider_cli_options"])), options)
        original = tomllib.loads(source)["model_providers"]["custom"]
        self.assertEqual(projected["base_url"], original["base_url"])
        for key in ("http_headers", "env_http_headers", "query_params", "model_catalog_url", "request_max_retries", "stream_idle_timeout_ms", "env_key", "supports_standalone_web_search"):
            self.assertEqual(options[key], original[key])
        for key in ("name", "base_url", "requires_openai_auth", "supports_websockets", "wire_api"):
            self.assertNotIn(key, options)

    def test_openai_overrides_are_read_and_capability_not_invented_for_custom(self):
        official = provider_projection("[model_providers.openai]\nbase_url='https://official.test/v1'")
        self.assertEqual(official["base_url"], "https://official.test/v1")
        self.assertTrue(tomllib.loads("\n".join(official["provider_options"]))["supports_standalone_web_search"])
        custom = provider_projection("model_provider='custom'\n[model_providers.custom]\nname='Custom'")
        self.assertFalse(tomllib.loads("\n".join(custom["provider_options"]))["supports_standalone_web_search"])

    @unittest.skipUnless(sys.platform == "win32", "Windows launcher integration")
    def test_powershell_launchers_parse_and_native_arguments_roundtrip(self):
        with tempfile.TemporaryDirectory(prefix="studio-provider-test-") as directory:
            cmd_echo = Path(directory) / "echo.cmd"
            cmd_echo.write_text(f'@echo off\n"{sys.executable}" "{Path(__file__).resolve()}" --echo-args %*\n', encoding="utf-8")
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/test_provider_config.ps1"),
                 "-PythonExe", sys.executable, "-CmdEcho", str(cmd_echo), "-FixtureDirectory", directory],
                capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            enabled = tomllib.loads((Path(directory) / "config.toml").read_text(encoding="utf-8-sig"))
            self.assertEqual(enabled["model"], "gpt-fixture")
            self.assertTrue(enabled["features"]["hooks"])
            self.assertEqual(enabled["model_provider"], "codex-context-studio")
            provider = enabled["model_providers"]["codex-context-studio"]
            self.assertEqual(provider["http_headers"]["x-project"], "hello there")
            self.assertEqual(provider["name"], "Codex Context Studio")
            self.assertFalse(provider["supports_websockets"])


if __name__ == "__main__":
    if "--echo-args" in sys.argv:
        print(json.dumps(sys.argv[sys.argv.index("--echo-args") + 1:]))
    else:
        unittest.main()
