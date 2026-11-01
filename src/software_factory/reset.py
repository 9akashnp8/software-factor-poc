"""Put issues back to a clean state by hand, after something broke mid-way.

Starting over tears down what the issue holds (databases, Redis queue, worktree, local
branch, slot) and queues it for triage as if new. Going back to plan review keeps all of
that and only resets the status. Neither touches Linear, Azure DevOps or a pushed branch.
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

from software_factory import cleanup, store
from software_factory.environment import branch_name

# What a run leaves in the workspace; set aside on a fresh start so the next run can't read it
ARTIFACTS = ("issue.md", "plan.md", "report.md", "review.md", "review.json", "conversation.json", "attachments")
# Finished issues: reset --all leaves them alone
FINISHED = frozenset({store.MERGED, store.RELEASED})


class ResetRefused(RuntimeError):
    pass


def set_aside(issue_id: str) -> Path | None:
    """Move the last attempt's files into reset-<time>/, for reference. Returns that folder."""
    ws = store.issue_dir(issue_id)
    present = [ws / name for name in ARTIFACTS if (ws / name).exists()]
    if not present:
        return None
    folder = ws / f"reset-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    folder.mkdir(parents=True)
    for path in present:
        shutil.move(path, folder / path.name)
    return folder


def _check_no_run(issue, force: bool):
    runs = store.open_runs(issue["id"])
    if runs and not force:
        phases = ", ".join(r["phase"] for r in runs)
        raise ResetRefused(
            f"{issue['identifier']} has a {phases} run in progress. Stop the worker first, "
            "or pass --force if the worker crashed and the run is dead."
        )
    for run in runs:
        store.finish_run(run["id"], error="Closed by a reset")


async def reset_issue(key: str, to: str = "start", force: bool = False, actor: str = "reset") -> str:
    """Reset one issue, by identifier or id. to is "start" (triage again) or "plan" (plan review).

    Returns what was done; raises ResetRefused when it can't.
    """
    issue = store.find_issue(key)
    if issue is None:
        raise ResetRefused(f"No issue {key} in the factory")
    identifier = issue["identifier"]
    _check_no_run(issue, force)

    if to == "plan":
        if not issue["session_id"] or not (store.issue_dir(issue["id"]) / "plan.md").exists():
            raise ResetRefused(f"{identifier} has no plan to go back to. Reset it without --to plan to start over.")
        store.back_to_plan_review(issue["id"], actor)
        return f"{identifier}: back to plan review; its worktree, slot and session are kept"

    if not await cleanup.teardown(issue["id"], None, "Reset"):
        reason = store.get_issue(issue["id"])["needs_attention_reason"]
        raise ResetRefused(f"{identifier}: {reason}. Fix that and run the reset again.")
    folder = set_aside(issue["id"])
    store.restart(issue["id"], actor)

    message = f"{identifier}: queued for triage from scratch"
    if folder:
        message += f"; the last attempt's files are in {folder}"
    if issue["pr_url"] and issue["status"] not in FINISHED:
        message += (
            f"\n  Its PR {issue['pr_url']} is still open. Abandon it in Azure DevOps and delete the "
            f"remote {branch_name(identifier)} branch, or the next attempt's push will be rejected."
        )
    return message


async def reset_all(force: bool = False, actor: str = "reset") -> list[str]:
    """Start every unfinished issue over, and lift any rate-limit pause."""
    issues = [i for i in store.list_issues() if i["status"] not in FINISHED]
    if not force and (runs := store.open_runs()):
        running = sorted({store.get_issue(r["issue_id"])["identifier"] for r in runs})
        raise ResetRefused(
            f"Runs are in progress for {', '.join(running)}. Stop the worker first, "
            "or pass --force if it crashed."
        )
    results = []
    for issue in issues:
        try:
            results.append(await reset_issue(issue["id"], force=force, actor=actor))
        except ResetRefused as e:
            results.append(f"Not reset: {e}")
    store.clear_pause()
    return results
