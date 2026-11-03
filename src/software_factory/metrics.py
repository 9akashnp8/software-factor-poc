"""How well the factory is doing, from its own history: the events and runs in SQLite.

Rework, verification failures and bugs coming back say whether its plans and fixes hold
up; stage durations say where issues wait, and on whom.
"""

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from software_factory import store

# Detail texts the pipeline records, which the counts below look for
VERIFICATION_PASSED = "Verification passed"
VERIFICATION_FAILED = "Verification failed"
CAME_BACK = "Back in Todo in Linear"
QA_QUESTION = "QA question posted to Linear"
MERGED_STATUSES = frozenset({store.MERGED, store.DEPLOY_PENDING})

# (label, the status it starts at, the statuses that end it). Each is measured from the
# first time an issue reached the start to the first end after it.
STAGES = [
    ("Triage to plan", store.GATHERING_CONTEXT, {store.PLAN_READY}),
    ("Plan waiting for review", store.PLAN_READY, {store.IMPLEMENT_QUEUED, store.REWORK_QUEUED}),
    ("Approval to PR", store.IMPLEMENT_QUEUED, {store.PR_OPENED}),
    ("PR waiting for merge", store.PR_OPENED, MERGED_STATUSES),
    ("Merge to deploy", store.DEPLOY_PENDING, {store.DEPLOYED}),
]


@dataclass
class Stage:
    label: str
    count: int
    median: timedelta | None
    p90: timedelta | None


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def _events(since: str) -> list:
    """Every event of the issues that had any event since then, oldest first."""
    with store._db() as conn:
        return conn.execute(
            """SELECT e.*, i.reproduction FROM events e JOIN issues i ON i.id = e.issue_id
               WHERE e.issue_id IN (SELECT DISTINCT issue_id FROM events WHERE at >= ?)
               ORDER BY e.id""",
            (since,),
        ).fetchall()


def _runs(since: str) -> list:
    with store._db() as conn:
        return conn.execute("SELECT * FROM runs WHERE started_at >= ? ORDER BY id", (since,)).fetchall()


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def stages(events: list, since: str) -> list[Stage]:
    """How long issues took through each stage, for the stages that ended in the window."""
    by_issue = defaultdict(list)
    for event in events:
        by_issue[event["issue_id"]].append(event)
    found = []
    for label, start, ends in STAGES:
        spans = []
        for history in by_issue.values():
            began = next((e for e in history if e["to_status"] == start and e["from_status"] != start), None)
            if began is None:
                continue
            ended = next((e for e in history if e["id"] > began["id"] and e["to_status"] in ends), None)
            if ended is not None and ended["at"] >= since:
                spans.append((_at(ended["at"]) - _at(began["at"])).total_seconds())
        found.append(
            Stage(
                label,
                len(spans),
                timedelta(seconds=statistics.median(spans)) if spans else None,
                timedelta(seconds=_percentile(spans, 0.9)) if spans else None,
            )
        )
    return found


def summary(days: int = 30) -> dict:
    since = _since(days)
    events = _events(since)
    recent = [e for e in events if e["at"] >= since]
    changed = [e for e in recent if e["from_status"] != e["to_status"]]

    def entered(status) -> int:
        return sum(1 for e in changed if e["to_status"] == status)

    plans = entered(store.PLAN_READY)
    reworks = entered(store.REWORK_QUEUED)
    approved = [e for e in changed if e["to_status"] == store.IMPLEMENT_QUEUED]
    reworked_issues = {e["issue_id"] for e in events if e["to_status"] == store.REWORK_QUEUED}
    verification_failed = [e for e in recent if (e["detail"] or "").startswith(VERIFICATION_FAILED)]
    attention = Counter(
        (e["detail"] or "no reason").split(":", 1)[0].split("\n", 1)[0][:80]
        for e in changed
        if e["to_status"] == store.NEEDS_ATTENTION
    )
    reproduction = Counter(
        e["reproduction"] for e in {e["issue_id"]: e for e in recent}.values() if e["reproduction"]
    )

    runs = _runs(since)
    by_phase = defaultdict(list)
    for run in runs:
        by_phase[run["phase"]].append(run)

    return {
        "days": days,
        "issues": len({e["issue_id"] for e in recent}),
        "plans": plans,
        "reworks": reworks,
        "rework_rate": reworks / plans if plans else None,
        "approved": len(approved),
        "approved_first_time": sum(1 for e in approved if e["issue_id"] not in reworked_issues),
        "prs": entered(store.PR_OPENED),
        "merged": sum(1 for e in changed if e["to_status"] in MERGED_STATUSES and e["from_status"] not in MERGED_STATUSES),
        "verification_passed": sum(1 for e in recent if e["detail"] == VERIFICATION_PASSED),
        "verification_failed": len(verification_failed),
        "came_back": sum(1 for e in recent if e["detail"] == CAME_BACK),
        "qa_questions": sum(1 for e in recent if e["detail"] == QA_QUESTION),
        "reproduction": dict(reproduction),
        "attention": attention.most_common(8),
        "stages": stages(events, since),
        "runs": [
            {
                "phase": phase,
                "count": len(group),
                "failed": sum(1 for r in group if r["error"] or r["is_error"]),
                "cost": sum(r["cost_usd"] or 0 for r in group),
                "median_minutes": statistics.median([r["duration_ms"] / 60000 for r in group if r["duration_ms"]])
                if any(r["duration_ms"] for r in group)
                else None,
            }
            for phase, group in sorted(by_phase.items())
        ],
        "cost": sum(r["cost_usd"] or 0 for r in runs),
    }


def duration(value: timedelta | None) -> str:
    """A duration as people read it: 45m, 3.5h, 2.1d."""
    if value is None:
        return "—"
    minutes = value.total_seconds() / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    return f"{hours:.1f}h" if hours < 36 else f"{hours / 24:.1f}d"
