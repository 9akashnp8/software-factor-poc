"""Reviewer comments on the factory's PRs, and the /factory commands that act on them.

A reviewer leaves comments on the PR in Azure DevOps as usual, then comments
`/factory fix` to send them to the agent, or `/factory replan` to send them back to the
planner. Only comments from config.PR_REVIEWERS count: they reach an agent with a shell.
A command that isn't acted on, from someone else or written so it doesn't read as one, is
reported once in the issue's history so it doesn't vanish silently.

The factory posts with the PAT owner's account, who may be a reviewer too, so its own
replies are told apart by their ids (store.record_comment) and by REPLY_PREFIX.
"""

import json
import re
from dataclasses import dataclass, field

import httpx

from software_factory import config, store
from software_factory.utils import ado_time

# A command starts a line; the rest of the line is feedback like any other ("/factory fix: rename it")
COMMAND_RE = re.compile(r"^[ \t]*/factory[ \t]+(fix|replan)(?![\w-])[ \t]*[:,.\-]?[ \t]*", re.IGNORECASE | re.MULTILINE)
# Looks like someone meant a command: /factory starting a line, or a command anywhere
MENTION_RE = re.compile(r"^[ \t]*/factory\b|/factory[ \t]+(fix|replan)\b", re.IGNORECASE | re.MULTILINE)
REPLY_PREFIX = "**Software Factory:**"
# Threads a reviewer still expects something on; fixed, won't fix, closed and by design are done
OPEN_STATUSES = frozenset({"active", "pending"})


@dataclass
class Review:
    """The newest /factory command not yet acted on, and the comments it sends."""

    command: str  # "fix" or "replan"
    posted_at: str
    thread_id: int
    comment_id: int
    threads: list[dict] = field(default_factory=list)
    # Comments in open threads from people not in config.PR_REVIEWERS, left out
    ignored: int = 0


@dataclass
class Skipped:
    """A comment that looks like a /factory command but isn't acted on."""

    thread_id: int
    comment_id: int
    reason: str  # "not a reviewer" or "unclear"
    author: dict

    def name(self) -> str:
        return self.author.get("uniqueName") or self.author.get("displayName") or "this person"

    def event(self) -> str:
        if self.reason == "unclear":
            return (
                f"Didn't act on a PR comment that mentions /factory from {self.name()}: "
                "commands go at the start of a line, as /factory fix or /factory replan"
            )
        reviewers = ", ".join(sorted(config.PR_REVIEWERS)) or "nobody: it isn't set"
        return (
            f"Didn't act on a /factory command from {self.name()}: they aren't in SF_PR_REVIEWERS "
            f"(which has {reviewers})"
        )


def allowed(author: dict | None) -> bool:
    names = {(author or {}).get(key, "").lower() for key in ("uniqueName", "id")}
    return bool(names & config.PR_REVIEWERS)


def without_commands(text: str | None) -> str:
    return COMMAND_RE.sub("", text or "").strip()


def _text_comments(thread: dict) -> list[dict]:
    """A thread's comments written by people, oldest first; votes and pushes are system ones."""
    comments = [
        c for c in thread.get("comments") or [] if c.get("commentType") == "text" and not c.get("isDeleted")
    ]
    return sorted(comments, key=lambda c: c["id"])


def _ours(thread_id: int, comment: dict, posted: set[tuple[int, int]]) -> bool:
    return (thread_id, comment["id"]) in posted or (comment.get("content") or "").lstrip().startswith(REPLY_PREFIX)


def _location(thread: dict) -> tuple[str | None, int | None]:
    context = thread.get("threadContext") or {}
    start = context.get("rightFileStart") or context.get("leftFileStart") or {}
    return context.get("filePath"), start.get("line")


def find_review(threads: list[dict], posted: set[tuple[int, int]], since: str | None) -> Review | None:
    """The newest /factory command from a reviewer posted after since, with the open
    threads that have reviewer comments the factory hasn't replied to yet."""
    after = ado_time(since)
    review = None
    for thread in threads:
        if thread.get("isDeleted"):
            continue
        for comment in _text_comments(thread):
            if _ours(thread["id"], comment, posted) or not allowed(comment.get("author")):
                continue
            match = COMMAND_RE.search(comment.get("content") or "")
            at = ado_time(comment.get("publishedDate"))
            if not match or at is None or (after is not None and at <= after):
                continue
            if review is None or at > ado_time(review.posted_at):
                review = Review(match.group(1).lower(), comment["publishedDate"], thread["id"], comment["id"])
    if review is None:
        return None

    for thread in threads:
        if thread.get("isDeleted") or thread.get("status") not in OPEN_STATUSES:
            continue
        comments = _text_comments(thread)
        # A thread the factory answered last is waiting on the reviewer, not on the factory
        last_ours = max((i for i, c in enumerate(comments) if _ours(thread["id"], c, posted)), default=-1)
        new = comments[last_ours + 1 :]
        review.ignored += sum(1 for c in new if not allowed(c.get("author")))
        if not any(allowed(c.get("author")) and without_commands(c.get("content")) for c in new):
            continue
        conversation = []
        for c in comments:
            if _ours(thread["id"], c, posted):
                conversation.append({"author": "Software Factory (you)", "text": c.get("content", "").removeprefix(REPLY_PREFIX).strip()})
            elif allowed(c.get("author")) and (text := without_commands(c.get("content"))):
                conversation.append({"author": c["author"].get("displayName") or "Reviewer", "text": text})
        path, line = _location(thread)
        review.threads.append(
            {"id": thread["id"], "reply_to": comments[0]["id"], "file": path, "line": line, "comments": conversation}
        )
    return review


def find_skipped(
    threads: list[dict], posted: set[tuple[int, int]], since: str | None, reported: set[tuple[int, int]]
) -> list[Skipped]:
    """Comments posted after since that look like a command but won't be acted on, and
    weren't reported yet: commands from people not in config.PR_REVIEWERS, and reviewers'
    commands that don't read as one."""
    after = ado_time(since)
    skipped = []
    for thread in threads:
        if thread.get("isDeleted"):
            continue
        for comment in _text_comments(thread):
            content = comment.get("content") or ""
            if (thread["id"], comment["id"]) in reported or _ours(thread["id"], comment, posted):
                continue
            at = ado_time(comment.get("publishedDate"))
            if at is None or (after is not None and at <= after) or not MENTION_RE.search(content):
                continue
            author = comment.get("author") or {}
            if not allowed(author):
                skipped.append(Skipped(thread["id"], comment["id"], "not a reviewer", author))
            elif not COMMAND_RE.search(content):
                skipped.append(Skipped(thread["id"], comment["id"], "unclear", author))
    return skipped


def render(threads: list[dict]) -> str:
    """The threads as the agent reads them."""
    parts = []
    for thread in threads:
        where = f' file="{thread["file"]}"' if thread.get("file") else ""
        where += f' line="{thread["line"]}"' if thread.get("file") and thread.get("line") else ""
        lines = [f"{c['author']}: {c['text']}" for c in thread["comments"]]
        parts.append(f'<thread id="{thread["id"]}"{where}>\n' + "\n\n".join(lines) + "\n</thread>")
    return "\n\n".join(parts)


def dump(review: Review) -> str:
    """What a /factory fix hands the update job, kept on the issue while it runs."""
    return json.dumps(
        {"command_thread": review.thread_id, "command_comment": review.comment_id, "threads": review.threads},
        ensure_ascii=False,
    )


async def list_threads(client: httpx.AsyncClient, pr_id: int) -> list[dict]:
    result = await client.get(f"{config.ADO_REPO_API}/pullRequests/{pr_id}/threads")
    result.raise_for_status()
    return result.json()["value"]


async def reply(client: httpx.AsyncClient, issue_id: str, pr_id: int, thread_id: int, parent_id: int, text: str):
    """Answer in a thread as the factory, and remember the comment as ours."""
    result = await client.post(
        f"{config.ADO_REPO_API}/pullRequests/{pr_id}/threads/{thread_id}/comments",
        json={"content": f"{REPLY_PREFIX} {text}", "parentCommentId": parent_id, "commentType": 1},
    )
    result.raise_for_status()
    store.record_comment(issue_id, thread_id, result.json()["id"])


NOTHING_TO_DO = "Nothing to act on: no open comments from reviewers since the last update."
REPLANNING = "Sent back for a new plan."


async def check(client: httpx.AsyncClient, issue) -> str | None:
    """Act on a new /factory command on the issue's PR, and say why when one isn't acted on.
    Returns what was done, for the log."""
    threads = await list_threads(client, issue["pr_id"])
    posted = store.factory_comments(issue["id"])
    outcomes = report_skipped(issue, threads, posted)
    if done := await act(client, issue, threads, posted):
        outcomes.append(done)
    return "; ".join(outcomes) or None


def report_skipped(issue, threads: list[dict], posted: set) -> list[str]:
    """Say once, in the issue's history and the worker's log, why a command isn't acted on.

    Not on the PR: a reply there would look like the factory answering the thread, and
    leave a reviewer's comments in it out of the next /factory fix.
    """
    outcomes = []
    for skip in find_skipped(threads, posted, issue["pr_command_at"], store.skipped_commands(issue["id"])):
        if store.record_skipped_command(issue["id"], skip.thread_id, skip.comment_id):
            store.add_event(issue["id"], skip.event())
            outcomes.append(skip.event())
    return outcomes


async def act(client: httpx.AsyncClient, issue, threads: list[dict], posted: set) -> str | None:
    review = find_review(threads, posted, issue["pr_command_at"])
    if review is None:
        return None
    if review.ignored:
        store.add_event(
            issue["id"],
            f"Left out {review.ignored} PR comment(s) from people not in SF_PR_REVIEWERS",
        )
    if not review.threads:
        # Marked done before replying, so a reply that fails doesn't act on the command again
        store.set_fields(issue["id"], pr_command_at=review.posted_at)
        await reply(client, issue["id"], issue["pr_id"], review.thread_id, review.comment_id, NOTHING_TO_DO)
        return f"/factory {review.command}: nothing to act on"

    replan = review.command == "replan"
    feedback = render(review.threads)
    if replan:
        feedback = REPLAN_FEEDBACK.format(pr=issue["pr_url"] or "the PR", threads=feedback)
    if not store.queue_review(issue["id"], replan, feedback, None if replan else dump(review), review.posted_at):
        return f"/factory {review.command}: not queued"
    if replan:
        plan = f" The revised plan will be at {config.PUBLIC_URL}/issue/{issue['id']}/plan." if config.PUBLIC_URL else ""
        for thread in review.threads:
            await reply(client, issue["id"], issue["pr_id"], thread["id"], thread["reply_to"], REPLANNING + plan)
        if review.thread_id not in {t["id"] for t in review.threads}:
            await reply(client, issue["id"], issue["pr_id"], review.thread_id, review.comment_id, REPLANNING + plan)
    return f"/factory {review.command}: {len(review.threads)} thread(s)"


# The rework feedback for /factory replan
REPLAN_FEEDBACK = """Reviewers of your pull request ({pr}) want the approach rethought. The branch
already has your earlier fix, pushed to that PR. Revise the plan with their comments in mind,
and say what changes from the fix already on the branch. The comments, by thread:

<review_comments>
{threads}
</review_comments>"""
