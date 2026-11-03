from datetime import UTC, datetime, timedelta

from conftest import seed_issue
from software_factory import metrics, store

NOW = datetime.now(UTC)


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def history(issue_id: str, *steps):
    """Replace the issue's events with steps: (hours ago, from, to, detail)."""
    with store._db() as conn:
        conn.execute("DELETE FROM events WHERE issue_id = ?", (issue_id,))
        for hours, from_status, to_status, detail in steps:
            conn.execute(
                "INSERT INTO events (issue_id, at, from_status, to_status, actor, detail) VALUES (?, ?, ?, ?, 'worker', ?)",
                (issue_id, at(hours), from_status, to_status, detail),
            )


def test_rework_verification_and_bugs_that_came_back():
    seed_issue(store.PR_OPENED, reproduction="reproduced")
    history(
        "issue-1",
        (50, None, store.QUEUED, None),
        (49, store.QUEUED, store.GATHERING_CONTEXT, None),
        (48, store.PLANNING, store.PLAN_READY, None),
        (40, store.PLAN_READY, store.REWORK_QUEUED, None),
        (39, store.REWORKING, store.PLAN_READY, None),
        (30, store.PLAN_READY, store.IMPLEMENT_QUEUED, None),
        (29, store.VERIFYING, store.VERIFYING, "Verification failed: still wrong. Sent back to the implementer."),
        (28, store.VERIFYING, store.VERIFYING, "Verification passed"),
        (27, store.WORK_COMPLETED, store.PR_OPENED, None),
    )
    seed_issue(store.QUEUED, issue_id="issue-2", identifier="BEI-2", slot=2, reproduction="not_reproduced")
    history(
        "issue-2",
        (10, store.PLAN_READY, store.NEEDS_ATTENTION, "Verification failed: The implementer couldn't verify the fix."),
        (5, store.DEPLOYED, store.QUEUED, "Back in Todo in Linear"),
        (4, store.PLAN_READY, store.PLAN_READY, "QA question posted to Linear"),
    )

    m = metrics.summary(30)
    assert (m["issues"], m["plans"], m["reworks"], m["rework_rate"]) == (2, 2, 1, 0.5)
    assert (m["approved"], m["approved_first_time"]) == (1, 0)
    assert (m["verification_passed"], m["verification_failed"]) == (1, 2)
    assert (m["came_back"], m["qa_questions"], m["prs"]) == (1, 1, 1)
    assert m["reproduction"] == {"reproduced": 1, "not_reproduced": 1}
    assert m["attention"] == [("Verification failed", 1)]

    stages = {s.label: s for s in m["stages"]}
    assert stages["Triage to plan"].count == 1 and stages["Triage to plan"].median == timedelta(hours=1)
    # From the first plan to its first decision, the rework
    assert stages["Plan waiting for review"].median == timedelta(hours=8)
    assert stages["Approval to PR"].median == timedelta(hours=3)
    assert stages["PR waiting for merge"].count == 0


def test_the_window_leaves_older_issues_out():
    seed_issue(store.PLAN_READY)
    history("issue-1", (24 * 40, store.PLANNING, store.PLAN_READY, None))
    assert metrics.summary(30)["issues"] == 0
    assert metrics.summary(90)["plans"] == 1


def test_runs_by_phase():
    seed_issue(store.IMPLEMENTING)
    run = store.start_run("issue-1", "implement", "worker")
    store.finish_run(run, error="crashed")
    with store._db() as conn:
        conn.execute("UPDATE runs SET cost_usd = 2.5, duration_ms = 600000")
    (implement,) = metrics.summary(7)["runs"]
    assert implement == {"phase": "implement", "count": 1, "failed": 1, "cost": 2.5, "median_minutes": 10.0}


def test_duration_reads_naturally():
    assert metrics.duration(None) == "—"
    assert metrics.duration(timedelta(minutes=45)) == "45m"
    assert metrics.duration(timedelta(hours=3, minutes=30)) == "3.5h"
    assert metrics.duration(timedelta(days=2, hours=2)) == "2.1d"
