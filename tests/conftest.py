import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# store reads this on import, so set it before any test module loads
os.environ["SF_WORKSPACE_DIR"] = tempfile.mkdtemp(prefix="sf-tests-")

from software_factory import config, factory_log, store  # noqa: E402


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """A fresh workspace and database for every test, and no writes to the real Linear."""
    root = tmp_path / "bug_workspace"
    monkeypatch.setattr(store, "WORKSPACE_DIR", root)
    monkeypatch.setattr(store, "DB_PATH", root / "factory.db")
    monkeypatch.setattr(config, "LINEAR_WRITEBACK", False)
    # Settings from your .env mustn't change what the tests expect
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    # Nor may the real target repo's worktrees
    monkeypatch.setattr(config, "TARGET_REPO", tmp_path / "target")
    monkeypatch.setattr(config, "WORKTREE_DIR", tmp_path / "worktrees")
    return root


@pytest.fixture(autouse=True)
def isolated_logging():
    """Undo what factory_log.setup() did in a test (worker.main() calls it), so each test
    that sets logging up gets its own files."""
    root = logging.getLogger()
    handlers, level, configured, hook = root.handlers[:], root.level, factory_log._configured, sys.excepthook
    factory_log._configured = None
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    factory_log._configured = configured
    sys.excepthook = hook


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class DevRepo:
    """An origin, a target repo that fetches from it, and an upstream clone where other PRs merge."""

    def __init__(self, tmp_path: Path):
        self.origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", config.BASE_BRANCH, str(self.origin))
        self.upstream = tmp_path / "upstream"
        self.upstream.mkdir()
        git(self.upstream, "init", "-q", "-b", config.BASE_BRANCH)
        git(self.upstream, "config", "user.email", "test@example.com")
        git(self.upstream, "config", "user.name", "Test")
        (self.upstream / "README.md").write_text("target")
        git(self.upstream, "add", "README.md")
        git(self.upstream, "commit", "-q", "-m", "base")
        git(self.upstream, "remote", "add", "origin", str(self.origin))
        git(self.upstream, "push", "-q", "origin", config.BASE_BRANCH)
        self.target = config.TARGET_REPO
        git(tmp_path, "clone", "-q", str(self.origin), str(self.target))

    def worktree(self, identifier: str = "BEI-1") -> Path:
        """An issue's worktree, branched from origin's dev as the factory does it."""
        path = config.WORKTREE_DIR / identifier
        git(self.target, "worktree", "add", "-q", "-b", f"fix/{identifier}", str(path), f"origin/{config.BASE_BRANCH}")
        git(path, "config", "user.email", "test@example.com")
        git(path, "config", "user.name", "Test")
        return path

    def merge_pr(self, number: int, title: str, files: list[str]) -> str:
        """Merge a PR into origin's dev the way Azure DevOps does; returns the merge commit."""
        git(self.upstream, "checkout", "-q", "-b", f"pr-{number}")
        for name in files:
            path = self.upstream / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"PR {number}")
            git(self.upstream, "add", name)
        git(self.upstream, "commit", "-q", "-m", title)
        git(self.upstream, "checkout", "-q", config.BASE_BRANCH)
        git(self.upstream, "merge", "-q", "--no-ff", "-m", f"Merged PR {number}: {title}", f"pr-{number}")
        git(self.upstream, "push", "-q", "origin", config.BASE_BRANCH)
        return git(self.upstream, "rev-parse", "HEAD")


@pytest.fixture
def dev_repo(tmp_path: Path) -> DevRepo:
    return DevRepo(tmp_path)


def set_slot(issue_id: str, slot: int | None):
    # Only claim_next() hands out slots in the factory itself
    with store._db() as conn:
        conn.execute("UPDATE issues SET slot = ? WHERE id = ?", (slot, issue_id))


def seed_issue(status: str, issue_id: str = "issue-1", identifier: str = "BEI-1", title: str = "Grade shows .None", slot: int | None = 1, **fields) -> Path:
    """Create an issue in status with a session and a slot, and return its workspace folder."""
    store.upsert_issue(issue_id, identifier, title)
    store.set_fields(issue_id, session_id="s", **fields)
    store.transition(issue_id, status)
    set_slot(issue_id, slot)
    ws = store.issue_dir(issue_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws
