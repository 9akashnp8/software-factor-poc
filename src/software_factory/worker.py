"""The one process that runs agents.

It queues new Todo bugs from Linear, runs at most config.MAX_CONCURRENT_RUNS agents at a
time from the queue in SQLite, and holds off while a rate limit is in force. It watches
the PRs it opened: merged ones are cleaned up, and ones that conflict or fail their build
go back to their agent. The web app only moves issues into the queued statuses.
"""

import argparse
import importlib
import traceback
from collections.abc import Awaitable, Callable

import anyio
import httpx

from software_factory import cleanup, config, pull_requests, store
from software_factory.linear import LinearError, list_todo_bugs

# By full name: the package's `main` attribute is this module's entry point, not the module
pipeline = importlib.import_module("software_factory.main")

# How often to look for queued work; claiming is one cheap SQLite transaction
DISPATCH_SECONDS = 5


async def ingest() -> list[str]:
    """Queue Todo bugs the factory hasn't seen, and finished ones QA moved back to Todo.

    Returns the queued identifiers.
    """
    try:
        bugs = await list_todo_bugs()
    except (LinearError, httpx.HTTPError) as e:
        print(f"Couldn't list Linear issues: {e}")
        return []
    queued = []
    # Oldest first: the queue serves issues in the order they were added
    for bug in sorted(bugs, key=lambda b: b.get("createdAt") or ""):
        if store.enqueue_new(bug["id"], bug["identifier"], bug["title"], bug.get("url")):
            queued.append(bug["identifier"])
        elif previous := store.reopen(bug["id"]):
            # Back in Todo after a merge or a release: the fix didn't hold, so keep what was tried
            pipeline.archive_attempt(bug["id"], previous["pr_url"])
            queued.append(f"{bug['identifier']} (reopened)")
    if queued:
        print(f"Queued {', '.join(queued)}")
    return queued


async def run_job(issue_id: str, job: str):
    if job == "triage":
        await pipeline.triage_and_plan_issue(issue_id, owner="worker")
    elif job == "implement":
        await pipeline.implement_and_review(issue_id, True, owner="worker")
    elif job == "update":
        await pipeline.update_pull_request(issue_id, owner="worker")
    elif job == "release":
        await cleanup.release(issue_id)
    else:
        feedback = store.get_issue(issue_id)["pending_feedback"]
        await pipeline.implement_and_review(issue_id, False, feedback, owner="worker")


class Dispatcher:
    def __init__(self, max_runs: int, job: Callable[[str, str], Awaitable] = run_job):
        self.max_runs = max_runs
        self.job = job
        self.running = 0

    async def dispatch(self, task_group) -> int:
        """Start queued jobs up to the limit, unless paused. Returns how many started."""
        started = 0
        while self.running < self.max_runs and store.paused_until() is None:
            claimed = store.claim_next()
            if claimed is None:
                break
            self.running += 1
            task_group.start_soon(self._run, *claimed)
            started += 1
        return started

    async def _run(self, issue_id: str, job: str):
        try:
            await self.job(issue_id, job)
        except Exception as e:
            traceback.print_exc()
            store.transition(issue_id, store.NEEDS_ATTENTION, reason=f"The {job} job failed: {e!r}")
        finally:
            self.running -= 1


async def every(seconds: float, step: Callable[[], Awaitable]):
    while True:
        try:
            await step()
        except Exception:
            # One failed poll mustn't stop the worker
            traceback.print_exc()
        await anyio.sleep(seconds)


async def run_worker():
    if requeued := store.requeue_orphans():
        print(f"Requeued {len(requeued)} issue(s) a previous worker left running")
    dispatcher = Dispatcher(config.MAX_CONCURRENT_RUNS)
    print(f"Worker started: up to {config.MAX_CONCURRENT_RUNS} runs at a time")
    async with anyio.create_task_group() as tg:
        tg.start_soon(every, config.INGEST_SECONDS, ingest)
        tg.start_soon(every, config.MERGE_POLL_SECONDS, pull_requests.check_pull_requests)
        tg.start_soon(every, DISPATCH_SECONDS, lambda: dispatcher.dispatch(tg))


def main() -> None:
    parser = argparse.ArgumentParser(prog="software-factory")
    parser.add_argument(
        "command",
        nargs="?",
        default="worker",
        choices=["worker", "ingest"],
        help="worker: run the queue (default). ingest: queue new Todo bugs once and exit.",
    )
    args = parser.parse_args()
    anyio.run(run_worker if args.command == "worker" else ingest)


if __name__ == "__main__":
    main()
