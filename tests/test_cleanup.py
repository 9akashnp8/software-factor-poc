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


def test_merge_tears_everything_down_and_frees_the_slot(steps, linear_states):
    ws = seed_issue(store.PR_OPENED, slot=4, pr_id=7)
    (ws / ".provisioned").write_text("app_bei_1")
    assert anyio.run(cleanup.cleanup_after_merge, "issue-1")

    assert steps == [("drop", "BEI-1"), ("flush", 4), ("worktree", "BEI-1"), ("branch", "BEI-1")]
    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["slot"]) == (store.MERGED, None)
    assert linear_states == [("issue-1", config.LINEAR_MERGED_STATE)]
    # A reopened attempt gets a new database, so it must provision again
    assert not (ws / ".provisioned").exists()


def test_failed_step_stops_cleanup_and_keeps_the_slot(monkeypatch, steps, linear_states):
    seed_issue(store.PR_OPENED, slot=4, pr_id=7)

    def locked(identifier):
        raise OSError("worktree files are in use")

    monkeypatch.setattr(cleanup, "remove_worktree", locked)
    assert not anyio.run(cleanup.cleanup_after_merge, "issue-1")

    assert steps == [("drop", "BEI-1"), ("flush", 4)]
    issue = store.get_issue("issue-1")
    assert issue["status"] == store.NEEDS_ATTENTION
    assert issue["needs_attention_reason"] == "PR merged, but cleanup failed at removing the worktree: worktree files are in use"
    assert issue["slot"] == 4
    assert linear_states == []


def test_release_tears_down_and_leaves_linear_alone(steps, linear_states):
    seed_issue(store.RELEASING, slot=2)
    assert anyio.run(cleanup.release, "issue-1")
    assert [s[0] for s in steps] == ["drop", "flush", "worktree", "branch"]
    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["slot"]) == (store.RELEASED, None)
    assert linear_states == []


def test_releasing_an_issue_that_never_started_touches_nothing(steps):
    seed_issue(store.RELEASING, slot=None)
    assert anyio.run(cleanup.release, "issue-1")
    assert steps == []
    assert store.get_issue("issue-1")["status"] == store.RELEASED
