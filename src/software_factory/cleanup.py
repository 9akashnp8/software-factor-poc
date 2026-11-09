"""Tear down an issue's worktree, branch and slot once its PR has merged, or it's released."""

import subprocess
import traceback

import anyio
import psycopg
import redis
from dotenv import dotenv_values
from psycopg import sql

from software_factory import config, deployments, linear, store
from software_factory.environment import branch_name, database_name, force_rmtree, stop_servers, worktree_path


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


def delete_branches(identifier: str, remote: bool = True):
    branch = branch_name(identifier)
    if _git("branch", "--list", branch).stdout.strip():
        _git("branch", "-D", branch)
    # Azure DevOps deletes the source branch on completion only when asked to
    if remote and _git("ls-remote", "--heads", "origin", branch).stdout.strip():
        _git("push", "origin", "--delete", branch)


async def teardown(
    issue_id: str, final_status: str | None, why: str, delete_remote: bool = False, flag: bool = True
) -> bool:
    """Remove the issue's databases, Redis queue, worktree and local branch, then free its slot.

    Each step runs in order and is safe to repeat. A failing one keeps the slot and, with
    flag, puts the issue in needs-attention; without, it's only recorded, so the caller's
    status stands and the teardown can be run again later. why names the occasion, e.g.
    "PR merged". With final_status None the status is left for the caller. The remote
    branch goes only with delete_remote, since an open PR lives on it. Returns whether
    every step finished.
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
            ("deleting the branch", lambda: delete_branches(identifier, remote=delete_remote)),
        ]

    for name, step in steps:
        try:
            await anyio.to_thread.run_sync(step)
        except Exception as e:
            traceback.print_exc()
            detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
            reason = f"{why}, but cleanup failed at {name}: {detail}"
            if flag:
                store.transition(issue_id, store.NEEDS_ATTENTION, reason=reason)
            else:
                store.add_event_once(issue_id, f"{reason}. It's retried on every PR check.")
            return False

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


async def release(issue_id: str) -> bool:
    """Give up on the issue: tear down what it holds. Linear and any PR are left as they are."""
    return await teardown(issue_id, store.RELEASED, "Released")
