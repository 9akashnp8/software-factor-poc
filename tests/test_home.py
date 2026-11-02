from datetime import UTC, datetime, timedelta

from conftest import seed_issue
from software_factory import config, home, store


def keys(groups):
    return {g.key: [r.issue["identifier"] for r in g.rows] for g in groups}


def test_issues_are_grouped_by_who_acts_next():
    seed_issue(store.PLAN_READY, "a", "BEI-1", slot=1)
    seed_issue(store.NEEDS_ATTENTION, "b", "BEI-2", slot=2)
    seed_issue(store.IMPLEMENTING, "c", "BEI-3", slot=3)
    seed_issue(store.QUEUED, "d", "BEI-4", slot=None)
    seed_issue(store.DEPLOY_PENDING, "e", "BEI-5", slot=None)
    seed_issue(store.RELEASED, "f", "BEI-6", slot=None)

    grouped = keys(home.build_groups(store.list_issues()))

    # Blocked work comes before plans to review
    assert grouped == {
        "you": ["BEI-2", "BEI-1"],
        "run": ["BEI-3"],
        "queued": ["BEI-4"],
        "ship": ["BEI-5"],
        "done": ["BEI-6"],
    }


def test_every_status_has_a_look():
    assert set(store.REQUEUE_TO) <= set(home.STATUS_LOOK)
    assert set(store.REOPENABLE) <= set(home.STATUS_LOOK)
    assert set(store.ACTION_QUEUES.values()) <= set(home.STATUS_LOOK)
    assert {store.QUEUED, store.PLAN_READY, store.NEEDS_ATTENTION, store.PR_OPENED, store.MERGED} <= set(home.STATUS_LOOK)


def test_load_flags_a_full_review_queue(monkeypatch):
    monkeypatch.setattr(config, "MAX_PLANS_IN_REVIEW", 1)
    seed_issue(store.PLAN_READY)
    load = home.load(store.list_issues())
    assert (load["plans"], load["triage_paused"], load["slots"]) == (1, True, 1)


def test_ago_is_short():
    now = datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
    stamp = lambda **kw: (now - timedelta(**kw)).isoformat()
    assert home.ago(stamp(minutes=4), now) == "4m"
    assert home.ago(stamp(hours=3, minutes=20), now) == "3h20"
    assert home.ago(stamp(days=2, hours=5), now) == "2d"


def test_activity_marks_failures():
    seed_issue(store.DEPLOY_PENDING, slot=None)
    store.add_event("issue-1", "Deploy build 8885 failed: out of disk")
    first = home.activity()[0]
    assert first["identifier"] == "BEI-1"
    assert first["kind"] == "attn"
    assert "8885" in first["text"]


def test_every_status_has_a_place_in_the_flow():
    assert set(home.STATUS_LOOK) <= set(home.FLOW) | home.STOPPED | {store.NEEDS_ATTENTION}


def test_flow_marks_the_stage_waiting_on_someone():
    seed_issue(store.PLAN_READY)
    issue = store.get_issue("issue-1")
    flow = home.flow(issue, store.events("issue-1"))
    states = {s["key"]: s["state"] for s in flow["stages"]}
    assert states["analyzing"] == "past" and states["plan"] == "current" and states["implementing"] == "future"
    assert flow["linear"] == "todo" and not flow["blocked"]
    assert [b["span"] for b in flow["bands"]] == [3, 2, 1, 2]


def test_a_blocked_issue_keeps_its_place():
    seed_issue(store.IMPLEMENTING)
    store.transition("issue-1", store.NEEDS_ATTENTION, reason="Tests failed")
    flow = home.flow(store.get_issue("issue-1"), store.events("issue-1"))
    current = next(s for s in flow["stages"] if s["state"] == "blocked")
    assert current["key"] == "implementing" and current["kind"] == "attn"
    # Implementation had started, so Linear already says In Progress
    assert flow["linear"] == "in-progress" and flow["blocked"]
