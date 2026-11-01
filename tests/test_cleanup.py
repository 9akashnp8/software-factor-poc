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
    monkeypatch.setattr(cleanup, "delete_branches", lambda identifier: calls.append(("branch", identifier)))
    return calls


@pytest.fixture
def linear_states(monkeypatch) -> list:
    moves = []

    async def fake_report(issue_id, state):
        moves.append((issue_id, state))

    monkeypatch.setattr(cleanup.linear, "report_state", fake_report)
    return moves


def merged_pr(monkeypatch, status="completed"):
    async def fake_status(pr_id):
        return status

    monkeypatch.setattr(cleanup, "pull_request_status", fake_status)


def test_merged_pr_tears_everything_down_and_frees_the_slot(monkeypatch, steps, linear_states):
    seed_issue(store.PR_OPENED, slot=4, pr_id=7)
    merged_pr(monkeypatch)
    anyio.run(cleanup.check_merges)

    assert steps == [("drop", "BEI-1"), ("flush", 4), ("worktree", "BEI-1"), ("branch", "BEI-1")]
    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["slot"]) == (store.MERGED, None)
    assert linear_states == [("issue-1", config.LINEAR_MERGED_STATE)]


def test_open_pr_is_left_alone(monkeypatch, steps):
    seed_issue(store.PR_OPENED, pr_id=7)
    merged_pr(monkeypatch, "active")
    anyio.run(cleanup.check_merges)
    assert steps == []
    assert store.get_issue("issue-1")["status"] == store.PR_OPENED


def test_abandoned_pr_needs_attention_and_keeps_everything(monkeypatch, steps):
    seed_issue(store.PR_OPENED, slot=4, pr_id=7)
    merged_pr(monkeypatch, "abandoned")
    anyio.run(cleanup.check_merges)
    assert steps == []
    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["slot"]) == (store.NEEDS_ATTENTION, 4)


def test_failed_step_stops_cleanup_and_keeps_the_slot(monkeypatch, steps, linear_states):
    seed_issue(store.PR_OPENED, slot=4, pr_id=7)
    merged_pr(monkeypatch)

    def locked(identifier):
        raise OSError("worktree files are in use")

    monkeypatch.setattr(cleanup, "remove_worktree", locked)
    anyio.run(cleanup.check_merges)

    assert steps == [("drop", "BEI-1"), ("flush", 4)]
    issue = store.get_issue("issue-1")
    assert issue["status"] == store.NEEDS_ATTENTION
    assert issue["needs_attention_reason"] == "PR merged, but cleanup failed at removing the worktree: worktree files are in use"
    assert issue["slot"] == 4
    assert linear_states == []


def test_unreachable_azure_devops_skips_the_issue(monkeypatch, steps):
    seed_issue(store.PR_OPENED, pr_id=7)

    async def failing(pr_id):
        raise RuntimeError("AZURE_DEVOPS_PAT is not set")

    monkeypatch.setattr(cleanup, "pull_request_status", failing)
    anyio.run(cleanup.check_merges)
    assert store.get_issue("issue-1")["status"] == store.PR_OPENED
