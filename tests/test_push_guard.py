import importlib

import anyio
import pytest

main = importlib.import_module("software_factory.main")


def decision(command: str):
    result = anyio.run(main.deny_git_push, {"tool_input": {"command": command}}, None, None)
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push -u origin fix/BEI-1",
        "git -C . push origin dev",
        "git -c push.default=current push",
        "git 'push' origin dev",
        "cd repo && git push --force",
        "git.exe push origin dev",
        "git --no-pager push",
    ],
)
def test_pushes_are_denied(command):
    assert decision(command) == "deny"


@pytest.mark.parametrize(
    "command",
    ["git status", "git stash push -m wip", "git log --grep push", "git commit -m 'push fix'"],
)
def test_other_git_commands_pass(command):
    assert decision(command) is None


def test_every_agent_phase_denies_push():
    assert set(main.PUSH_DENY_RULES) <= set(main.PLAN_DISALLOWED_TOOLS)
