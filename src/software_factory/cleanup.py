"""Tear down an issue's worktree, branch and slot once its PR has merged, or it's released."""

import subprocess
import traceback

import anyio
import psycopg
import redis
from dotenv import dotenv_values
from psycopg import sql

from software_factory import config, deployments, linear, store
from software_factory.environment import (
    branch_name,
    database_name,
    force_rmtree,
    stop_servers,
    stop_worktree_processes,
    worktree_path,
)


def _target_env() -> dict:
    """The target repo's .env, which holds the shared Postgres and Redis credentials."""
    return dotenv_values(config.TARGET_REPO / ".env")


def drop_databases(identifier: str):
    env = _target_env()
    name = database_name(identifier)
    with psycopg.connect(
        host=env.get("POSTGRES_HOST") or "localhost",
        port=int(env.get("POSTGRES_PORT") or 5432),
        user=env.get("POSTGRES_USER") or "postgres",
        password=env.get("POSTGRES_PASSWORD") or "postgres",
        dbname="postgres",
        # DROP DATABASE can't run inside a transaction block
        autocommit=True,
    ) as conn:
        # The target's tests use <name>_test; FORCE ends sessions a dev server left open
        for db in (name, f"{name}_test"):
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db)))


def flush_redis(slot: int):
    env = _target_env()
    client = redis.Redis(
        host=env.get("REDIS_HOST") or "localhost",
        port=int(env.get("REDIS_PORT") or 6379),
        password=env.get("REDIS_PASSWORD") or None,
        db=slot + 1,
    )
    try:
        client.flushdb()
    finally:
        client.close()


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(config.TARGET_REPO), *args], check=True, capture_output=True, text=True)


def remove_worktree(identifier: str):
    path = worktree_path(identifier)
    if path.exists():
        # On Windows anything running from the worktree keeps its files from being deleted
        stop_worktree_processes(path)
        try:
            _git("worktree", "remove", "--force", str(path))
        except subprocess.CalledProcessError:
            # Git gives up on a file something holds open (a .venv on Windows) or on a
            # directory it no longer knows, leaving the rest. Delete what's left; a file
            # still held open raises here, and the teardown retries it.
            pass
        if path.exists():
            force_rmtree(path)
    _git("worktree", "prune")


def remote_tip(ref: str) -> str | None:
    """The commit origin's ref points at, matching the full ref name exactly."""
    # ls-remote matches patterns against the end of ref names, so check each one it lists
    for line in _git("ls-remote", "origin", ref).stdout.splitlines():
        sha, _, name = line.partition("	")
        if name == ref:
            return sha
    return None


def delete_branches(identifier: str, remote: bool = True, expected_tip: str | None = None) -> str | None:
    """Delete the issue's local branch and, with remote, its branch on origin.

    Only refs/heads/fix/<identifier> is touched, by its full name. On origin it's deleted
    only while it still points at the commit we saw: expected_tip when given (the PR's last
    pushed commit), else the tip ls-remote reported. A branch someone pushed to since is
    kept; the returned note says so, for the issue's history.
    """
    branch = branch_name(identifier)
    if _git("branch", "--list", branch).stdout.strip():
        _git("branch", "-D", branch)
    # Azure DevOps deletes the source branch on completion only when asked to
    if not remote:
        return None
    ref = f"refs/heads/{branch}"
    tip = remote_tip(ref)
    if tip is None:
        return None
    if expected_tip is not None and tip != expected_tip:
        return f"Kept {branch} on origin: it's at {tip[:8]}, not the PR's {expected_tip[:8]}, so someone pushed to it"
    # The lease makes origin refuse the delete if the branch moved after ls-remote
    _git("push", f"--force-with-lease={ref}:{tip}", "origin", f":{ref}")
    return None


async def teardown(
    issue_id: str,
    final_status: str | None,
    why: str,
    delete_remote: bool = False,
    flag: bool = True,
    remote_tip: str | None = None,
) -> bool:
    """Remove the issue's databases, Redis queue, worktree and local branch, then free its slot.

    Each step runs in order and is safe to repeat. A failing one keeps the slot and, with
    flag, puts the issue in needs-attention; without, it's only recorded, so the caller's
    status stands and the teardown can be run again later. why names the occasion, e.g.
    "PR merged". With final_status None the status is left for the caller. The remote
    branch goes only with delete_remote, since an open PR lives on it, and with remote_tip
    only while it still points there. Returns whether every step finished.
    """
    issue = store.get_issue(issue_id)
    identifier = issue["identifier"]
    # An issue that never got past the queue has nothing to tear down
    has_resources = issue["slot"] is not None or worktree_path(identifier).exists()
    steps = []
    if issue["slot"] is not None:
        # First: a server left running holds the database and the worktree's files open
        steps.append(("stopping servers left running", lambda: stop_servers(issue["slot"])))
    if has_resources:
        steps.append(("dropping the databases", lambda: drop_databases(identifier)))
    if issue["slot"] is not None:
        steps.append(("flushing the Redis DB", lambda: flush_redis(issue["slot"])))
    if has_resources:
        steps += [
            ("removing the worktree", lambda: remove_worktree(identifier)),
            ("deleting the branch", lambda: delete_branches(identifier, remote=delete_remote, expected_tip=remote_tip)),
        ]

    for name, step in steps:
        try:
            note = await anyio.to_thread.run_sync(step)
        except Exception as e:
            traceback.print_exc()
            detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
            reason = f"{why}, but cleanup failed at {name}: {detail}"
            if flag:
                store.transition(issue_id, store.NEEDS_ATTENTION, reason=reason)
            else:
                store.add_event_once(issue_id, f"{reason}. It's retried on every PR check.")
            return False
        if isinstance(note, str):
            store.add_event_once(issue_id, note)

    # A later attempt gets a new database, so it must provision again
    (store.issue_dir(issue_id) / ".provisioned").unlink(missing_ok=True)
    store.free_slot(issue_id)
    if steps:
        store.add_event(issue_id, "Cleanup done: databases, Redis DB, worktree and branch removed")
    if final_status is not None:
        store.transition(issue_id, final_status)
    return True


async def cleanup_after_merge(issue_id: str, merge_commit: str | None = None, merged_at: str | None = None) -> bool:
    """Record the merge and tell Linear, then tear down.

    With a deploy pipeline, the issue waits in Pending Deployment for a deploy that ships
    it (see pull_requests.check_deploy); without one, QA hears now. Linear goes first: the
    merge is real even if a cleanup step fails, and a failed step is retried by the PR check.
    """
    store.set_fields(issue_id, merge_commit=merge_commit, merged_at=merged_at)
    if config.DEPLOY_PIPELINE_ID:
        store.transition(issue_id, store.DEPLOY_PENDING)
        await linear.report_state(issue_id, config.LINEAR_PENDING_DEPLOY_STATE)
    else:
        store.transition(issue_id, store.MERGED)
        await deployments.announce(issue_id)
    return await teardown(issue_id, None, "PR merged", delete_remote=True, flag=False)


async def release(issue_id: str, remote_tip: str | None = None) -> bool:
    """Give up on the issue: tear down what it holds.

    With remote_tip, the branch on origin goes too, if it's still at that commit.
    Linear is left as it is; pull_requests.release abandons an open PR first.
    """
    return await teardown(
        issue_id, store.RELEASED, "Released", delete_remote=remote_tip is not None, remote_tip=remote_tip
    )
