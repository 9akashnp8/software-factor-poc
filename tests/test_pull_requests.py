import anyio
import httpx
import pytest

from conftest import seed_issue
from software_factory import config, pull_requests, store

SOURCE, TARGET = "src1", "tgt1"


def pr(status="active", merge_status="succeeded", source=SOURCE, target=TARGET) -> dict:
    return {
        "status": status,
        "mergeStatus": merge_status,
        "lastMergeSourceCommit": {"commitId": source},
        "lastMergeTargetCommit": {"commitId": target},
    }


def build(build_id=11, result="failed", status="completed", sha=SOURCE) -> dict:
    return {
        "id": build_id,
        "status": status,
        "result": result,
        "triggerInfo": {"pr.sourceSha": sha},
        "_links": {"web": {"href": f"https://ado/build/{build_id}"}},
    }


TIMELINE = {
    "records": [
        {"name": "Run tests", "result": "failed", "issues": [{"type": "error", "message": "test_grades failed"}, {"type": "warning", "message": "slow"}]},
        {"name": "Lint", "result": "succeeded", "issues": [{"type": "error", "message": "ignored"}]},
    ]
}


@pytest.fixture
def ado(monkeypatch):
    """Serve canned Azure DevOps responses; set state['pr'] and state['builds'] per test."""
    state = {"pr": pr(), "builds": [], "requests": []}

    def handler(request: httpx.Request):
        state["requests"].append(request)
        path = request.url.path
        if path.endswith("/timeline"):
            return httpx.Response(200, json=TIMELINE)
        if path.endswith("/build/builds"):
            assert request.url.params["branchName"] == "refs/pull/7/merge"
            return httpx.Response(200, json={"value": state["builds"]})
        return httpx.Response(200, json=state["pr"])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(pull_requests.httpx, "AsyncClient", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "test-pat")
    return state


@pytest.fixture
def merged(monkeypatch) -> list:
    calls = []

    async def fake_cleanup(issue_id):
        calls.append(issue_id)

    monkeypatch.setattr(pull_requests.cleanup, "cleanup_after_merge", fake_cleanup)
    return calls


def check():
    anyio.run(pull_requests.check_pull_requests)


def issue() -> dict:
    return store.get_issue("issue-1")


def test_completed_pr_is_cleaned_up(ado, merged):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["pr"] = pr(status="completed")
    check()
    assert merged == ["issue-1"]


def test_abandoned_pr_needs_attention(ado, merged):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["pr"] = pr(status="abandoned")
    check()
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert merged == []


def test_green_pr_is_left_alone(ado, merged):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["builds"] = [build(result="succeeded")]
    check()
    assert issue()["status"] == store.PR_OPENED


def test_conflicts_send_the_pr_back_once_per_commit_pair(ado):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["pr"] = pr(merge_status="conflicts")
    check()
    assert issue()["status"] == store.UPDATE_QUEUED
    assert "merge conflicts" in issue()["pending_feedback"]

    # The agent's update didn't land yet and the same pair still conflicts: nothing new to do
    store.transition("issue-1", store.PR_OPENED)
    check()
    assert issue()["status"] == store.PR_OPENED

    # The base moved on and conflicts again
    ado["pr"] = pr(merge_status="conflicts", target="tgt2")
    check()
    assert issue()["status"] == store.UPDATE_QUEUED


def test_failed_build_sends_its_errors_back(ado):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["builds"] = [build()]
    check()
    problem = issue()["pending_feedback"]
    assert issue()["status"] == store.UPDATE_QUEUED
    assert "https://ado/build/11" in problem
    assert "Run tests: test_grades failed" in problem
    assert "slow" not in problem and "ignored" not in problem


def test_same_failed_build_isnt_sent_twice(ado):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["builds"] = [build()]
    check()
    store.transition("issue-1", store.PR_OPENED)
    check()
    assert issue()["status"] == store.PR_OPENED


def test_build_of_an_older_push_is_ignored(ado):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["builds"] = [build(sha="old")]
    check()
    assert issue()["status"] == store.PR_OPENED


def test_running_build_is_waited_for(ado):
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["builds"] = [build(status="inProgress", result=None)]
    check()
    assert issue()["status"] == store.PR_OPENED


def test_update_rounds_are_capped(ado, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPDATE_ROUNDS", 1)
    seed_issue(store.PR_OPENED, pr_id=7)
    ado["builds"] = [build(build_id=11)]
    check()
    store.transition("issue-1", store.PR_OPENED)
    ado["builds"] = [build(build_id=12)]
    check()
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert "after 1 update rounds" in issue()["needs_attention_reason"]


def test_pr_merged_while_waiting_for_its_update_is_still_cleaned_up(ado, merged):
    seed_issue(store.UPDATE_QUEUED, pr_id=7)
    ado["pr"] = pr(status="completed")
    check()
    assert merged == ["issue-1"]


def test_without_a_pat_nothing_is_checked(ado, monkeypatch, merged):
    seed_issue(store.PR_OPENED, pr_id=7)
    monkeypatch.delenv("AZURE_DEVOPS_PAT")
    check()
    assert ado["requests"] == []
