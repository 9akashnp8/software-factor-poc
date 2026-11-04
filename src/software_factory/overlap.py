"""Which other in-flight issues touch the files a plan names, so the reviewer can see a
likely merge conflict before approving. A warning only: plans name files loosely."""

import re
import subprocess

from software_factory import config, store
from software_factory.environment import branch_name

# Repo-relative paths with at least one directory, e.g. apps/api/agents/router.py
PATH_RE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)+[\w-]+\.\w+")
# Issues whose branch has commits to compare against
WITH_COMMITS = frozenset({store.IMPLEMENTING, store.WORK_COMPLETED, store.PR_OPENED, store.UPDATE_QUEUED, store.UPDATING})
# Issues with a plan but no commits yet
WITH_PLAN_ONLY = frozenset(
    {store.PLAN_READY, store.AWAITING_QA, store.IMPLEMENT_QUEUED, store.REWORK_QUEUED, store.REWORKING, store.REFRESH_QUEUED, store.REFRESHING}
)


def mentioned_paths(text: str) -> set[str]:
    return {m.removeprefix("./") for m in PATH_RE.findall(text)}


def plan_paths(issue_id: str) -> set[str]:
    plan = store.issue_dir(issue_id) / "plan.md"
    return mentioned_paths(plan.read_text(encoding="utf-8")) if plan.exists() else set()


def changed_files(identifier: str) -> set[str]:
    """Files the issue's branch changes against the base; empty if there's no branch."""
    result = subprocess.run(
        ["git", "-C", str(config.TARGET_REPO), "diff", "--name-only",
         f"origin/{config.BASE_BRANCH}...{branch_name(identifier)}"],
        capture_output=True,
        text=True,
    )
    return set(result.stdout.split()) if result.returncode == 0 else set()


def overlaps(issue) -> list[tuple[str, str, list[str]]]:
    """(identifier, status, shared files) for each other in-flight issue sharing a file with issue's plan."""
    mine = plan_paths(issue["id"])
    if not mine:
        return []
    found = []
    for other in store.list_issues():
        if other["id"] == issue["id"]:
            continue
        if other["status"] in WITH_COMMITS:
            theirs = changed_files(other["identifier"]) | plan_paths(other["id"])
        elif other["status"] in WITH_PLAN_ONLY:
            theirs = plan_paths(other["id"])
        else:
            continue
        if shared := sorted(mine & theirs):
            found.append((other["identifier"], other["status"], shared))
    return found
