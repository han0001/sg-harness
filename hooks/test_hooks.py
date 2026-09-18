import json
import re
import subprocess
from pathlib import Path

import pytest


HOOKS_FILE = Path(__file__).with_name("hooks.json")
PLUGIN_ROOT = HOOKS_FILE.parent.parent


def _run_bash_hook(payload: dict) -> subprocess.CompletedProcess:
    hooks = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    return subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


@pytest.fixture(params=["claude", "codex"])
def bash_payload(request):
    common = {
        "session_id": "session-1",
        "cwd": "/tmp/project",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git reset --hard HEAD~1"},
    }
    if request.param == "codex":
        common.update({"turn_id": "turn-1", "tool_use_id": "call-1"})
    return common


def test_dangerous_bash_commands_are_blocked_for_both_hosts(bash_payload):
    result = _run_bash_hook(bash_payload)

    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


@pytest.mark.parametrize("whitespace", ["\t", "\n"])
def test_json_escaped_whitespace_cannot_bypass_the_hook(bash_payload, whitespace):
    bash_payload["tool_input"]["command"] = f"rm{whitespace}-rf /tmp/example"

    result = _run_bash_hook(bash_payload)

    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


def test_dangerous_text_outside_tool_input_is_ignored(bash_payload):
    bash_payload["tool_input"]["command"] = "python3 -m pytest -q"
    bash_payload["prompt"] = "Explain why rm -rf is dangerous"

    result = _run_bash_hook(bash_payload)

    assert result.returncode == 0
    assert result.stderr == ""


def test_safe_bash_command_is_allowed():
    payload = {
        "session_id": "session-1",
        "cwd": "/tmp/project",
        "hook_event_name": "PreToolUse",
        "turn_id": "turn-1",
        "tool_name": "Bash",
        "tool_use_id": "call-1",
        "tool_input": {"command": "python3 -m pytest -q"},
    }

    result = _run_bash_hook(payload)

    assert result.returncode == 0
    assert result.stderr == ""


def test_provider_manifests_share_identity_and_version():
    claude_manifest = json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex_manifest = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert claude_manifest["name"] == codex_manifest["name"] == "sg-harness"
    assert claude_manifest["version"] == codex_manifest["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", codex_manifest["version"])
    assert claude_manifest["description"] == codex_manifest["description"]
    assert codex_manifest["skills"] == "./skills/"
    assert (PLUGIN_ROOT / "skills").is_dir()
    assert HOOKS_FILE.is_file()
