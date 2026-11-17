"""Reviewers' questions about a plan and the planner's answers, in the workspace's questions.json.

Asking doesn't change the plan: the planner answers in its own session, read-only, and the
plan comes back to review as it was. The reviewer then approves, asks again, or sends it
for rework. Only the worker writes the file, after the answering run.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from software_factory import store

FILE = "questions.json"


def load(ws: Path) -> list[dict]:
    """Every question asked about this issue's plans, oldest first."""
    path = ws / FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def asked_by(events) -> str | None:
    """Who asked the question waiting for an answer: whoever put the issue on the ask queue."""
    return next((e["actor"] for e in reversed(events) if e["to_status"] == store.ASK_QUEUED), None)


def record(ws: Path, question: str, asked_by: str | None, answer: str | None, run_id: int | None = None):
    """Add a question and its answer; answer None means the planner couldn't answer it."""
    entries = load(ws)
    entries.append(
        {
            "question": question,
            "asked_by": asked_by,
            "answer": answer,
            "run_id": run_id,
            "answered_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    (ws / FILE).write_text(json.dumps(entries, indent=2), encoding="utf-8")
