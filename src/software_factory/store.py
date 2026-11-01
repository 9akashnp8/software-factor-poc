"""Issue state in SQLite, shared by the worker and the web app.

issues holds each issue's current state, events every status change, and runs every agent
session with its outcome and cost. Large artifacts (issue.md, plan.md, report.md,
attachments, conversation.json) stay as files in issue_dir(issue_id).

The worker is the only process that runs agents. It takes work from the queued statuses
with claim_next(); the web app only moves issues into them.
"""

import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from software_factory import config

# Absolute, so the worker and the web app share state whatever directory they start in
WORKSPACE_DIR = Path(os.getenv("SF_WORKSPACE_DIR", "bug_workspace")).resolve()
DB_PATH = WORKSPACE_DIR / "factory.db"

QUEUED = "queued"
GATHERING_CONTEXT = "gathering-context"
PLANNING = "gathered-context"
PLAN_READY = "plan-generated-for-review"
IMPLEMENT_QUEUED = "plan-approved-queued"
IMPLEMENTING = "plan-approved-agent-working"
REWORK_QUEUED = "plan-rework-queued"
REWORKING = "plan-rework-requested-agent-planning"
REVIEWING = "reviewing"
FIXING = "fixing-review-findings"
READY_FOR_PR = "ready-for-pr"
WORK_COMPLETED = "agent-work-completed"
PR_OPENED = "pr-opened"
# The PR conflicts with the base branch or its build failed; pending_feedback says which
UPDATE_QUEUED = "pr-update-queued"
UPDATING = "pr-updating-agent-working"
MERGED = "merged"
# Merged and cleaned up; waiting for the deploy pipeline to ship it
DEPLOY_PENDING = "merged-awaiting-deploy"
DEPLOYED = "deployed"
# The fix is committed; only the push and the PR are left
PUBLISH_QUEUED = "pr-open-queued"
RELEASE_QUEUED = "release-queued"
RELEASING = "releasing"
# Given up on: the worktree, branch and slot are gone, and the PR, if any, is left as it is
RELEASED = "released"
NEEDS_ATTENTION = "needs-attention"

# Statuses a reviewer's action may start from, and the queue each action puts the issue
# in. Claiming the issue with a compare-and-set on these keeps two clicks from queueing twice.
ACTIONS = {
    "approve": frozenset({PLAN_READY, NEEDS_ATTENTION}),
    "rework": frozenset({PLAN_READY, NEEDS_ATTENTION}),
    "retry": frozenset({NEEDS_ATTENTION}),
    "release": frozenset({QUEUED, PLAN_READY, NEEDS_ATTENTION}),
    "publish": frozenset({NEEDS_ATTENTION}),
}
ACTION_QUEUES = {
    "approve": IMPLEMENT_QUEUED,
    "rework": REWORK_QUEUED,
    "retry": QUEUED,
    "release": RELEASE_QUEUED,
    "publish": PUBLISH_QUEUED,
}

# What the worker picks up, in priority order: a person is waiting on the first four
JOBS = [
    (IMPLEMENT_QUEUED, IMPLEMENTING, "implement"),
    (PUBLISH_QUEUED, WORK_COMPLETED, "publish"),
    (REWORK_QUEUED, REWORKING, "rework"),
    (UPDATE_QUEUED, UPDATING, "update"),
    (RELEASE_QUEUED, RELEASING, "release"),
    (QUEUED, GATHERING_CONTEXT, "triage"),
]
# A finished issue that's back in Todo in Linear gets triaged again
REOPENABLE = frozenset({MERGED, DEPLOY_PENDING, DEPLOYED, RELEASED})
# Where an issue goes back to when its run is cut off, by the status it was running in
REQUEUE_TO = {
    GATHERING_CONTEXT: QUEUED,
    PLANNING: QUEUED,
    IMPLEMENTING: IMPLEMENT_QUEUED,
    REVIEWING: IMPLEMENT_QUEUED,
    FIXING: IMPLEMENT_QUEUED,
    READY_FOR_PR: IMPLEMENT_QUEUED,
    WORK_COMPLETED: PUBLISH_QUEUED,
    REWORKING: REWORK_QUEUED,
    UPDATING: UPDATE_QUEUED,
    RELEASING: RELEASE_QUEUED,
}
# Plans that are, or will soon be, waiting on a person's review
REVIEW_LOAD = frozenset({GATHERING_CONTEXT, PLANNING, PLAN_READY, REWORK_QUEUED, REWORKING})
# Reaching one of these ends a run of rate-limit retries
MILESTONES = frozenset({PLAN_READY, PR_OPENED, MERGED, DEPLOY_PENDING, DEPLOYED})

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    identifier TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT,
    status TEXT NOT NULL,
    session_id TEXT,
    pr_url TEXT,
    pr_id INTEGER,
    needs_attention_reason TEXT,
    plan_reviewed_by TEXT,
    pending_feedback TEXT,
    slot INTEGER UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    pr_conflict_key TEXT,
    pr_build_id INTEGER,
    update_rounds INTEGER NOT NULL DEFAULT 0,
    merged_at TEXT,
    merge_commit TEXT,
    deploy_build_seen INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(id),
    at TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(id),
    phase TEXT NOT NULL,
    owner TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    session_id TEXT,
    subtype TEXT,
    is_error INTEGER,
    num_turns INTEGER,
    cost_usd REAL,
    duration_ms INTEGER,
    result_text TEXT,
    error TEXT,
    rate_limited INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS factory_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first release, for databases created before them
_ADDED_COLUMNS = {
    "issues": {
        "pr_id": "INTEGER",
        "pending_feedback": "TEXT",
        "slot": "INTEGER",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "pr_conflict_key": "TEXT",
        "pr_build_id": "INTEGER",
        "update_rounds": "INTEGER NOT NULL DEFAULT 0",
        "merged_at": "TEXT",
        "merge_commit": "TEXT",
        "deploy_build_seen": "INTEGER",
    },
    "runs": {"rate_limited": "INTEGER NOT NULL DEFAULT 0"},
}

ISSUE_FIELDS = {
    "identifier", "title", "url", "session_id", "pr_url", "pr_id",
    "needs_attention_reason", "plan_reviewed_by", "pending_feedback",
    "pr_conflict_key", "pr_build_id", "merged_at", "merge_commit", "deploy_build_seen",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _add_missing_columns(conn: sqlite3.Connection):
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Autocommit, with explicit BEGIN IMMEDIATE where several statements must land together
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
        yield conn
    finally:
        conn.close()


def _event(conn, issue_id, from_status, to_status, actor=None, detail=None):
    conn.execute(
        "INSERT INTO events (issue_id, at, from_status, to_status, actor, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (issue_id, _now(), from_status, to_status, actor, detail),
    )


def issue_dir(issue_id: str) -> Path:
    return WORKSPACE_DIR / issue_id


def upsert_issue(issue_id: str, identifier: str, title: str = "", url: str | None = None, status: str = GATHERING_CONTEXT):
    """Create the issue, or refresh its Linear fields and restart it at status."""
    now = _now()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT status FROM issues WHERE id = ?", (issue_id,)).fetchone()
        conn.execute(
            """INSERT INTO issues (id, identifier, title, url, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET identifier = excluded.identifier, title = excluded.title,
                   url = excluded.url, status = excluded.status, needs_attention_reason = NULL,
                   updated_at = excluded.updated_at""",
            (issue_id, identifier, title, url, status, now, now),
        )
        _event(conn, issue_id, previous["status"] if previous else None, status)
        conn.execute("COMMIT")


def enqueue_new(issue_id: str, identifier: str, title: str = "", url: str | None = None) -> bool:
    """Queue a Linear issue for triage unless the factory has seen it before. Returns whether it was new."""
    now = _now()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """INSERT INTO issues (id, identifier, title, url, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING""",
            (issue_id, identifier, title, url, QUEUED, now, now),
        )
        if cursor.rowcount:
            _event(conn, issue_id, None, QUEUED)
        conn.execute("COMMIT")
        return bool(cursor.rowcount)


def get_issue(issue_id: str) -> sqlite3.Row | None:
    with _db() as conn:
        return conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()


def list_issues(status: str | None = None) -> list[sqlite3.Row]:
    with _db() as conn:
        if status is None:
            return conn.execute("SELECT * FROM issues ORDER BY updated_at DESC").fetchall()
        return conn.execute("SELECT * FROM issues WHERE status = ? ORDER BY updated_at", (status,)).fetchall()


def set_fields(issue_id: str, **fields):
    if unknown := set(fields) - ISSUE_FIELDS:
        raise ValueError(f"Unknown issue fields: {sorted(unknown)}")
    columns = ", ".join(f"{name} = ?" for name in fields)
    with _db() as conn:
        conn.execute(
            f"UPDATE issues SET {columns}, updated_at = ? WHERE id = ?",
            (*fields.values(), _now(), issue_id),
        )


def transition(
    issue_id: str,
    to_status: str,
    *,
    expected: Iterable[str] | None = None,
    actor: str | None = None,
    reason: str | None = None,
) -> bool:
    """Move the issue to to_status, only if it's currently in one of expected (any when None).

    reason is shown in the UI while the issue needs attention; other statuses clear it.
    Returns whether the issue moved.
    """
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None or (expected is not None and row["status"] not in set(expected)):
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """UPDATE issues SET status = ?, needs_attention_reason = ?, updated_at = ?,
                   attempts = CASE WHEN ? THEN 0 ELSE attempts END
               WHERE id = ?""",
            (
                to_status,
                reason if to_status == NEEDS_ATTENTION else None,
                _now(),
                to_status in MILESTONES,
                issue_id,
            ),
        )
        _event(conn, issue_id, row["status"], to_status, actor, reason)
        conn.execute("COMMIT")
        return True


def _free_slot_number(conn: sqlite3.Connection) -> int | None:
    taken = {row["slot"] for row in conn.execute("SELECT slot FROM issues WHERE slot IS NOT NULL")}
    return next((n for n in range(1, config.SLOT_COUNT + 1) if n not in taken), None)


def plans_in_review(conn: sqlite3.Connection | None = None) -> int:
    """Plans waiting for a person, counting the ones still being written or reworked."""
    if conn is None:
        with _db() as conn:
            return plans_in_review(conn)
    return conn.execute(
        f"SELECT COUNT(*) FROM issues WHERE status IN ({','.join('?' * len(REVIEW_LOAD))})",
        tuple(REVIEW_LOAD),
    ).fetchone()[0]


def claim_next() -> tuple[str, str] | None:
    """Take the next queued issue and move it to its running status.

    Returns (issue_id, job), job being "implement", "rework" or "triage", or None when
    nothing is waiting. Triage also takes a slot, so it waits while every slot is in use,
    and while config.MAX_PLANS_IN_REVIEW plans are already waiting for a person.
    """
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        free_slot = _free_slot_number(conn)
        review_full = plans_in_review(conn) >= config.MAX_PLANS_IN_REVIEW
        for queued, running, job in JOBS:
            if job == "triage" and review_full:
                continue
            row = conn.execute(
                """SELECT id, slot FROM issues WHERE status = ? AND (slot IS NOT NULL OR ?)
                   ORDER BY updated_at, rowid LIMIT 1""",
                (queued, job != "triage" or free_slot is not None),
            ).fetchone()
            if row is None:
                continue
            slot = row["slot"] if row["slot"] is not None or job != "triage" else free_slot
            conn.execute(
                "UPDATE issues SET status = ?, slot = ?, updated_at = ? WHERE id = ?",
                (running, slot, _now(), row["id"]),
            )
            _event(conn, row["id"], queued, running, "worker")
            conn.execute("COMMIT")
            return row["id"], job
        conn.execute("ROLLBACK")
        return None


def queue_update(issue_id: str, reason: str, **seen) -> bool:
    """Send an opened PR back to its agent with reason, recording what was seen (seen are PR fields).

    After config.MAX_UPDATE_ROUNDS rounds the issue needs attention instead, so a fix that
    keeps failing its build doesn't loop. Returns whether it was queued.
    """
    if unknown := set(seen) - {"pr_conflict_key", "pr_build_id"}:
        raise ValueError(f"Unknown PR fields: {sorted(unknown)}")
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status, update_rounds FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None or row["status"] != PR_OPENED:
            conn.execute("ROLLBACK")
            return False
        rounds = row["update_rounds"] + 1
        if rounds > config.MAX_UPDATE_ROUNDS:
            to_status, flagged = NEEDS_ATTENTION, f"{reason} (after {config.MAX_UPDATE_ROUNDS} update rounds)"
        else:
            to_status, flagged = UPDATE_QUEUED, None
        columns = "".join(f", {name} = ?" for name in seen)
        conn.execute(
            f"""UPDATE issues SET status = ?, update_rounds = ?, pending_feedback = ?,
                    needs_attention_reason = ?, updated_at = ?{columns} WHERE id = ?""",
            (to_status, rounds, reason, flagged, _now(), *seen.values(), issue_id),
        )
        _event(conn, issue_id, PR_OPENED, to_status, "worker", flagged or reason)
        conn.execute("COMMIT")
        return to_status == UPDATE_QUEUED


def reopen(issue_id: str) -> sqlite3.Row | None:
    """Queue a finished issue for triage again, e.g. when QA moved it back to Todo.

    Returns the issue as it was, for its PR link, or None if it isn't finished.
    """
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        before = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if before is None or before["status"] not in REOPENABLE:
            conn.execute("ROLLBACK")
            return None
        # A new attempt: new session, slot, worktree and PR
        conn.execute(
            """UPDATE issues SET status = ?, session_id = NULL, pr_url = NULL, pr_id = NULL,
                   pr_conflict_key = NULL, pr_build_id = NULL, update_rounds = 0, attempts = 0,
                   pending_feedback = NULL, needs_attention_reason = NULL, slot = NULL, updated_at = ?
               WHERE id = ?""",
            (QUEUED, _now(), issue_id),
        )
        _event(conn, issue_id, before["status"], QUEUED, "worker", "Back in Todo in Linear")
        conn.execute("COMMIT")
        return before


def find_issue(key: str) -> sqlite3.Row | None:
    """An issue by its Linear identifier (BEI-12, any case) or id."""
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM issues WHERE UPPER(identifier) = UPPER(?) OR id = ?", (key, key)
        ).fetchone()


def open_runs(issue_id: str | None = None) -> list[sqlite3.Row]:
    """Runs not yet finished, for one issue or all."""
    with _db() as conn:
        if issue_id is None:
            return conn.execute("SELECT * FROM runs WHERE ended_at IS NULL").fetchall()
        return conn.execute("SELECT * FROM runs WHERE issue_id = ? AND ended_at IS NULL", (issue_id,)).fetchall()


def restart(issue_id: str, actor: str) -> bool:
    """Queue the issue for triage from scratch: no session, slot, PR or retry counts.

    The caller tears down the slot first. Returns whether the issue exists.
    """
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        before = conn.execute("SELECT status FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if before is None:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """UPDATE issues SET status = ?, session_id = NULL, pr_url = NULL, pr_id = NULL,
                   pr_conflict_key = NULL, pr_build_id = NULL, update_rounds = 0, attempts = 0,
                   pending_feedback = NULL, needs_attention_reason = NULL, plan_reviewed_by = NULL,
                   slot = NULL, updated_at = ?
               WHERE id = ?""",
            (QUEUED, _now(), issue_id),
        )
        _event(conn, issue_id, before["status"], QUEUED, actor, "Reset: starting over")
        conn.execute("COMMIT")
        return True


def back_to_plan_review(issue_id: str, actor: str) -> bool:
    """Put the issue back on its plan's review, keeping its worktree, slot and session."""
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        before = conn.execute("SELECT status FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if before is None:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """UPDATE issues SET status = ?, attempts = 0, update_rounds = 0, pending_feedback = NULL,
                   needs_attention_reason = NULL, updated_at = ? WHERE id = ?""",
            (PLAN_READY, _now(), issue_id),
        )
        _event(conn, issue_id, before["status"], PLAN_READY, actor, "Reset: back to plan review")
        conn.execute("COMMIT")
        return True


def free_slot(issue_id: str):
    with _db() as conn:
        conn.execute("UPDATE issues SET slot = NULL, updated_at = ? WHERE id = ?", (_now(), issue_id))


def requeue(issue_id: str, reason: str) -> bool:
    """Put an issue whose run was cut off back on its queue.

    After config.MAX_ATTEMPTS requeues without reaching a milestone it needs attention
    instead. Returns whether it was requeued.
    """
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status, attempts FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None or row["status"] not in REQUEUE_TO:
            conn.execute("ROLLBACK")
            return False
        attempts = row["attempts"] + 1
        if attempts > config.MAX_ATTEMPTS:
            to_status, flagged = NEEDS_ATTENTION, f"{reason} (gave up after {config.MAX_ATTEMPTS} retries)"
        else:
            to_status, flagged = REQUEUE_TO[row["status"]], None
        conn.execute(
            "UPDATE issues SET status = ?, attempts = ?, needs_attention_reason = ?, updated_at = ? WHERE id = ?",
            (to_status, attempts, flagged, _now(), issue_id),
        )
        _event(conn, issue_id, row["status"], to_status, "worker", flagged or reason)
        conn.execute("COMMIT")
        return to_status != NEEDS_ATTENTION


def pause_until(timestamp: float):
    """Stop the worker starting runs until timestamp (Unix seconds), e.g. a rate limit's reset."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO factory_state (key, value) VALUES ('paused_until', ?) "
            # Both cast: value is TEXT, and SQLite ranks any text above any number
            "ON CONFLICT(key) DO UPDATE SET value = MAX(CAST(value AS REAL), CAST(excluded.value AS REAL))",
            (float(timestamp),),
        )


def clear_pause():
    with _db() as conn:
        conn.execute("DELETE FROM factory_state WHERE key = 'paused_until'")


def paused_until() -> float | None:
    """The pause's end, while one is in force."""
    with _db() as conn:
        row = conn.execute("SELECT value FROM factory_state WHERE key = 'paused_until'").fetchone()
    if row is None or float(row["value"]) <= time.time():
        return None
    return float(row["value"])


def events(issue_id: str) -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute("SELECT * FROM events WHERE issue_id = ? ORDER BY id", (issue_id,)).fetchall()


def recent_events(limit: int) -> list[sqlite3.Row]:
    """The newest events across all issues, with each issue's identifier."""
    with _db() as conn:
        return conn.execute(
            """SELECT e.*, i.identifier FROM events e JOIN issues i ON i.id = e.issue_id
               ORDER BY e.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def add_event(issue_id: str, detail: str, actor: str = "worker"):
    """Record something that happened to the issue without changing its status."""
    with _db() as conn:
        status = conn.execute("SELECT status FROM issues WHERE id = ?", (issue_id,)).fetchone()["status"]
        _event(conn, issue_id, status, status, actor, detail)


def add_event_once(issue_id: str, detail: str, actor: str = "worker"):
    """add_event, unless the issue's latest event already says the same, e.g. a retried failure."""
    with _db() as conn:
        last = conn.execute(
            "SELECT detail FROM events WHERE issue_id = ? ORDER BY id DESC LIMIT 1", (issue_id,)
        ).fetchone()
    if last is None or last["detail"] != detail:
        add_event(issue_id, detail, actor)


def start_run(issue_id: str, phase: str, owner: str) -> int:
    with _db() as conn:
        cursor = conn.execute(
            "INSERT INTO runs (issue_id, phase, owner, started_at) VALUES (?, ?, ?, ?)",
            (issue_id, phase, owner, _now()),
        )
        return cursor.lastrowid


def finish_run(run_id: int, result=None, error: str | None = None, rate_limited: bool = False):
    """Close a run with the agent's ResultMessage, if one came back, and any error."""
    with _db() as conn:
        conn.execute(
            """UPDATE runs SET ended_at = ?, session_id = ?, subtype = ?, is_error = ?, num_turns = ?,
                   cost_usd = ?, duration_ms = ?, result_text = ?, error = ?, rate_limited = ? WHERE id = ?""",
            (
                _now(),
                getattr(result, "session_id", None),
                getattr(result, "subtype", None),
                getattr(result, "is_error", None),
                getattr(result, "num_turns", None),
                getattr(result, "total_cost_usd", None),
                getattr(result, "duration_ms", None),
                getattr(result, "result", None),
                error,
                rate_limited,
                run_id,
            ),
        )


def runs(issue_id: str) -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute("SELECT * FROM runs WHERE issue_id = ? ORDER BY id", (issue_id,)).fetchall()


def requeue_orphans() -> list[str]:
    """Put issues left in a running status back on their queues.

    Only the worker runs agents, so at its startup nothing can still be running. Call before
    it claims any work. Returns the requeued issue ids.
    """
    with _db() as conn:
        open_runs = conn.execute("SELECT id FROM runs WHERE ended_at IS NULL").fetchall()
        orphans = conn.execute(
            f"SELECT id FROM issues WHERE status IN ({','.join('?' * len(REQUEUE_TO))})",
            tuple(REQUEUE_TO),
        ).fetchall()
    for run in open_runs:
        finish_run(run["id"], error="Interrupted: the worker stopped")
    return [row["id"] for row in orphans if requeue(row["id"], "Interrupted: the worker restarted")]


def allowed_actions(issue: sqlite3.Row) -> list[str]:
    """Reviewer actions for the issue's current status."""
    if issue is None:
        return []
    has_plan = bool(issue["session_id"]) and (issue_dir(issue["id"]) / "plan.md").exists()
    actions = []
    if has_plan:
        actions += [a for a in ("approve", "rework") if issue["status"] in ACTIONS[a]]
    elif issue["status"] in ACTIONS["retry"]:
        actions.append("retry")
    # A successful implementation wrote its report; publishing reuses that commit
    if issue["status"] in ACTIONS["publish"] and (issue_dir(issue["id"]) / "report.md").exists():
        actions.append("publish")
    if issue["status"] in ACTIONS["release"]:
        actions.append("release")
    return actions
