import importlib
import subprocess
from pathlib import Path

import anyio
import pytest

from conftest import seed_issue
from software_factory import store

main = importlib.import_module("software_factory.main")

BLOCKING = {
    "blocking": True,
    "summary": "Null grade still crashes the formatter.",
    "findings": [{"severity": "blocking", "file": "app/grades.py", "line": 12, "issue": "None not handled", "suggestion": "Return 'Not available'"}],
}
CLEAN = {"blocking": False, "summary": "Looks good.", "findings": [{"severity": "minor", "file": "app/grades.py", "issue": "Rename variable"}]}


def run(cwd: Path, *args: str):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def commit(repo: Path, name: str):
    (repo / name).write_text(name)
    run(repo, "add", name)
    run(repo, "commit", "-m", name)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-b", main.BASE_BRANCH)
    run(repo, "config", "user.email", "test@example.com")
    run(repo, "config", "user.name", "Test")
    commit(repo, "base.txt")
    # Stands in for the fetched remote tip the worktree branches from
    run(repo, "update-ref", f"refs/remotes/origin/{main.BASE_BRANCH}", "HEAD")
    run(repo, "checkout", "-b", "fix/BEI-1")
    return repo


@pytest.fixture
def ws(monkeypatch, repo: Path) -> Path:
    ws = seed_issue(store.IMPLEMENTING)
    monkeypatch.setattr(main, "ensure_worktree", lambda _: repo)
    return ws


def stub_agents(monkeypatch, reviews: list, on_fix=None):
    """Replace the review session with canned verdicts and the fix session with on_fix."""
    calls = {"review": 0, "fix": 0}

    async def fake_review(issue_id, worktree, ws, owner="cli"):
        calls["review"] += 1
        return reviews.pop(0)

    async def fake_fix(issue_id, review_md, owner="cli"):
        calls["fix"] += 1
        if on_fix:
            on_fix()
        return True

    monkeypatch.setattr(main, "review_changes", fake_review)
    monkeypatch.setattr(main, "fix_review_findings", fake_fix)
    return calls


def issue() -> dict:
    return store.get_issue("issue-1")


def gate() -> bool:
    return anyio.run(main.review_and_gate, "issue-1")


def test_check_branch_without_commits(repo):
    assert main.check_branch(repo) == f"No commits ahead of origin/{main.BASE_BRANCH}"


def test_check_branch_ignores_stale_local_base(repo):
    # Upstream moved on after the local dev was last pulled, and the branch starts from
    # origin/dev, so the upstream commit isn't the agent's work
    commit(repo, "upstream.txt")
    run(repo, "update-ref", f"refs/remotes/origin/{main.BASE_BRANCH}", "HEAD")
    assert main.check_branch(repo) == f"No commits ahead of origin/{main.BASE_BRANCH}"


def test_check_branch_with_dirty_tree(repo):
    commit(repo, "fix.txt")
    (repo / "stray.txt").write_text("x")
    assert main.check_branch(repo) == "Uncommitted changes left in the worktree: stray.txt"


def test_check_branch_ignores_line_ending_rewrites(repo):
    # A commit hook (prettier) rewrites committed CRLF files with LF while core.autocrlf
    # is on: git status lists them as modified, but their content is what was committed
    run(repo, "config", "core.autocrlf", "true")
    (repo / "fix.ts").write_bytes(b"a\r\nb\r\n")
    run(repo, "add", "fix.ts")
    run(repo, "commit", "-m", "fix")
    (repo / "fix.ts").write_bytes(b"a\nb\n")
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    assert main.check_branch(repo) is None


def test_check_branch_lists_real_changes(repo):
    commit(repo, "fix.txt")
    (repo / "fix.txt").write_text("changed after the commit")
    (repo / "new.txt").write_text("untracked")
    assert main.check_branch(repo) == "Uncommitted changes left in the worktree: fix.txt, new.txt"


def test_check_branch_ready(repo):
    commit(repo, "fix.txt")
    assert main.check_branch(repo) is None


def test_no_commits_skips_review(ws, monkeypatch):
    calls = stub_agents(monkeypatch, [])
    assert not gate()
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert calls["review"] == 0


def test_clean_review_is_ready_for_pr(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    calls = stub_agents(monkeypatch, [CLEAN])
    assert gate()
    assert issue()["status"] == store.READY_FOR_PR
    assert calls == {"review": 1, "fix": 0}
    assert "Not blocking" in (ws / "review.md").read_text()


def test_blocking_then_fixed(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    calls = stub_agents(monkeypatch, [BLOCKING, CLEAN], on_fix=lambda: commit(repo, "fix2.txt"))
    assert gate()
    assert issue()["status"] == store.READY_FOR_PR
    assert issue()["needs_attention_reason"] is None
    assert calls == {"review": 2, "fix": 1}


def test_blocking_twice_needs_attention(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    stub_agents(monkeypatch, [BLOCKING, BLOCKING], on_fix=lambda: commit(repo, "fix2.txt"))
    assert not gate()
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert issue()["needs_attention_reason"] == "Blocking review findings remain after the fix round"


def test_fix_round_without_commit(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    calls = stub_agents(monkeypatch, [BLOCKING])
    assert not gate()
    assert issue()["needs_attention_reason"] == "Fix round made no new commit"
    assert calls["review"] == 1


def test_no_verdict_needs_attention(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    stub_agents(monkeypatch, [None])
    assert not gate()
    assert issue()["needs_attention_reason"] == "Review returned no verdict"


def test_render_review():
    out = main.render_review(BLOCKING)
    assert "**Verdict:** Blocking" in out
    assert "`app/grades.py:12`: None not handled" in out
    assert "Suggestion: Return 'Not available'" in out
    assert "`app/grades.py`: Rename variable" in main.render_review(CLEAN)
