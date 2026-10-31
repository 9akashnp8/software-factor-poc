import importlib
import json
import subprocess
from pathlib import Path

import anyio
import pytest

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
    run(repo, "checkout", "-b", "fix/BEI-1")
    return repo


@pytest.fixture
def ws(tmp_path: Path, monkeypatch, repo: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "bug_workspace" / "issue-1"
    ws.mkdir(parents=True)
    (ws / "metadata.json").write_text(json.dumps({"identifier": "BEI-1", "status": "plan-approved-agent-working", "session_id": "s"}))
    monkeypatch.setattr(main, "ensure_worktree", lambda _: repo)
    return ws


def stub_agents(monkeypatch, reviews: list, on_fix=None):
    """Replace the review session with canned verdicts and the fix session with on_fix."""
    calls = {"review": 0, "fix": 0}

    async def fake_review(worktree, ws):
        calls["review"] += 1
        return reviews.pop(0)

    async def fake_resume(issue_id, prompt=main.IMPLEMENT_PROMPT):
        calls["fix"] += 1
        if on_fix:
            on_fix()

    monkeypatch.setattr(main, "review_changes", fake_review)
    monkeypatch.setattr(main, "resume_session", fake_resume)
    return calls


def metadata(ws: Path) -> dict:
    return json.loads((ws / "metadata.json").read_text())


def gate():
    anyio.run(main.review_and_gate, "issue-1")


def test_check_branch_without_commits(repo):
    assert main.check_branch(repo) == f"No commits ahead of {main.BASE_BRANCH}"


def test_check_branch_with_dirty_tree(repo):
    commit(repo, "fix.txt")
    (repo / "stray.txt").write_text("x")
    assert main.check_branch(repo) == "Uncommitted changes left in the worktree"


def test_check_branch_ready(repo):
    commit(repo, "fix.txt")
    assert main.check_branch(repo) is None


def test_no_commits_skips_review(ws, monkeypatch):
    calls = stub_agents(monkeypatch, [])
    gate()
    assert metadata(ws)["status"] == "needs-attention"
    assert calls["review"] == 0


def test_clean_review_is_ready_for_pr(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    calls = stub_agents(monkeypatch, [CLEAN])
    gate()
    meta = metadata(ws)
    assert meta["status"] == "ready-for-pr"
    assert meta["review_rounds"] == 1
    assert calls["fix"] == 0
    assert "Not blocking" in (ws / "review.md").read_text()


def test_blocking_then_fixed(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    calls = stub_agents(monkeypatch, [BLOCKING, CLEAN], on_fix=lambda: commit(repo, "fix2.txt"))
    gate()
    meta = metadata(ws)
    assert meta["status"] == "ready-for-pr"
    assert meta["review_rounds"] == 2
    assert "needs_attention_reason" not in meta
    assert calls == {"review": 2, "fix": 1}


def test_blocking_twice_needs_attention(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    stub_agents(monkeypatch, [BLOCKING, BLOCKING], on_fix=lambda: commit(repo, "fix2.txt"))
    gate()
    meta = metadata(ws)
    assert meta["status"] == "needs-attention"
    assert meta["needs_attention_reason"] == "Blocking review findings remain after the fix round"


def test_fix_round_without_commit(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    calls = stub_agents(monkeypatch, [BLOCKING])
    gate()
    meta = metadata(ws)
    assert meta["status"] == "needs-attention"
    assert meta["needs_attention_reason"] == "Fix round made no new commit"
    assert calls["review"] == 1


def test_no_verdict_needs_attention(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    stub_agents(monkeypatch, [None])
    gate()
    assert metadata(ws)["needs_attention_reason"] == "Review returned no verdict"


def test_render_review():
    out = main.render_review(BLOCKING)
    assert "**Verdict:** Blocking" in out
    assert "`app/grades.py:12`: None not handled" in out
    assert "Suggestion: Return 'Not available'" in out
    assert "`app/grades.py`: Rename variable" in main.render_review(CLEAN)
