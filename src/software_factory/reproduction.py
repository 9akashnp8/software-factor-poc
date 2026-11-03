"""What the planner found when it tried to reproduce the bug, in the workspace's reproduction.json.

The planner writes the file before it presents its plan; ExitPlanMode is refused until the
file is valid. A bug that didn't reproduce gets findings and a question for QA instead of
a fix plan, and can't be approved.
"""

import json
from pathlib import Path

FILE = "reproduction.json"

REPRODUCED = "reproduced"
NOT_REPRODUCED = "not_reproduced"
NOT_ATTEMPTED = "not_attempted"
STATUSES = (REPRODUCED, NOT_REPRODUCED, NOT_ATTEMPTED)
METHODS = ("test", "replay", "browser", "api", "manual")

# What the planner is told to write, quoted in its prompt and in refusals
SHAPE = """{
  "status": "reproduced" | "not_reproduced" | "not_attempted",
  "method": "test" | "replay" | "browser" | "api" | "manual",
  "steps": ["what you ran or did, in order"],
  "evidence": "a short excerpt of the output that shows the bug, or that it didn't happen",
  "reason": "required for not_attempted: why it couldn't be tried",
  "qa_question": "required for not_reproduced: what to ask QA, in plain words"
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
    if status != NOT_ATTEMPTED and not (isinstance(steps, list) and steps and all(isinstance(s, str) for s in steps)):
        found.append("steps must be a non-empty list of strings")
    if status != NOT_ATTEMPTED and not _text(data.get("evidence")):
        found.append("evidence is required")
    if status == NOT_ATTEMPTED and not _text(data.get("reason")):
        found.append("reason is required when the status is not_attempted")
    if status == NOT_REPRODUCED and not _text(data.get("qa_question")):
        found.append("qa_question is required when the status is not_reproduced")
    return found


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
