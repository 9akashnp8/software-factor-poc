import importlib
import subprocess
from pathlib import Path

import anyio
import pytest

from conftest import seed_issue
from software_factory import config, store
from test_main import fake_agent, result

main = importlib.import_module("software_factory.main")
BRANCH = "fix/BEI-1"


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, name: str, content: str | None = None):
    (repo / name).write_text(content or name)
    run(repo, "add", name)
    run(repo, "commit", "-m", name)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A fix branch already pushed as a PR, with the base moved on since."""
    remote = tmp_path / "remote.git"
    run(tmp_path, "init", "--bare", "-b", config.BASE_BRANCH, str(remote))
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-b", config.BASE_BRANCH)
    run(repo, "config", "user.email", "test@example.com")
    run(repo, "config", "user.name", "Test")
    run(repo, "remote", "add", "origin", str(remote))
    commit(repo, "base.txt")
    run(repo, "push", "-q", "origin", config.BASE_BRANCH)
    run(repo, "checkout", "-q", "-b", BRANCH)
    commit(repo, "fix.txt")
    run(repo, "push", "-q", "-u", "origin", BRANCH)
    # Another fix merged into the base meanwhile
    run(repo, "checkout", "-q", config.BASE_BRANCH)
    commit(repo, "other-fix.txt")
    run(repo, "push", "-q", "origin", config.BASE_BRANCH)
    run(repo, "checkout", "-q", BRANCH)
    monkeypatch.setattr(main, "ensure_worktree", lambda _: repo)
    return repo


@pytest.fixture
def pr_waiting(repo) -> Path:
    return seed_issue(store.UPDATING, pending_feedback="Azure DevOps reports merge conflicts between this branch and dev.")


def update():
    anyio.run(main.update_pull_request, "issue-1")


def remote_head(repo: Path) -> str:
    return run(repo, "ls-remote", "origin", BRANCH).split()[0]


def test_merging_the_base_pushes_to_the_same_pr(monkeypatch, repo, pr_waiting):
    sessions = fake_agent(monkeypatch, work=lambda: run(repo, "merge", "--no-edit", f"origin/{config.BASE_BRANCH}"), results=[result(text="Merged dev; no conflicts")])
    update()

    assert store.get_issue("issue-1")["status"] == store.PR_OPENED
    assert remote_head(repo) == run(repo, "rev-parse", "HEAD")
    assert main.contains(repo, f"origin/{config.BASE_BRANCH}")
    prompt = sessions[0].prompt
    assert "merge conflicts" in prompt and f"git merge origin/{config.BASE_BRANCH}" in prompt
    assert sessions[0].options.resume == "s"
    assert "Merged dev" in store.events("issue-1")[-1]["detail"]


def test_update_without_merging_the_base_needs_attention(monkeypatch, repo, pr_waiting):
    pushed = remote_head(repo)
    fake_agent(monkeypatch, work=lambda: commit(repo, "tweak.txt"))
    update()
    assert store.get_issue("issue-1")["needs_attention_reason"] == f"The update didn't merge origin/{config.BASE_BRANCH} into the branch"
    assert remote_head(repo) == pushed


def test_unresolved_conflict_needs_attention(monkeypatch, repo, pr_waiting):
    # Both sides change the same line, and the agent leaves the conflict in the tree
    run(repo, "checkout", "-q", config.BASE_BRANCH)
    commit(repo, "fix.txt", "base's version")
    run(repo, "push", "-q", "origin", config.BASE_BRANCH)
    run(repo, "checkout", "-q", BRANCH)

    def leave_conflict():
        subprocess.run(["git", "-C", str(repo), "merge", f"origin/{config.BASE_BRANCH}"], capture_output=True)

    fake_agent(monkeypatch, work=leave_conflict)
    update()
    assert store.get_issue("issue-1")["needs_attention_reason"] == "Uncommitted changes left in the worktree"


def test_failed_update_run_needs_attention_and_pushes_nothing(monkeypatch, repo, pr_waiting):
    pushed = remote_head(repo)
    fake_agent(monkeypatch, results=[result("error_max_turns", is_error=True, text="")])
    update()
    assert "update run didn't finish" in store.get_issue("issue-1")["needs_attention_reason"]
    assert remote_head(repo) == pushed
