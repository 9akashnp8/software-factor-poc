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

from software_factory import cleanup, config, pull_requests, reset, store
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


def reset_command(args) -> int:
    if args.all and args.issue:
        print("Pass an issue or --all, not both.")
        return 2
    if not args.all and not args.issue:
        print("Pass an issue (e.g. BEI-12) or --all.")
        return 2
    if args.all:
        unfinished = [i for i in store.list_issues() if i["status"] not in reset.FINISHED]
        print(
            f"This starts {len(unfinished)} unfinished issue(s) over: their databases, Redis queues, "
            "worktrees and local branches are removed, and they're queued for triage again. "
            "Merged and released issues, Linear and Azure DevOps are left alone."
        )
        if not args.yes and input("Type 'reset' to continue: ").strip() != "reset":
            print("Nothing reset.")
            return 1
    try:
        if args.all:
            results = anyio.run(reset.reset_all, args.force)
        else:
            results = [anyio.run(reset.reset_issue, args.issue, args.to, args.force)]
    except reset.ResetRefused as e:
        print(e)
        return 1
    print("\n".join(results) or "Nothing to reset.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="software-factory")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("worker", help="Run the queue (the default)")
    commands.add_parser("ingest", help="Queue new Todo bugs once and exit")
    reset_parser = commands.add_parser(
        "reset",
        help="Put an issue, or all of them, back to a clean state",
        description="Start an issue over (triage from scratch), or put it back on plan review.",
    )
    reset_parser.add_argument("issue", nargs="?", help="The issue's identifier, e.g. BEI-12")
    reset_parser.add_argument("--all", action="store_true", help="Start every unfinished issue over")
    reset_parser.add_argument(
        "--to",
        choices=["start", "plan"],
        default="start",
        help="start: tear down and triage again (default). plan: back to plan review, keeping the worktree.",
    )
    reset_parser.add_argument("--force", action="store_true", help="Reset even with a run in progress, after a crash")
    reset_parser.add_argument("--yes", action="store_true", help="Don't ask before resetting everything")
    args = parser.parse_args()

    if args.command == "reset":
        raise SystemExit(reset_command(args))
    anyio.run(ingest if args.command == "ingest" else run_worker)


if __name__ == "__main__":
    main()
