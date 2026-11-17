import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from conftest import seed_issue
from software_factory import cleanup, config, store


@pytest.fixture
def steps(monkeypatch) -> list:
    """Record teardown steps instead of touching Postgres, Redis and git."""
    calls = []
    monkeypatch.setattr(cleanup, "drop_databases", lambda identifier: calls.append(("drop", identifier)))
    monkeypatch.setattr(cleanup, "flush_redis", lambda slot: calls.append(("flush", slot)))
    monkeypatch.setattr(cleanup, "remove_worktree", lambda identifier: calls.append(("worktree", identifier)))
    monkeypatch.setattr(
        cleanup, "delete_branches", lambda identifier, remote, expected_tip=None: calls.append(("branch", identifier, remote))
    )
    return calls


@pytest.fixture
def linear_states(monkeypatch) -> list:
    moves = []

    async def fake_report(issue_id, state):
        moves.append((issue_id, state))

    monkeypatch.setattr(cleanup.linear, "report_state", fake_report)
    return moves


def merge():
    return anyio.run(cleanup.cleanup_after_merge, "issue-1", "merge-sha", "2026-11-01T09:26:17.9652534Z")


def test_merge_waits_for_deploy_and_tears_everything_down(steps, linear_states):
    ws = seed_issue(store.PR_OPENED, slot=4, pr_id=7)
    (ws / ".provisioned").write_text("app_bei_1")
    assert merge()

    # Merged: the PR is done with its branch, so the remote one goes too
    assert steps == [("drop", "BEI-1"), ("flush", 4), ("worktree", "BEI-1"), ("branch", "BEI-1", True)]
    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["slot"], issue["merge_commit"]) == (store.DEPLOY_PENDING, None, "merge-sha")
    assert linear_states == [("issue-1", config.LINEAR_PENDING_DEPLOY_STATE)]
    # A reopened attempt gets a new database, so it must provision again
    assert not (ws / ".provisioned").exists()


def test_without_a_deploy_pipeline_qa_hears_at_merge(steps, linear_states, monkeypatch):
    monkeypatch.setattr(config, "DEPLOY_PIPELINE_ID", 0)
    seed_issue(store.PR_OPENED, slot=4, pr_id=7)
    assert merge()
    assert store.get_issue("issue-1")["status"] == store.MERGED
    assert linear_states == [("issue-1", config.LINEAR_MERGED_STATE)]


def test_failed_step_keeps_the_merge_and_the_slot_for_a_retry(monkeypatch, steps, linear_states):
    seed_issue(store.PR_OPENED, slot=4, pr_id=7)

    def locked(identifier):
        raise OSError("worktree files are in use")

    monkeypatch.setattr(cleanup, "remove_worktree", locked)
    assert not merge()

    assert steps == [("drop", "BEI-1"), ("flush", 4)]
    issue = store.get_issue("issue-1")
    # The merge happened whatever cleanup does, so Linear already knows and the status stands
    assert (issue["status"], issue["slot"]) == (store.DEPLOY_PENDING, 4)
    assert linear_states == [("issue-1", config.LINEAR_PENDING_DEPLOY_STATE)]
    detail = store.events("issue-1")[-1]["detail"]
    assert detail.startswith("PR merged, but cleanup failed at removing the worktree: worktree files are in use")

    # Retried failures aren't recorded twice
    anyio.run(cleanup.teardown, "issue-1", None, "PR merged", True, False)
    assert [e["detail"] for e in store.events("issue-1")].count(detail) == 1


def test_release_tears_down_and_leaves_linear_alone(steps, linear_states):
    seed_issue(store.RELEASING, slot=2)
    assert anyio.run(cleanup.release, "issue-1")
    assert [s[0] for s in steps] == ["drop", "flush", "worktree", "branch"]
    # Released: any PR is left as it is, and it lives on the remote branch
    assert steps[-1] == ("branch", "BEI-1", False)
    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["slot"]) == (store.RELEASED, None)
    assert linear_states == []



def test_release_after_an_abandoned_pr_deletes_the_remote_branch(steps):
    seed_issue(store.RELEASING, slot=2, pr_id=7)
    assert anyio.run(cleanup.release, "issue-1", "abc123")
    assert steps[-1] == ("branch", "BEI-1", True)
    assert store.get_issue("issue-1")["status"] == store.RELEASED

def test_releasing_an_issue_that_never_started_touches_nothing(steps):
    seed_issue(store.RELEASING, slot=None)
    assert anyio.run(cleanup.release, "issue-1")
    assert steps == []
    assert store.get_issue("issue-1")["status"] == store.RELEASED


def test_remove_worktree_deletes_what_git_left_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_DIR", tmp_path)
    leftover = tmp_path / "BEI-1"
    (leftover / ".venv").mkdir(parents=True)
    (leftover / ".venv" / "pyvenv.cfg").write_text("home = nowhere")
    (leftover / ".venv" / "pyvenv.cfg").chmod(0o444)
    git_calls = []

    def fake_git(*args):
        git_calls.append(args)
        if args[0] == "worktree" and args[1] == "remove":
            raise cleanup.subprocess.CalledProcessError(128, args, stderr="not a working tree")

    monkeypatch.setattr(cleanup, "_git", fake_git)
    cleanup.remove_worktree("BEI-1")

    assert not leftover.exists()
    assert git_calls[-1] == ("worktree", "prune")


def test_remove_worktree_stops_an_orphan_holding_it_open(tmp_path, monkeypatch):
    """A uvicorn --reload worker whose reloader died listens on nothing, so stop_servers
    misses it, yet on Windows its open files keep the worktree from being deleted."""
    monkeypatch.setattr(config, "WORKTREE_DIR", tmp_path)
    worktree = tmp_path / "BEI-1"
    (worktree / ".venv").mkdir(parents=True)
    held = worktree / ".venv" / "held.txt"
    held.write_text("open")
    orphan = cleanup.subprocess.Popen(
        [sys.executable, "-c", "import sys, time; f = open('.venv/held.txt'); print('ready', flush=True); time.sleep(60)"],
        cwd=worktree,
        stdout=cleanup.subprocess.PIPE,
        text=True,
    )
    try:
        assert orphan.stdout.readline().strip() == "ready"

        def fake_git(*args):
            if args[0] == "worktree" and args[1] == "remove":
                raise cleanup.subprocess.CalledProcessError(128, args, stderr="Permission denied")

        monkeypatch.setattr(cleanup, "_git", fake_git)
        cleanup.remove_worktree("BEI-1")

        assert orphan.wait(timeout=10) is not None
        assert not worktree.exists()
    finally:
        orphan.kill()
        orphan.stdout.close()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch) -> Path:
    """A clone whose origin has fix/BEI-1 and branches and a tag with similar names."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "dev", str(remote))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "dev")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "a.txt").write_text("a")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    for ref in ("dev", "fix/BEI-1", "fix/BEI-10", "someone/fix/BEI-1"):
        git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{ref}")
    git(repo, "push", "-q", "origin", "HEAD:refs/tags/fix/BEI-1")
    monkeypatch.setattr(config, "TARGET_REPO", repo)
    return repo


def remote_refs(repo: Path) -> set[str]:
    return {line.split("	")[1] for line in git(repo, "ls-remote", "origin").splitlines()}


def test_delete_branches_removes_only_the_issues_branch(origin):
    tip = git(origin, "rev-parse", "HEAD")
    assert cleanup.delete_branches("BEI-1", expected_tip=tip) is None
    assert remote_refs(origin) == {
        "HEAD", "refs/heads/dev", "refs/heads/fix/BEI-10", "refs/heads/someone/fix/BEI-1", "refs/tags/fix/BEI-1"
    }


def test_delete_branches_keeps_a_branch_someone_pushed_to(origin):
    pr_tip = git(origin, "rev-parse", "HEAD")
    (origin / "b.txt").write_text("b")
    git(origin, "add", "b.txt")
    git(origin, "commit", "-q", "-m", "b")
    git(origin, "push", "-q", "origin", "HEAD:refs/heads/fix/BEI-1")
    note = cleanup.delete_branches("BEI-1", expected_tip=pr_tip)
    assert note.startswith("Kept fix/BEI-1 on origin")
    assert "refs/heads/fix/BEI-1" in remote_refs(origin)


def test_delete_branches_without_the_branch_on_origin_does_nothing(origin):
    assert cleanup.delete_branches("BEI-2") is None
    assert "refs/heads/fix/BEI-1" in remote_refs(origin)
