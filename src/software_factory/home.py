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
    store.IMPLEMENT_QUEUED: ("run", "approved, queued", "run"),
    store.IMPLEMENTING: ("run", "implementing", "run"),
    store.REVIEWING: ("run", "reviewing", "run"),
    store.FIXING: ("run", "fixing review findings", "run"),
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
        store.IMPLEMENTING,
        store.REVIEWING,
        store.FIXING,
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
