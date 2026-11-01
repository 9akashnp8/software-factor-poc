"""Issue state in SQLite, shared by the CLI and the web app.

issues holds each issue's current state, events every status change, and runs every agent
session with its outcome and cost. Large artifacts (issue.md, plan.md, report.md,
attachments, conversation.json) stay as files in issue_dir(issue_id).
"""

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Absolute, so the CLI and the web app share state whatever directory they start in
WORKSPACE_DIR = Path(os.getenv("SF_WORKSPACE_DIR", "bug_workspace")).resolve()
DB_PATH = WORKSPACE_DIR / "factory.db"

GATHERING_CONTEXT = "gathering-context"
PLANNING = "gathered-context"
PLAN_READY = "plan-generated-for-review"
IMPLEMENTING = "plan-approved-agent-working"
REWORKING = "plan-rework-requested-agent-planning"
REVIEWING = "reviewing"
FIXING = "fixing-review-findings"
READY_FOR_PR = "ready-for-pr"
WORK_COMPLETED = "agent-work-completed"
PR_OPENED = "pr-opened"
NEEDS_ATTENTION = "needs-attention"

# Statuses a reviewer's action may start from. Claiming the issue with a compare-and-set
# on these is what keeps two clicks from starting two agents.
ACTIONS = {
    "approve": frozenset({PLAN_READY, NEEDS_ATTENTION}),
    "rework": frozenset({PLAN_READY, NEEDS_ATTENTION}),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    identifier TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT,
    status TEXT NOT NULL,
    session_id TEXT,
    pr_url TEXT,
    needs_attention_reason TEXT,
    plan_reviewed_by TEXT,
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
    error TEXT
);
"""

ISSUE_FIELDS = {"identifier", "title", "url", "session_id", "pr_url", "needs_attention_reason", "plan_reviewed_by"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        yield conn
    finally:
        conn.close()


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
        conn.execute(
            "INSERT INTO events (issue_id, at, from_status, to_status) VALUES (?, ?, ?, ?)",
            (issue_id, now, previous["status"] if previous else None, status),
        )
        conn.execute("COMMIT")


def get_issue(issue_id: str) -> sqlite3.Row | None:
    with _db() as conn:
        return conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()


def list_issues() -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute("SELECT * FROM issues ORDER BY updated_at DESC").fetchall()


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
        now = _now()
        conn.execute(
            "UPDATE issues SET status = ?, needs_attention_reason = ?, updated_at = ? WHERE id = ?",
            (to_status, reason if to_status == NEEDS_ATTENTION else None, now, issue_id),
        )
        conn.execute(
            "INSERT INTO events (issue_id, at, from_status, to_status, actor, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (issue_id, now, row["status"], to_status, actor, reason),
        )
        conn.execute("COMMIT")
        return True


def events(issue_id: str) -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute("SELECT * FROM events WHERE issue_id = ? ORDER BY id", (issue_id,)).fetchall()


def start_run(issue_id: str, phase: str, owner: str) -> int:
    with _db() as conn:
        cursor = conn.execute(
            "INSERT INTO runs (issue_id, phase, owner, started_at) VALUES (?, ?, ?, ?)",
            (issue_id, phase, owner, _now()),
        )
        return cursor.lastrowid


def finish_run(run_id: int, result=None, error: str | None = None):
    """Close a run with the agent's ResultMessage, if one came back, and any error."""
    with _db() as conn:
        conn.execute(
            """UPDATE runs SET ended_at = ?, session_id = ?, subtype = ?, is_error = ?, num_turns = ?,
                   cost_usd = ?, duration_ms = ?, result_text = ?, error = ? WHERE id = ?""",
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
                run_id,
            ),
        )


def runs(issue_id: str) -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute("SELECT * FROM runs WHERE issue_id = ? ORDER BY id", (issue_id,)).fetchall()


def reap_interrupted(owner: str) -> list[str]:
    """Close runs a crashed or restarted process left open and flag their issues.

    Call at startup, before this owner starts any run. Returns the flagged issue ids.
    """
    with _db() as conn:
        open_runs = conn.execute(
            "SELECT id, issue_id FROM runs WHERE owner = ? AND ended_at IS NULL", (owner,)
        ).fetchall()
    for run in open_runs:
        finish_run(run["id"], error="Interrupted: the process running it stopped")
        transition(run["issue_id"], NEEDS_ATTENTION, reason=f"Interrupted: the {owner} process restarted mid-run")
    return [run["issue_id"] for run in open_runs]


def allowed_actions(issue: sqlite3.Row) -> list[str]:
    """Reviewer actions for the issue's current status. Both resume the planning session."""
    if issue is None or not issue["session_id"] or not (issue_dir(issue["id"]) / "plan.md").exists():
        return []
    return [action for action, statuses in ACTIONS.items() if issue["status"] in statuses]
