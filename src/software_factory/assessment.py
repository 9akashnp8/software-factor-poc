"""What kind of work the planner's fix is and how big, in the workspace's assessment.json.

The planner writes it with a fix plan; ExitPlanMode is refused until it's valid. The size
picks the implementer's model and caps, and the reviewer can change it when approving.
Findings with no fix (reproduction.NO_FIX) don't need one.
"""

import json
from pathlib import Path

FILE = "assessment.json"

KINDS = ("bug", "missing_capability", "change_request")
SMALL, MEDIUM, LARGE = "small", "medium", "large"
SIZES = (SMALL, MEDIUM, LARGE)

# What the planner is told to write, quoted in its prompt and in refusals
SHAPE = """{
  "kind": "bug" | "missing_capability" | "change_request",
  "size": "small" | "medium" | "large",
  "why": "one sentence: what makes it this size (files and modules touched, new pieces needed)"
}"""

# How each reads on the plan page
KIND_LABELS = {"bug": "Bug", "missing_capability": "Missing capability", "change_request": "Change request"}


def problems(data) -> list[str]:
    """What's wrong with an assessment; empty when it's valid."""
    if not isinstance(data, dict):
        return ["it must be a JSON object"]
    found = []
    if data.get("kind") not in KINDS:
        found.append(f"kind must be one of {', '.join(KINDS)}")
    if data.get("size") not in SIZES:
        found.append(f"size must be one of {', '.join(SIZES)}")
    why = data.get("why")
    if not (isinstance(why, str) and why.strip()):
        found.append("why is required")
    return found


def check(ws: Path) -> str | None:
    """Why the planner can't present its fix plan yet, or None when assessment.json is valid."""
    path = ws / FILE
    if not path.is_file():
        return f"Write {path} before presenting a fix plan, shaped like:\n{SHAPE}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return f"{path} isn't valid JSON ({e}). Fix it, then call ExitPlanMode again."
    if found := problems(data):
        return f"Fix {path}: {'; '.join(found)}. Then call ExitPlanMode again."
    return None


def load(ws: Path) -> dict | None:
    """The assessment, or None when there's none or it isn't valid."""
    try:
        data = json.loads((ws / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None if problems(data) else data
