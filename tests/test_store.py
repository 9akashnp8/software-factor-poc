import sqlite3
import time
from types import SimpleNamespace

from conftest import seed_issue
from software_factory import config, store


def statuses() -> list[tuple]:
    return [(e["from_status"], e["to_status"]) for e in store.events("issue-1")]


def test_upsert_creates_issue_with_event():
    store.upsert_issue("issue-1", "BEI-1", "Title", "https://linear/BEI-1")
    issue = store.get_issue("issue-1")
    assert (issue["identifier"], issue["status"], issue["url"]) == ("BEI-1", store.GATHERING_CONTEXT, "https://linear/BEI-1")
    assert statuses() == [(None, store.GATHERING_CONTEXT)]


def test_transition_with_expected_status_claims_once():
    seed_issue(store.PLAN_READY)
    assert store.transition("issue-1", store.IMPLEMENTING, expected=store.ACTIONS["approve"], actor="akash")
    # A second click finds the issue already claimed
    assert not store.transition("issue-1", store.REWORKING, expected=store.ACTIONS["rework"], actor="akash")
    assert store.get_issue("issue-1")["status"] == store.IMPLEMENTING
    assert store.events("issue-1")[-1]["actor"] == "akash"


def test_transition_unknown_issue_returns_false():
    assert not store.transition("missing", store.PLANNING)


def test_reason_is_kept_only_while_needing_attention():
    seed_issue(store.PLANNING)
    store.transition("issue-1", store.NEEDS_ATTENTION, reason="No plan")
    assert store.get_issue("issue-1")["needs_attention_reason"] == "No plan"
    store.transition("issue-1", store.PLAN_READY)
    assert store.get_issue("issue-1")["needs_attention_reason"] is None


def test_runs_record_result():
    seed_issue(store.IMPLEMENTING)
    run_id = store.start_run("issue-1", "implement", "web")
    result = SimpleNamespace(session_id="s2", subtype="error_max_turns", is_error=True, num_turns=150, total_cost_usd=1.25, duration_ms=9000, result="cut off")
    store.finish_run(run_id, result)
    (run,) = store.runs("issue-1")
    assert (run["subtype"], run["num_turns"], run["cost_usd"], run["result_text"]) == ("error_max_turns", 150, 1.25, "cut off")
    assert run["ended_at"] is not None


def test_allowed_actions_need_a_plan_and_session():
    ws = seed_issue(store.PLAN_READY)
    assert store.allowed_actions(store.get_issue("issue-1")) == ["release"]
    (ws / "plan.md").write_text("# Plan")
    assert store.allowed_actions(store.get_issue("issue-1")) == ["approve", "rework", "refresh", "release"]
    store.transition("issue-1", store.PR_OPENED)
    assert store.allowed_actions(store.get_issue("issue-1")) == []


def test_refresh_is_claimed_after_rework_and_requeued_if_cut_off():
    seed_issue(store.PLAN_READY)
    seed_issue(store.REWORK_QUEUED, issue_id="r", identifier="BEI-2", slot=2)
    assert store.transition("issue-1", store.REFRESH_QUEUED, expected=store.ACTIONS["refresh"])
    assert store.claim_next() == ("r", "rework")
    assert store.claim_next() == ("issue-1", "refresh")
    assert store.get_issue("issue-1")["status"] == store.REFRESHING
    assert sorted(store.requeue_orphans()) == ["issue-1", "r"]
    assert store.get_issue("issue-1")["status"] == store.REFRESH_QUEUED


def test_enqueue_new_skips_issues_seen_before():
    assert store.enqueue_new("issue-1", "BEI-1", "Title")
    store.transition("issue-1", store.PR_OPENED)
    assert not store.enqueue_new("issue-1", "BEI-1", "Title")
    assert store.get_issue("issue-1")["status"] == store.PR_OPENED


def test_claim_next_serves_waiting_people_first_then_oldest():
    store.enqueue_new("t1", "BEI-1")
    store.enqueue_new("t2", "BEI-2")
    seed_issue(store.IMPLEMENT_QUEUED, issue_id="i1", identifier="BEI-3", slot=5)
    assert store.claim_next() == ("i1", "implement")
    assert store.claim_next() == ("t1", "triage")
    assert store.claim_next() == ("t2", "triage")
    assert store.claim_next() is None
    assert store.get_issue("t1")["status"] == store.GATHERING_CONTEXT


def test_triage_claim_takes_the_lowest_free_slot(monkeypatch):
    monkeypatch.setattr(config, "SLOT_COUNT", 2)
    seed_issue(store.PR_OPENED, issue_id="held", identifier="BEI-9", slot=1)
    store.enqueue_new("t1", "BEI-1")
    store.enqueue_new("t2", "BEI-2")
    store.claim_next()
    assert store.get_issue("t1")["slot"] == 2
    # Every slot is in use until a merge frees one
    assert store.claim_next() is None
    store.free_slot("held")
    assert store.claim_next() == ("t2", "triage")
    assert store.get_issue("t2")["slot"] == 1


def test_retried_triage_keeps_its_slot(monkeypatch):
    monkeypatch.setattr(config, "SLOT_COUNT", 1)
    seed_issue(store.QUEUED, slot=1)
    assert store.claim_next() == ("issue-1", "triage")
    assert store.get_issue("issue-1")["slot"] == 1


def test_requeue_returns_to_the_phase_queue_until_attempts_run_out(monkeypatch):
    monkeypatch.setattr(config, "MAX_ATTEMPTS", 2)
    seed_issue(store.IMPLEMENTING)
    for _ in range(2):
        assert store.requeue("issue-1", "rate limit")
        assert store.get_issue("issue-1")["status"] == store.IMPLEMENT_QUEUED
        store.claim_next()
    assert not store.requeue("issue-1", "rate limit")
    issue = store.get_issue("issue-1")
    assert issue["status"] == store.NEEDS_ATTENTION
    assert "gave up after 2 retries" in issue["needs_attention_reason"]


def test_milestones_reset_attempts():
    seed_issue(store.IMPLEMENTING)
    store.requeue("issue-1", "rate limit")
    store.transition("issue-1", store.PR_OPENED)
    assert store.get_issue("issue-1")["attempts"] == 0


def test_requeue_ignores_issues_not_running():
    seed_issue(store.PLAN_READY)
    assert not store.requeue("issue-1", "rate limit")
    assert store.get_issue("issue-1")["status"] == store.PLAN_READY


def test_pause_keeps_the_latest_end():
    assert store.paused_until() is None
    store.pause_until(time.time() + 600)
    store.pause_until(time.time() + 60)
    assert store.paused_until() > time.time() + 500
    store.pause_until(time.time() - 1)
    assert store.paused_until() is not None


def test_expired_pause_reads_as_none():
    store.pause_until(time.time() - 5)
    assert store.paused_until() is None


def test_worker_restart_requeues_orphans_and_closes_their_runs():
    seed_issue(store.IMPLEMENTING)
    seed_issue(store.PLANNING, issue_id="issue-2", identifier="BEI-2", slot=2)
    seed_issue(store.PLAN_READY, issue_id="issue-3", identifier="BEI-3", slot=3)
    store.start_run("issue-1", "implement", "worker")

    assert sorted(store.requeue_orphans()) == ["issue-1", "issue-2"]
    assert store.get_issue("issue-1")["status"] == store.IMPLEMENT_QUEUED
    assert store.get_issue("issue-2")["status"] == store.QUEUED
    assert store.get_issue("issue-3")["status"] == store.PLAN_READY
    assert store.runs("issue-1")[0]["ended_at"] is not None


def test_retry_is_offered_only_without_a_plan():
    ws = seed_issue(store.NEEDS_ATTENTION)
    assert store.allowed_actions(store.get_issue("issue-1")) == ["retry", "release"]
    (ws / "plan.md").write_text("# Plan")
    assert store.allowed_actions(store.get_issue("issue-1")) == ["approve", "rework", "release"]


def test_older_databases_gain_the_new_columns():
    store.DB_PATH.parent.mkdir(parents=True)
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("CREATE TABLE issues (id TEXT PRIMARY KEY, identifier TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT, status TEXT NOT NULL, session_id TEXT, pr_url TEXT, needs_attention_reason TEXT, plan_reviewed_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.close()
    store.enqueue_new("issue-1", "BEI-1")
    assert store.get_issue("issue-1")["attempts"] == 0


def test_triage_waits_while_review_is_full(monkeypatch):
    monkeypatch.setattr(config, "MAX_PLANS_IN_REVIEW", 2)
    seed_issue(store.PLAN_READY, issue_id="p1", identifier="BEI-1", slot=1)
    seed_issue(store.REWORKING, issue_id="p2", identifier="BEI-2", slot=2)
    seed_issue(store.IMPLEMENT_QUEUED, issue_id="i1", identifier="BEI-3", slot=3)
    store.enqueue_new("t1", "BEI-4")

    # Approved work isn't held back by the cap; new triage is
    assert store.claim_next() == ("i1", "implement")
    assert store.claim_next() is None
    store.transition("p1", store.IMPLEMENT_QUEUED)
    assert store.plans_in_review() == 1
    assert store.claim_next() == ("p1", "implement")
    assert store.claim_next() == ("t1", "triage")
    # The plan being written counts too, so a second worker slot can't overshoot
    assert store.plans_in_review() == 2


def test_open_pr_is_offered_once_the_implementation_reported():
    ws = seed_issue(store.NEEDS_ATTENTION)
    (ws / "plan.md").write_text("# Plan")
    assert "publish" not in store.allowed_actions(store.get_issue("issue-1"))
    (ws / "report.md").write_text("Root cause: x")
    assert "publish" in store.allowed_actions(store.get_issue("issue-1"))


def test_publish_is_claimed_before_new_triage_and_requeued_as_publish():
    store.enqueue_new("t1", "BEI-2")
    seed_issue(store.PUBLISH_QUEUED)
    assert store.claim_next() == ("issue-1", "publish")
    assert store.get_issue("issue-1")["status"] == store.WORK_COMPLETED
    # Cut off mid-push: it goes back to publishing, not to implementing again
    assert store.requeue("issue-1", "worker restarted")
    assert store.get_issue("issue-1")["status"] == store.PUBLISH_QUEUED
