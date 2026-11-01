import json

import anyio
import httpx
import pytest

from conftest import seed_issue
from software_factory import config, deployments, linear, pull_requests, store

MERGED_AT = "2026-11-01T09:26:17.9652534Z"


def run(build_id, result="succeeded", status="completed", queued="2026-11-01T09:26:20Z", sha="other") -> dict:
    return {
        "id": build_id,
        "status": status,
        "result": result,
        "queueTime": queued,
        "sourceVersion": sha,
        "_links": {"web": {"href": f"https://ado/build/{build_id}"}},
    }


@pytest.fixture
def ado(monkeypatch):
    """Serve the deploy pipeline's runs from state['runs']; failed runs report a full disk."""
    state = {"runs": [], "params": []}

    def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("/timeline"):
            return httpx.Response(204)
        if path.endswith("/build/builds"):
            state["params"].append(dict(request.url.params))
            return httpx.Response(200, json={"value": state["runs"]})
        return httpx.Response(200, json={"validationResults": [{"result": "error", "message": "There is not enough space on the disk."}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(pull_requests.httpx, "AsyncClient", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "test-pat")
    return state


@pytest.fixture
def told(monkeypatch) -> list:
    """What QA would be told in Linear."""
    calls = []

    async def fake_comment(issue_id, body):
        calls.append(("comment", body))

    async def fake_state(issue_id, state):
        calls.append(("state", state))

    monkeypatch.setattr(linear, "report_comment", fake_comment)
    monkeypatch.setattr(linear, "report_state", fake_state)
    return calls


@pytest.fixture
def merged():
    ws = seed_issue(store.DEPLOY_PENDING, slot=None, pr_id=7, pr_url="https://ado/pullrequest/7", merge_commit="merge-sha", merged_at=MERGED_AT)
    (ws / "qa.json").write_text(json.dumps({
        "root_cause": "Times were read as local time; reported by jane@cpc.gov.ae",
        "fix": "Times are now read as UTC.",
    }))
    return ws


def check():
    anyio.run(pull_requests.check_pull_requests)


def issue():
    return store.get_issue("issue-1")


def test_successful_deploy_moves_to_in_review_with_a_comment(ado, told, merged):
    ado["runs"] = [run(8890, sha="merge-sha")]
    check()

    assert issue()["status"] == store.DEPLOYED
    (kind, body), state = told
    assert kind == "comment" and state == ("state", config.LINEAR_MERGED_STATE)
    assert body.startswith(f"**Fix deployed to {config.BASE_BRANCH}** · [PR 7](https://ado/pullrequest/7) · [build 8890](https://ado/build/8890)")
    assert "**Root cause:** Times were read as local time; reported by <email>" in body
    assert "**Fix:** Times are now read as UTC." in body
    params = ado["params"][0]
    assert (params["definitions"], params["branchName"]) == (str(config.DEPLOY_PIPELINE_ID), f"refs/heads/{config.BASE_BRANCH}")


def test_failed_deploy_is_recorded_once_and_keeps_waiting(ado, told, merged):
    ado["runs"] = [run(8874, result="failed", sha="merge-sha")]
    check()
    check()
    assert issue()["status"] == store.DEPLOY_PENDING
    failures = [e["detail"] for e in store.events("issue-1") if "Deploy build 8874 failed" in (e["detail"] or "")]
    assert failures == ["Deploy build 8874 failed: There is not enough space on the disk. Waiting for the next deploy."]
    assert told == []


def test_a_later_deploy_ships_the_fix_after_a_failed_one(ado, told, merged):
    ado["runs"] = [run(8874, result="failed", sha="merge-sha"), run(8875, queued="2026-11-01T10:00:00Z")]
    check()
    assert issue()["status"] == store.DEPLOYED
    assert "[build 8875]" in told[0][1]


def test_runs_queued_before_the_merge_dont_count(ado, told, merged):
    ado["runs"] = [run(8860, queued="2026-11-01T09:23:00Z")]
    check()
    assert issue()["status"] == store.DEPLOY_PENDING


def test_running_deploy_is_waited_for(ado, told, merged):
    ado["runs"] = [run(8890, result=None, status="inProgress", sha="merge-sha")]
    check()
    assert issue()["status"] == store.DEPLOY_PENDING


def test_comment_without_a_qa_summary_still_links_the_pr():
    seed_issue(store.DEPLOYED, pr_id=7, pr_url="https://ado/pullrequest/7")
    body = deployments.comment_body(issue(), run(8890))
    assert body == f"**Fix deployed to {config.BASE_BRANCH}** · [PR 7](https://ado/pullrequest/7) · [build 8890](https://ado/build/8890)"


def test_failed_cleanup_after_merge_is_retried_on_the_next_check(ado, told, monkeypatch):
    seed_issue(store.DEPLOY_PENDING, slot=4, merge_commit="merge-sha", merged_at=MERGED_AT)
    retried = []

    async def fake_teardown(issue_id, final_status, why, delete_remote=False, flag=True):
        retried.append((issue_id, delete_remote, flag))
        return True

    monkeypatch.setattr(pull_requests.cleanup, "teardown", fake_teardown)
    check()
    assert retried == [("issue-1", True, False)]


@pytest.mark.parametrize("details, reason", [
    ("Errors:\n- Build and Push API: #0 building with docker\n- Build and Push API: \n\nEnd of the Build and Push API log:\n```\n##[error]ERROR: failed to build: DeadlineExceeded: ghcr.io/astral-sh/uv\n##[error]The process '/usr/bin/docker' failed with exit code 1\n```",
     "ERROR: failed to build: DeadlineExceeded: ghcr.io/astral-sh/uv"),
    ("Errors:\n- /devops/azure-pipelines.yaml: There is not enough space on the disk.", "/devops/azure-pipelines.yaml: There is not enough space on the disk"),
    ("Errors:\n(none reported)", "no error reported"),
])
def test_failure_reason_finds_the_explaining_line(details, reason):
    assert pull_requests.failure_reason(details) == reason
