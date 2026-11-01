import time

import anyio
import pytest

from conftest import seed_issue
from software_factory import store, worker


def queue(n: int):
    for i in range(1, n + 1):
        store.enqueue_new(f"t{i}", f"BEI-{i}")


async def dispatch_and_hold(dispatcher, release: anyio.Event) -> int:
    async with anyio.create_task_group() as tg:
        started = await dispatcher.dispatch(tg)
        # Nothing finishes until released, so a second pass can't exceed the limit
        started += await dispatcher.dispatch(tg)
        release.set()
    return started


def test_dispatch_never_runs_more_than_the_limit():
    queue(3)
    release = anyio.Event()
    seen = []

    async def job(issue_id, kind):
        seen.append((issue_id, kind))
        await release.wait()

    dispatcher = worker.Dispatcher(2, job)
    assert anyio.run(dispatch_and_hold, dispatcher, release) == 2
    assert seen == [("t1", "triage"), ("t2", "triage")]
    assert dispatcher.running == 0
    assert store.get_issue("t3")["status"] == store.QUEUED


def test_nothing_starts_while_paused():
    queue(1)
    store.pause_until(time.time() + 600)

    async def job(issue_id, kind):
        raise AssertionError("started while paused")

    async def go():
        async with anyio.create_task_group() as tg:
            return await worker.Dispatcher(2, job).dispatch(tg)

    assert anyio.run(go) == 0
    assert store.get_issue("t1")["status"] == store.QUEUED


def test_failed_job_flags_the_issue_and_frees_its_place():
    queue(1)

    async def job(issue_id, kind):
        raise RuntimeError("boom")

    dispatcher = worker.Dispatcher(1, job)

    async def go():
        async with anyio.create_task_group() as tg:
            await dispatcher.dispatch(tg)

    anyio.run(go)
    assert dispatcher.running == 0
    assert "boom" in store.get_issue("t1")["needs_attention_reason"]


def test_ingest_queues_only_new_bugs(monkeypatch):
    seed_issue(store.PR_OPENED, issue_id="old", identifier="BEI-1")

    async def fake_list():
        return [{"id": "old", "identifier": "BEI-1", "title": "a"}, {"id": "new", "identifier": "BEI-2", "title": "b"}]

    monkeypatch.setattr(worker, "list_todo_bugs", fake_list)
    assert anyio.run(worker.ingest) == ["BEI-2"]
    assert anyio.run(worker.ingest) == []
    assert store.get_issue("old")["status"] == store.PR_OPENED


def test_ingest_queues_oldest_first(monkeypatch):
    async def newest_first():
        return [
            {"id": "b", "identifier": "BEI-2", "title": "", "createdAt": "2026-10-02T00:00:00Z"},
            {"id": "a", "identifier": "BEI-1", "title": "", "createdAt": "2026-10-01T00:00:00Z"},
        ]

    monkeypatch.setattr(worker, "list_todo_bugs", newest_first)
    assert anyio.run(worker.ingest) == ["BEI-1", "BEI-2"]
    assert store.claim_next() == ("a", "triage")


@pytest.mark.parametrize("job, expected", [("triage", ("triage", "t1")), ("implement", ("implement", "t1", True)), ("rework", ("rework", "t1", False, "Check routing"))])
def test_run_job_routes_to_the_pipeline(monkeypatch, job, expected):
    seed_issue(store.REWORKING, issue_id="t1", pending_feedback="Check routing")
    calls = []

    async def fake_triage(issue_id, owner):
        calls.append(("triage", issue_id))

    async def fake_implement(issue_id, approved, feedback=None, owner=None):
        calls.append(("implement", issue_id, approved) if approved else ("rework", issue_id, approved, feedback))

    monkeypatch.setattr(worker.pipeline, "triage_and_plan_issue", fake_triage)
    monkeypatch.setattr(worker.pipeline, "implement_and_review", fake_implement)
    anyio.run(worker.run_job, "t1", job)
    assert calls == [expected]


def test_finished_bug_back_in_todo_is_triaged_again_with_its_history(monkeypatch):
    ws = seed_issue(store.MERGED, slot=None, pr_url="https://ado/pullrequest/7", pr_id=7)
    (ws / "plan.md").write_text("# Old plan")
    (ws / "report.md").write_text("Root cause: old guess")

    async def back_in_todo():
        return [{"id": "issue-1", "identifier": "BEI-1", "title": "Grade shows .None"}]

    monkeypatch.setattr(worker, "list_todo_bugs", back_in_todo)
    assert anyio.run(worker.ingest) == ["BEI-1 (reopened)"]

    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["pr_url"], issue["session_id"]) == (store.QUEUED, None, None)
    assert not (ws / "plan.md").exists()
    section = worker.pipeline.previous_attempt_section(ws)
    assert "https://ado/pullrequest/7" in section and "# Old plan" in section and "old guess" in section
    # Already queued: a second poll doesn't reopen it again
    assert anyio.run(worker.ingest) == []


def test_bug_still_in_progress_isnt_reopened(monkeypatch):
    seed_issue(store.PR_OPENED, pr_id=7)

    async def in_todo():
        return [{"id": "issue-1", "identifier": "BEI-1", "title": ""}]

    monkeypatch.setattr(worker, "list_todo_bugs", in_todo)
    assert anyio.run(worker.ingest) == []
    assert store.get_issue("issue-1")["status"] == store.PR_OPENED
