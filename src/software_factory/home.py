"""What the home page shows: issues grouped by who acts next, plus the factory's load.

Pure functions over store rows, so the grouping can be tested without rendering.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from software_factory import config, store

# Each status: the colour family it is drawn in, the label shown, and the group it sits in
STATUS_LOOK = {
    store.NEEDS_ATTENTION: ("attn", "needs attention", "you"),
    store.PLAN_READY: ("review", "plan ready", "you"),
    store.GATHERING_CONTEXT: ("run", "gathering context", "run"),
    store.PLANNING: ("run", "planning", "run"),
    store.REWORK_QUEUED: ("run", "rework queued", "run"),
    store.REWORKING: ("run", "reworking plan", "run"),
    store.REFRESH_QUEUED: ("run", "refresh queued", "run"),
    store.REFRESHING: ("run", "refreshing plan", "run"),
    store.IMPLEMENT_QUEUED: ("run", "approved, queued", "run"),
    store.IMPLEMENTING: ("run", "implementing", "run"),
    store.REVIEWING: ("run", "reviewing", "run"),
    store.FIXING: ("run", "fixing review findings", "run"),
    store.VERIFYING: ("run", "verifying the fix", "run"),
    store.READY_FOR_PR: ("run", "ready for PR", "run"),
    store.WORK_COMPLETED: ("run", "work completed", "run"),
    store.PUBLISH_QUEUED: ("run", "PR queued", "run"),
    store.UPDATE_QUEUED: ("run", "PR update queued", "run"),
    store.UPDATING: ("run", "updating PR", "run"),
    store.RELEASE_QUEUED: ("run", "release queued", "run"),
    store.RELEASING: ("run", "releasing", "run"),
    store.QUEUED: ("queued", "queued", "queued"),
    store.PR_OPENED: ("ship", "PR open", "ship"),
    store.MERGED: ("ship", "merged", "ship"),
    store.DEPLOY_PENDING: ("ship", "awaiting deploy", "ship"),
    store.DEPLOYED: ("done", "deployed", "done"),
    store.RELEASED: ("done", "released", "done"),
}
# An agent is actually running in these; the rest of the "run" group is waiting to be picked up
AGENT_RUNNING = frozenset(
    {
        store.GATHERING_CONTEXT,
        store.PLANNING,
        store.REWORKING,
        store.REFRESHING,
        store.IMPLEMENTING,
        store.REVIEWING,
        store.FIXING,
        store.VERIFYING,
        store.READY_FOR_PR,
        store.WORK_COMPLETED,
        store.UPDATING,
        store.RELEASING,
    }
)
# What the reviewer does next, by status
NEXT_STEP = {
    store.NEEDS_ATTENTION: "Open",
    store.PLAN_READY: "Review plan",
}

# (key, name, hint, rows kept visible before "show more", None = all)
GROUPS = [
    ("you", "Needs you", "Blocked runs, then plans to review, oldest first", None),
    ("run", "Agent working", "", None),
    ("queued", "Queued", "In the order the worker picks them up", 5),
    ("ship", "Shipping", "PR open, merged or waiting on the deploy pipeline", None),
    ("done", "Done", "Deployed or released", 0),
]
KIND_OF_GROUP = {"you": "attn", "run": "run", "queued": "queued", "ship": "ship", "done": "done"}

# The path every issue takes, from QA's report to QA's review: (key, label, who acts, Linear state
# once the stage is reached). The issue page draws it with the stage waiting on its actor current.
STAGES = [
    ("logged", "Logged", "QA", "todo"),
    ("analyzing", "Analyzing", "Agent", "todo"),
    ("plan", "Plan review", "You", "todo"),
    ("implementing", "Implementing", "Agent", "in-progress"),
    ("pr", "Draft PR", "Agent", "in-progress"),
    ("merged", "Merged", "Reviewer", "pending-deployment"),
    ("deployed", "Deployed", "Pipeline", "in-review"),
    ("qa", "QA review", "QA", "in-review"),
]
ACTOR_KIND = {"You": "review", "Reviewer": "review", "Agent": "run", "Pipeline": "ship", "QA": "ship"}
LINEAR_STATES = {
    "todo": config.LINEAR_TODO_STATE,
    "in-progress": config.LINEAR_IN_PROGRESS_STATE,
    "pending-deployment": config.LINEAR_PENDING_DEPLOY_STATE,
    "in-review": config.LINEAR_MERGED_STATE,
}
# Each status: the stage waiting on someone, and the state the worker has set in Linear by then
FLOW = {
    store.QUEUED: ("analyzing", "todo"),
    store.GATHERING_CONTEXT: ("analyzing", "todo"),
    store.PLANNING: ("analyzing", "todo"),
    store.PLAN_READY: ("plan", "todo"),
    store.REWORK_QUEUED: ("analyzing", "todo"),
    store.REWORKING: ("analyzing", "todo"),
    store.REFRESH_QUEUED: ("analyzing", "todo"),
    store.REFRESHING: ("analyzing", "todo"),
    store.IMPLEMENT_QUEUED: ("implementing", "todo"),
    store.IMPLEMENTING: ("implementing", "in-progress"),
    store.REVIEWING: ("implementing", "in-progress"),
    store.FIXING: ("implementing", "in-progress"),
    store.VERIFYING: ("implementing", "in-progress"),
    store.READY_FOR_PR: ("pr", "in-progress"),
    store.WORK_COMPLETED: ("pr", "in-progress"),
    store.PUBLISH_QUEUED: ("pr", "in-progress"),
    store.UPDATE_QUEUED: ("pr", "in-progress"),
    store.UPDATING: ("pr", "in-progress"),
    store.PR_OPENED: ("merged", "in-progress"),
    store.DEPLOY_PENDING: ("deployed", "pending-deployment"),
    # Without a deploy pipeline a merge goes straight to QA
    store.MERGED: ("qa", "in-review"),
    store.DEPLOYED: ("qa", "in-review"),
}
STOPPED = frozenset({store.RELEASE_QUEUED, store.RELEASING, store.RELEASED})
ACTIVITY_LIMIT = 12
# Build errors run to hundreds of characters; the issue page has the rest
ACTIVITY_TEXT_MAX = 80


@dataclass
class Row:
    issue: sqlite3.Row
    kind: str
    label: str
    reason: str
    next_step: str
    ago: str


@dataclass
class Group:
    key: str
    name: str
    kind: str
    hint: str
    peek: int | None
    rows: list[Row] = field(default_factory=list)


def ago(iso: str, now: datetime | None = None) -> str:
    """'4m', '3h20' or '2d': how long since iso, in the shortest useful form."""
    now = now or datetime.now(UTC)
    minutes = max(0, int((now - datetime.fromisoformat(iso)).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 60 * 24:
        return f"{minutes // 60}h{minutes % 60:02d}"
    return f"{minutes // (60 * 24)}d"


def look(status: str) -> tuple[str, str]:
    """(kind, label) a status is drawn with."""
    kind, label, _ = STATUS_LOOK.get(status, ("queued", status, "queued"))
    return kind, label


def flow(issue: sqlite3.Row, events: list[sqlite3.Row]) -> dict:
    """The issue's place in STAGES and the Linear state that goes with it, for the issue page.

    A blocked or released issue keeps the place of the last status before it.
    """
    status = issue["status"]
    if status not in FLOW:
        status = next((e["to_status"] for e in reversed(events) if e["to_status"] in FLOW), store.QUEUED)
    at_key, linear = FLOW[status]
    at = next(i for i, s in enumerate(STAGES) if s[0] == at_key)
    blocked = issue["status"] == store.NEEDS_ATTENTION
    stages = []
    for i, (key, label, actor, _) in enumerate(STAGES):
        state = "past" if i < at else "future" if i > at else "blocked" if blocked else "current"
        kind = {"past": "done", "future": "queued", "blocked": "attn"}.get(state, ACTOR_KIND[actor])
        stages.append({"key": key, "label": label, "actor": "Blocked" if state == "blocked" else actor, "state": state, "kind": kind})
    bands = []
    for _, _, _, state in STAGES:
        if bands and bands[-1]["state"] == state:
            bands[-1]["span"] += 1
        else:
            bands.append({"state": state, "label": LINEAR_STATES[state], "span": 1, "current": state == linear})
    return {
        "stages": stages,
        "bands": bands,
        "linear": linear,
        "linear_label": LINEAR_STATES[linear],
        "blocked": blocked,
        "stopped": issue["status"] in STOPPED,
    }


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def build_groups(issues: list[sqlite3.Row], now: datetime | None = None) -> list[Group]:
    groups = {key: Group(key, name, KIND_OF_GROUP[key], hint, peek) for key, name, hint, peek in GROUPS}
    for issue in issues:
        kind, label, key = STATUS_LOOK.get(issue["status"], ("queued", issue["status"], "queued"))
        groups[key].rows.append(
            Row(
                issue=issue,
                kind=kind,
                label=label,
                reason=_first_line(issue["needs_attention_reason"]) if issue["status"] == store.NEEDS_ATTENTION else "",
                next_step=NEXT_STEP.get(issue["status"], ""),
                ago=ago(issue["updated_at"], now),
            )
        )
    # Blocked before plans to review, then oldest first: whatever has waited longest is on top
    groups["you"].rows.sort(key=lambda r: (r.kind != "attn", r.issue["updated_at"]))
    groups["queued"].rows.sort(key=lambda r: r.issue["updated_at"])
    for key in ("run", "ship", "done"):
        groups[key].rows.sort(key=lambda r: r.issue["updated_at"], reverse=True)
    return list(groups.values())


def load(issues: list[sqlite3.Row]) -> dict:
    """The worker's capacity, for the header chips."""
    running = sum(1 for i in issues if i["status"] in AGENT_RUNNING)
    plans = store.plans_in_review()
    return {
        "runs": running,
        "max_runs": config.MAX_CONCURRENT_RUNS,
        "slots": sum(1 for i in issues if i["slot"] is not None),
        "max_slots": config.SLOT_COUNT,
        "plans": plans,
        "max_plans": config.MAX_PLANS_IN_REVIEW,
        "triage_paused": plans >= config.MAX_PLANS_IN_REVIEW,
    }


def activity(now: datetime | None = None) -> list[dict]:
    """The latest status changes and notes across all issues, newest first."""
    out = []
    for event in store.recent_events(ACTIVITY_LIMIT):
        kind, label, _ = STATUS_LOOK.get(event["to_status"], ("queued", event["to_status"], ""))
        detail = _first_line(event["detail"])
        if len(detail) > ACTIVITY_TEXT_MAX:
            detail = detail[: ACTIVITY_TEXT_MAX - 1] + "…"
        changed = event["from_status"] != event["to_status"]
        text = f"{label}: {detail}" if changed and detail else label if changed else detail
        failed = "failed" in detail.lower()
        out.append(
            {
                "identifier": event["identifier"],
                "issue_id": event["issue_id"],
                "kind": "attn" if failed else kind,
                "text": text,
                "ago": ago(event["at"], now),
            }
        )
    return out
