"""Tear down an issue's worktree, branch and slot once its PR has merged."""

import subprocess
import traceback

import anyio
import httpx
import psycopg
import redis
from dotenv import dotenv_values
from psycopg import sql

from software_factory import config, linear, store
from software_factory.environment import branch_name, database_name, worktree_path
from software_factory.utils import ssl_context


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


def delete_branches(identifier: str):
    branch = branch_name(identifier)
    if _git("branch", "--list", branch).stdout.strip():
        _git("branch", "-D", branch)
    # Azure DevOps deletes the source branch on completion only when asked to
    if _git("ls-remote", "--heads", "origin", branch).stdout.strip():
        _git("push", "origin", "--delete", branch)


async def cleanup_after_merge(issue_id: str) -> bool:
    """Run each teardown step in order. A failing step flags the issue and keeps its slot."""
    issue = store.get_issue(issue_id)
    identifier = issue["identifier"]
    steps = [("dropping the databases", lambda: drop_databases(identifier))]
    if issue["slot"] is not None:
        steps.append(("flushing the Redis DB", lambda: flush_redis(issue["slot"])))
    steps += [
        ("removing the worktree", lambda: remove_worktree(identifier)),
        ("deleting the branch", lambda: delete_branches(identifier)),
    ]

    for name, step in steps:
        try:
            await anyio.to_thread.run_sync(step)
        except Exception as e:
            traceback.print_exc()
            detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
            store.transition(issue_id, store.NEEDS_ATTENTION, reason=f"PR merged, but cleanup failed at {name}: {detail}")
            return False
        store.add_event(issue_id, f"Cleanup: {name} done")

    store.free_slot(issue_id)
    store.transition(issue_id, store.MERGED)
    await linear.report_state(issue_id, config.LINEAR_MERGED_STATE)
    return True


async def pull_request_status(pr_id: int) -> str:
    """active, abandoned or completed."""
    async with httpx.AsyncClient(
        auth=("", str(config.ado_pat())),
        params={"api-version": config.ADO_API_VERSION},
        verify=ssl_context(),
        timeout=30,
    ) as client:
        result = await client.get(f"{config.ADO_REPO_API}/pullrequests/{pr_id}")
        result.raise_for_status()
        return result.json()["status"]


async def check_merges():
    """Clean up after every opened PR that has merged; flag the abandoned ones."""
    for issue in store.list_issues(store.PR_OPENED):
        if issue["pr_id"] is None:
            continue
        try:
            status = await pull_request_status(issue["pr_id"])
        except (httpx.HTTPError, RuntimeError) as e:
            print(f"{issue['identifier']}: couldn't check PR {issue['pr_id']}: {e}")
            continue
        if status == "completed":
            await cleanup_after_merge(issue["id"])
        elif status == "abandoned":
            store.transition(issue["id"], store.NEEDS_ATTENTION, reason="The PR was abandoned in Azure DevOps")
