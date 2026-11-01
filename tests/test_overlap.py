import subprocess
from pathlib import Path

import pytest

from conftest import seed_issue
from software_factory import config, overlap, store


def run(cwd: Path, *args: str):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def plan(ws: Path, text: str):
    (ws / "plan.md").write_text(text)


@pytest.fixture
def target(tmp_path: Path, monkeypatch) -> Path:
    """A target repo where BEI-2's branch changes apps/api/router.py."""
    repo = tmp_path / "target"
    repo.mkdir()
    run(repo, "init", "-b", config.BASE_BRANCH)
    run(repo, "config", "user.email", "test@example.com")
    run(repo, "config", "user.name", "Test")
    (repo / "apps" / "api").mkdir(parents=True)
    (repo / "apps" / "api" / "router.py").write_text("x = 1\n")
    run(repo, "add", ".")
    run(repo, "commit", "-m", "base")
    run(repo, "update-ref", f"refs/remotes/origin/{config.BASE_BRANCH}", "HEAD")
    run(repo, "checkout", "-q", "-b", "fix/BEI-2")
    (repo / "apps" / "api" / "router.py").write_text("x = 2\n")
    run(repo, "commit", "-am", "fix")
    monkeypatch.setattr(config, "TARGET_REPO", repo)
    return repo


def test_mentioned_paths_finds_repo_paths():
    text = "Change `apps/api/router.py:42` and ./packages/shared/x.ts, not README.md or http://x"
    assert overlap.mentioned_paths(text) == {"apps/api/router.py", "packages/shared/x.ts"}


def test_plan_overlapping_a_branch_in_flight_is_reported(target):
    ws = seed_issue(store.PLAN_READY, slot=1)
    plan(ws, "Fix the routing in apps/api/router.py")
    seed_issue(store.PR_OPENED, issue_id="issue-2", identifier="BEI-2", slot=2)
    assert overlap.overlaps(store.get_issue("issue-1")) == [("BEI-2", store.PR_OPENED, ["apps/api/router.py"])]


def test_plan_overlapping_another_plan_is_reported(target):
    ws = seed_issue(store.PLAN_READY, slot=1)
    plan(ws, "Edit apps/web/src/chat.ts")
    other = seed_issue(store.IMPLEMENT_QUEUED, issue_id="issue-3", identifier="BEI-3", slot=3)
    plan(other, "Also edit apps/web/src/chat.ts")
    assert overlap.overlaps(store.get_issue("issue-1")) == [("BEI-3", store.IMPLEMENT_QUEUED, ["apps/web/src/chat.ts"])]


def test_finished_issues_and_unrelated_files_are_ignored(target):
    ws = seed_issue(store.PLAN_READY, slot=1)
    plan(ws, "Edit apps/api/other.py")
    seed_issue(store.PR_OPENED, issue_id="issue-2", identifier="BEI-2", slot=2)
    done = seed_issue(store.MERGED, issue_id="issue-4", identifier="BEI-4", slot=None)
    plan(done, "Edit apps/api/other.py")
    assert overlap.overlaps(store.get_issue("issue-1")) == []
