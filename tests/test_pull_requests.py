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
        {"name": "Run tests", "type": "Task", "result": "failed", "log": {"url": "https://ado/logs/5"}, "issues": [{"type": "error", "message": "Bash exited with code '1'."}, {"type": "warning", "message": "slow"}]},
        {"name": "Python lint and tests", "type": "Job", "result": "failed", "log": {"url": "https://ado/logs/4"}, "issues": []},
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
        if path.startswith("/logs/"):
            return httpx.Response(200, text="2026-10-19T07:38:40.0143543Z == short test summary info ==\n2026-10-19T07:38:40.0143731Z FAILED apps/api/tests/test_grades.py::test_none\n")
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

    async def fake_cleanup(issue_id, merge_commit=None, merged_at=None):
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
    assert "- Run tests: Bash exited with code '1'." in problem
    assert "End of the Run tests log" in problem
    assert "\nFAILED apps/api/tests/test_grades.py::test_none" in problem
    # Only the task's log: the job's would repeat it
    assert "Python lint and tests log" not in problem
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


def test_pat_without_build_scope_still_handles_conflicts(monkeypatch, capsys):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "test-pat")
    seed_issue(store.PR_OPENED, pr_id=7)
    seed_issue(store.PR_OPENED, issue_id="issue-2", identifier="BEI-2", slot=2, pr_id=8)
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request):
        if "/_apis/build/" in request.url.path:
            return httpx.Response(401)
        conflicted = request.url.path.endswith("/8")
        return httpx.Response(200, json=pr(merge_status="conflicts" if conflicted else "succeeded"))

    monkeypatch.setattr(pull_requests.httpx, "AsyncClient", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    check()
    assert store.get_issue("issue-2")["status"] == store.UPDATE_QUEUED
    assert issue()["status"] == store.PR_OPENED
    assert "Build (Read) scope" in capsys.readouterr().out


def test_build_that_failed_before_any_job_reports_its_validation_errors(monkeypatch):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/timeline"):
            return httpx.Response(204)
        return httpx.Response(200, json={"id": 11, "validationResults": [{"result": "error", "message": "/devops/azure-pipelines.yaml: There is not enough space on the disk.\n"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(pull_requests.httpx, "AsyncClient", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "test-pat")

    async def go():
        async with pull_requests.ado_client() as client:
            return await pull_requests.build_failure(client, 11)

    assert anyio.run(go) == "Errors:\n- /devops/azure-pipelines.yaml: There is not enough space on the disk."
