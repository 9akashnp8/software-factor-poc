"""What the planner found when it tried to reproduce the bug, in the workspace's reproduction.json.

The planner writes the file before it presents its plan; ExitPlanMode is refused until the
file is valid. A bug that didn't reproduce, or that turned out to be the code working as
designed, gets findings instead of a fix plan, and can't be approved: the reviewer posts the
question or the explanation to QA, or answers the planner's question for them as rework.
"""

import json
from pathlib import Path

FILE = "reproduction.json"

REPRODUCED = "reproduced"
NOT_REPRODUCED = "not_reproduced"
# What QA saw is real, and it's what the code is meant to do
WORKS_AS_DESIGNED = "works_as_designed"
NOT_ATTEMPTED = "not_attempted"
STATUSES = (REPRODUCED, NOT_REPRODUCED, WORKS_AS_DESIGNED, NOT_ATTEMPTED)
# The outcomes with findings instead of a fix to approve
NO_FIX = frozenset({NOT_REPRODUCED, WORKS_AS_DESIGNED})
# logs and code: a diagnosis from the app's logs, or from reading the code, with nothing run
METHODS = ("test", "replay", "browser", "api", "logs", "code", "manual")

# What the planner is told to write, quoted in its prompt and in refusals
SHAPE = """{
  "status": "reproduced" | "not_reproduced" | "works_as_designed" | "not_attempted",
  "method": "test" | "replay" | "browser" | "api" | "logs" | "code" | "manual",
  "steps": ["what you ran or did, in order"],
  "evidence": "a short excerpt of the output that shows the bug, or that it didn't happen",
  "reason": "required for not_attempted: why it couldn't be tried",
  "qa_question": "for not_reproduced: what to ask QA, in plain words (required unless reviewer_ask is given)",
  "reviewer_ask": "optional: what a developer can check that you couldn't, with the exact commands or queries",
  "explanation": "required for works_as_designed: for QA, in plain words, what happened and why it's correct",
  "follow_ups": ["optional: real problems the report points at that aren't the reported bug"]
}"""


def problems(data) -> list[str]:
    """What's wrong with a reproduction record; empty when it's valid."""
    if not isinstance(data, dict):
        return ["it must be a JSON object"]
    found = []
    status = data.get("status")
    if status not in STATUSES:
        found.append(f"status must be one of {', '.join(STATUSES)}")
    if status != NOT_ATTEMPTED and data.get("method") not in METHODS:
        found.append(f"method must be one of {', '.join(METHODS)}")
    steps = data.get("steps")
    if status != NOT_ATTEMPTED and not _texts(steps):
        found.append("steps must be a non-empty list of strings")
    if status != NOT_ATTEMPTED and not _text(data.get("evidence")):
        found.append("evidence is required")
    if status == NOT_ATTEMPTED and not _text(data.get("reason")):
        found.append("reason is required when the status is not_attempted")
    if status == NOT_REPRODUCED and not (_text(data.get("qa_question")) or _text(data.get("reviewer_ask"))):
        found.append("qa_question or reviewer_ask is required when the status is not_reproduced")
    if status == WORKS_AS_DESIGNED and not _text(data.get("explanation")):
        found.append("explanation is required when the status is works_as_designed")
    if "reviewer_ask" in data and data["reviewer_ask"] is not None and not isinstance(data["reviewer_ask"], str):
        found.append("reviewer_ask must be a string")
    follow_ups = data.get("follow_ups")
    if follow_ups is not None and not (isinstance(follow_ups, list) and all(isinstance(f, str) for f in follow_ups)):
        found.append("follow_ups must be a list of strings")
    return found


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _texts(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(s, str) for s in value)


def check(ws: Path) -> str | None:
    """Why the planner can't present its plan yet, or None when reproduction.json is valid."""
    path = ws / FILE
    if not path.is_file():
        return f"Write {path} before presenting the plan, shaped like:\n{SHAPE}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return f"{path} isn't valid JSON ({e}). Fix it, then call ExitPlanMode again."
    if found := problems(data):
        return f"Fix {path}: {'; '.join(found)}. Then call ExitPlanMode again."
    return None


def load(ws: Path) -> dict | None:
    """The reproduction record, or None when there's none or it isn't valid."""
    path = ws / FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None if problems(data) else data
