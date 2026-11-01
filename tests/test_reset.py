import sys
import time

import anyio
import pytest

from conftest import seed_issue
from software_factory import cleanup, reset, store, worker


@pytest.fixture
def torn_down(monkeypatch) -> list:
    calls = []

    async def fake_teardown(issue_id, final_status, why, delete_remote=False):
        calls.append((issue_id, final_status, delete_remote))
        store.free_slot(issue_id)
        return True

    monkeypatch.setattr(cleanup, "teardown", fake_teardown)
    return calls


def issue(issue_id="issue-1"):
    return store.get_issue(issue_id)


def test_start_over_tears_down_sets_files_aside_and_queues(torn_down):
    ws = seed_issue(store.NEEDS_ATTENTION, slot=3, pr_url="https://ado/pullrequest/7", pr_id=7, plan_reviewed_by="akash")
    (ws / "plan.md").write_text("# Broken plan")
    (ws / "attachments").mkdir()
    message = anyio.run(reset.reset_issue, "bei-1")

    assert torn_down == [("issue-1", None, False)]
    row = issue()
    assert (row["status"], row["slot"], row["session_id"], row["pr_id"], row["attempts"]) == (store.QUEUED, None, None, None, 0)
    assert not (ws / "plan.md").exists()
    (aside,) = ws.glob("reset-*")
    assert (aside / "plan.md").read_text() == "# Broken plan" and (aside / "attachments").is_dir()
    assert "queued for triage from scratch" in message
    # The open PR still holds the branch, so the next attempt's push would be refused
    assert "https://ado/pullrequest/7 is still open" in message
    assert store.events("issue-1")[-1]["detail"] == "Reset: starting over"


def test_back_to_plan_keeps_the_worktree_and_session(torn_down):
    ws = seed_issue(store.NEEDS_ATTENTION, slot=3)
    (ws / "plan.md").write_text("# Plan")
    message = anyio.run(reset.reset_issue, "BEI-1", "plan")

    assert torn_down == []
    row = issue()
    assert (row["status"], row["slot"], row["session_id"]) == (store.PLAN_READY, 3, "s")
    assert "back to plan review" in message
    assert "approve" in store.allowed_actions(row)


def test_back_to_plan_needs_a_plan(torn_down):
    seed_issue(store.NEEDS_ATTENTION)
    with pytest.raises(reset.ResetRefused, match="no plan to go back to"):
        anyio.run(reset.reset_issue, "BEI-1", "plan")


def test_a_run_in_progress_blocks_the_reset_unless_forced(torn_down):
    seed_issue(store.IMPLEMENTING)
    store.start_run("issue-1", "implement", "worker")
    with pytest.raises(reset.ResetRefused, match="implement run in progress"):
        anyio.run(reset.reset_issue, "BEI-1")
    assert issue()["status"] == store.IMPLEMENTING

    anyio.run(reset.reset_issue, "BEI-1", "start", True)
    assert issue()["status"] == store.QUEUED
    assert store.open_runs() == []


def test_unknown_issue_is_refused():
    with pytest.raises(reset.ResetRefused, match="No issue BEI-404"):
        anyio.run(reset.reset_issue, "BEI-404")


def test_failed_teardown_is_reported_and_leaves_the_issue_flagged(monkeypatch):
    seed_issue(store.NEEDS_ATTENTION, slot=3)

    async def failing(issue_id, final_status, why, delete_remote=False):
        store.transition(issue_id, store.NEEDS_ATTENTION, reason="Reset, but cleanup failed at dropping the databases: Postgres is down")
        return False

    monkeypatch.setattr(cleanup, "teardown", failing)
    with pytest.raises(reset.ResetRefused, match="Postgres is down"):
        anyio.run(reset.reset_issue, "BEI-1")
    assert issue()["slot"] == 3


def test_reset_all_starts_unfinished_issues_over_and_lifts_the_pause(torn_down):
    seed_issue(store.NEEDS_ATTENTION, slot=1)
    seed_issue(store.PR_OPENED, issue_id="issue-2", identifier="BEI-2", slot=2)
    seed_issue(store.MERGED, issue_id="issue-3", identifier="BEI-3", slot=None)
    store.pause_until(time.time() + 600)

    results = anyio.run(reset.reset_all)
    assert len(results) == 2
    assert issue()["status"] == issue("issue-2")["status"] == store.QUEUED
    assert issue("issue-3")["status"] == store.MERGED
    assert store.paused_until() is None


def test_reset_all_refuses_while_runs_are_open(torn_down):
    seed_issue(store.IMPLEMENTING)
    store.start_run("issue-1", "implement", "worker")
    with pytest.raises(reset.ResetRefused, match="Runs are in progress for BEI-1"):
        anyio.run(reset.reset_all)


def test_cli_asks_before_resetting_everything(torn_down, monkeypatch, capsys):
    seed_issue(store.NEEDS_ATTENTION)
    monkeypatch.setattr(sys, "argv", ["software-factory", "reset", "--all"])
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    with pytest.raises(SystemExit) as exit_info:
        worker.main()
    assert exit_info.value.code == 1
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert "Nothing reset." in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["software-factory", "reset", "--all", "--yes"])
    with pytest.raises(SystemExit) as exit_info:
        worker.main()
    assert exit_info.value.code == 0
    assert issue()["status"] == store.QUEUED


def test_cli_resets_one_issue(torn_down, monkeypatch, capsys):
    seed_issue(store.NEEDS_ATTENTION)
    monkeypatch.setattr(sys, "argv", ["software-factory", "reset", "BEI-1"])
    with pytest.raises(SystemExit) as exit_info:
        worker.main()
    assert exit_info.value.code == 0
    assert "BEI-1: queued for triage from scratch" in capsys.readouterr().out
