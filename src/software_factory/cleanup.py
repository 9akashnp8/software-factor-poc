"""Tear down an issue's worktree, branch and slot once its PR has merged, or it's released."""

import subprocess
import traceback

import anyio
import psycopg
import redis
from dotenv import dotenv_values
from psycopg import sql

from software_factory import config, linear, store
from software_factory.environment import branch_name, database_name, worktree_path


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
        _git("worktree", "remove", "--force", str(path))
    _git("worktree", "prune")


def delete_branches(identifier: str, remote: bool = True):
    branch = branch_name(identifier)
    if _git("branch", "--list", branch).stdout.strip():
        _git("branch", "-D", branch)
    # Azure DevOps deletes the source branch on completion only when asked to
    if remote and _git("ls-remote", "--heads", "origin", branch).stdout.strip():
        _git("push", "origin", "--delete", branch)


async def teardown(issue_id: str, final_status: str | None, why: str, delete_remote: bool = False) -> bool:
    """Remove the issue's databases, Redis queue, worktree and local branch, then free its slot.

    Each step runs in order; a failing one flags the issue and keeps its slot. why names
    the occasion in that flag, e.g. "PR merged". With final_status None the status is left
    for the caller. The remote branch goes only with delete_remote, since an open PR lives
    on it. Returns whether every step finished.
    """
    issue = store.get_issue(issue_id)
    identifier = issue["identifier"]
    # An issue that never got past the queue has nothing to tear down
    has_resources = issue["slot"] is not None or worktree_path(identifier).exists()
    steps = [("dropping the databases", lambda: drop_databases(identifier))] if has_resources else []
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
            store.transition(issue_id, store.NEEDS_ATTENTION, reason=f"{why}, but cleanup failed at {name}: {detail}")
            return False
        store.add_event(issue_id, f"Cleanup: {name} done")

    # A later attempt gets a new database, so it must provision again
    (store.issue_dir(issue_id) / ".provisioned").unlink(missing_ok=True)
    store.free_slot(issue_id)
    if final_status is not None:
        store.transition(issue_id, final_status)
    return True


async def cleanup_after_merge(issue_id: str) -> bool:
    if not await teardown(issue_id, store.MERGED, "PR merged", delete_remote=True):
        return False
    await linear.report_state(issue_id, config.LINEAR_MERGED_STATE)
    return True


async def release(issue_id: str) -> bool:
    """Give up on the issue: tear down what it holds. Linear and any PR are left as they are."""
    return await teardown(issue_id, store.RELEASED, "Released")
