from types import SimpleNamespace

from conftest import seed_issue
from software_factory import store


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


def test_reap_flags_only_this_owners_open_runs():
    seed_issue(store.IMPLEMENTING)
    seed_issue(store.PLANNING, issue_id="issue-2", identifier="BEI-2")
    web_run = store.start_run("issue-1", "implement", "web")
    store.start_run("issue-2", "plan", "cli")

    assert store.reap_interrupted("web") == ["issue-1"]
    assert store.get_issue("issue-1")["status"] == store.NEEDS_ATTENTION
    assert "restarted" in store.get_issue("issue-1")["needs_attention_reason"]
    assert store.runs("issue-1")[0]["ended_at"] is not None and store.runs("issue-1")[0]["id"] == web_run
    assert store.get_issue("issue-2")["status"] == store.PLANNING


def test_allowed_actions_need_a_plan_and_session():
    ws = seed_issue(store.PLAN_READY)
    assert store.allowed_actions(store.get_issue("issue-1")) == []
    (ws / "plan.md").write_text("# Plan")
    assert store.allowed_actions(store.get_issue("issue-1")) == ["approve", "rework"]
    store.transition("issue-1", store.PR_OPENED)
    assert store.allowed_actions(store.get_issue("issue-1")) == []
