"""What merged to dev since a plan was written, for re-checking a plan in review.

Each entry on dev's first-parent line is one merged PR ("Merged PR 5540: BEI-17: …"). The
ones the factory opened come with the root cause and fix their implementer wrote.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from software_factory import config, store
from software_factory.environment import worktree_path

MERGED_PR_RE = re.compile(r"^Merged PR (\d+)")
# Enough for the agent to see what moved; it reads the rest with git
MAX_MERGES = 30
MAX_FILES = 15


@dataclass
class Merge:
    commit: str
    subject: str
    files: list[str] = field(default_factory=list)
    # The factory issue this PR fixed, if the factory opened it
    issue: dict | None = None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def plan_base(issue, worktree: Path) -> str:
    """The dev commit the issue's plan was written or last re-checked against."""
    # Plans from before plan_base existed were written on the worktree's HEAD
    return issue["plan_base"] or _git(worktree, "rev-parse", "HEAD")


def _factory_fix(issue) -> dict:
    fix = {"identifier": issue["identifier"]}
    qa = store.issue_dir(issue["id"]) / "qa.json"
    if qa.exists():
        try:
            fix.update(json.loads(qa.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return fix


def merges(worktree: Path, old: str, new: str) -> list[Merge]:
    """The PRs merged to dev between old and new, oldest first."""
    # --diff-merges=first-parent lists a merge commit's files against the dev it landed on
    log = _git(
        worktree, "log", "--first-parent", "--diff-merges=first-parent", "--name-only",
        "--reverse", "--format=%x1e%H%x1f%s", f"{old}..{new}",
    )
    by_pr = {issue["pr_id"]: issue for issue in store.list_issues() if issue["pr_id"] is not None}
    found = []
    for record in filter(None, log.split("\x1e")):
        header, _, files = record.partition("\n")
        commit, _, subject = header.partition("\x1f")
        merge = Merge(commit, subject.strip(), [f for f in files.splitlines() if f.strip()])
        if (m := MERGED_PR_RE.match(merge.subject)) and (issue := by_pr.get(int(m.group(1)))):
            merge.issue = _factory_fix(issue)
        found.append(merge)
    return found


def render(found: list[Merge], old: str, new: str) -> str:
    """The <dev_changes> block of the refresh prompt."""
    lines = [
        "<dev_changes>",
        f"Your plan was written against {old[:10]}. The worktree is now at {new[:10]}, "
        f"{len(found)} merge(s) later:",
    ]
    for merge in found[:MAX_MERGES]:
        lines += ["", f"- {merge.subject}" + (" (a factory fix)" if merge.issue else "")]
        if merge.issue:
            for key, label in (("root_cause", "Root cause"), ("fix", "Fix")):
                if merge.issue.get(key):
                    lines.append(f"  {label}: {merge.issue[key]}")
        if merge.files:
            shown = ", ".join(merge.files[:MAX_FILES])
            more = f" and {len(merge.files) - MAX_FILES} more" if len(merge.files) > MAX_FILES else ""
            lines.append(f"  Files: {shown}{more}")
    if len(found) > MAX_MERGES:
        lines += [
            "",
            f"…and {len(found) - MAX_MERGES} more: run `git log --first-parent {old[:10]}..HEAD`.",
        ]
    lines.append("</dev_changes>")
    return "\n".join(lines)


def label(found: list[Merge]) -> str:
    """"5 new merges to dev, including BEI-17 and BEI-20", for the issue's history."""
    text = f"{len(found)} new merge{'s' if len(found) != 1 else ''} to {config.BASE_BRANCH}"
    if fixes := [m.issue["identifier"] for m in found if m.issue]:
        names = fixes[0] if len(fixes) == 1 else f"{', '.join(fixes[:-1])} and {fixes[-1]}"
        text += f", including {names}"
    return text


def summary(issue) -> str | None:
    """What merged to dev since the issue's plan, as label() words it; None if nothing did.

    Uses origin/dev as the worker last fetched it, so it can be one fetch behind.
    """
    worktree = worktree_path(issue["identifier"])
    if not worktree.exists():
        return None
    try:
        found = merges(worktree, plan_base(issue, worktree), f"origin/{config.BASE_BRANCH}")
    except subprocess.CalledProcessError:
        return None
    return label(found) if found else None
