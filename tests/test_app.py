import base64
import re
import time
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

import app as web
from conftest import seed_issue
from software_factory import store


def basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


AUTH = basic("akash", "s3cret")


@pytest.fixture
def ws() -> Path:
    ws = seed_issue(store.PLAN_READY)
    (ws / "plan.md").write_text("# Plan\n\nFix it.")
    (ws / "issue.md").write_text('<issue id="BEI-1">\nTitle: t\n\nHi <script>alert(1)</script> <img src=x onerror="alert(2)">\n</issue>')
    return ws


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web, "USERS", {"akash": generate_password_hash("s3cret")})
    monkeypatch.setitem(web.app.config, "SECRET_KEY", "test")
    return web.app.test_client()


def csrf_token(client, issue_id="issue-1") -> str:
    page = client.get(f"/issue/{issue_id}/plan", headers=AUTH).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def test_requests_without_login_are_refused(client, ws):
    response = client.get("/")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


@pytest.mark.parametrize("headers", [basic("akash", "wrong"), basic("nobody", "s3cret")])
def test_wrong_credentials_are_refused(client, ws, headers):
    assert client.get("/", headers=headers).status_code == 401


def test_without_configured_users_everything_is_refused(client, ws, monkeypatch):
    monkeypatch.setattr(web, "USERS", {})
    assert client.get("/", headers=AUTH).status_code == 503


def test_login_shows_issues(client, ws):
    response = client.get("/", headers=AUTH)
    assert response.status_code == 200
    assert "BEI-1" in response.get_data(as_text=True)


def test_issue_context_strips_script(client, ws):
    page = client.get("/issue/issue-1/issue_context", headers=AUTH).get_data(as_text=True)
    assert "<script>" not in page
    assert "onerror" not in page
    assert "Hi" in page


def test_approve_without_csrf_token_is_rejected(client, ws):
    assert client.post("/issue/issue-1/approve", headers=AUTH).status_code == 400
    assert store.get_issue("issue-1")["status"] == store.PLAN_READY


def test_approve_with_csrf_token_queues_implementation(client, ws):
    token = csrf_token(client)
    response = client.post("/issue/issue-1/approve", headers=AUTH, data={"csrf_token": token})
    assert response.status_code == 302
    issue = store.get_issue("issue-1")
    assert issue["status"] == store.IMPLEMENT_QUEUED
    assert issue["plan_reviewed_by"] == "akash"
    assert store.events("issue-1")[-1]["actor"] == "akash"


def test_rework_with_csrf_token_queues_feedback(client, ws):
    token = csrf_token(client)
    client.post("/issue/issue-1/rework-plan", headers=AUTH, data={"csrf_token": token, "feedback": "Check the router"})
    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["pending_feedback"]) == (store.REWORK_QUEUED, "Check the router")


def test_load_users():
    assert web._load_users("a:scrypt:1$x$y, b:pbkdf2:z,") == {"a": "scrypt:1$x$y", "b": "pbkdf2:z"}


def test_second_action_after_queueing_is_refused(client, ws):
    token = csrf_token(client)
    client.post("/issue/issue-1/approve", headers=AUTH, data={"csrf_token": token})
    response = client.post("/issue/issue-1/rework-plan", headers=AUTH, data={"csrf_token": token, "feedback": "x"}, follow_redirects=True)
    assert store.get_issue("issue-1")["status"] == store.IMPLEMENT_QUEUED
    assert store.get_issue("issue-1")["pending_feedback"] is None
    assert "Can&#39;t rework" in response.get_data(as_text=True)


def test_buttons_follow_status(client, ws):
    assert "Approve Plan" in client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    store.transition("issue-1", store.PR_OPENED)
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Approve Plan" not in page and "Rework Plan" not in page


def test_issue_without_plan_renders(client):
    seed_issue(store.PLANNING)
    response = client.get("/issue/issue-1/plan", headers=AUTH)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "No plan yet" in page and "Approve Plan" not in page


def test_needs_attention_shows_reason(client, ws):
    store.transition("issue-1", store.NEEDS_ATTENTION, reason="The implement run didn't finish (error_max_turns)")
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "error_max_turns" in page and "Approve Plan" in page


def test_unknown_issue_is_404(client):
    assert client.get("/issue/nope/plan", headers=AUTH).status_code == 404
    assert client.get("/issue/nope/issue_context", headers=AUTH).status_code == 404


def test_retry_requeues_an_issue_that_failed_before_planning(client):
    seed_issue(store.NEEDS_ATTENTION)
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Retry triage" in page and "Approve Plan" not in page
    client.post("/issue/issue-1/retry", headers=AUTH, data={"csrf_token": csrf_token(client)})
    assert store.get_issue("issue-1")["status"] == store.QUEUED


def test_pages_show_rate_limit_pause(client, ws):
    store.pause_until(time.time() + 600)
    assert "Rate limited" in client.get("/", headers=AUTH).get_data(as_text=True)


def test_release_queues_the_teardown(client, ws):
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Release" in page
    client.post("/issue/issue-1/release", headers=AUTH, data={"csrf_token": csrf_token(client)})
    assert store.get_issue("issue-1")["status"] == store.RELEASE_QUEUED


def test_plan_page_warns_about_overlapping_work(client, ws, monkeypatch):
    monkeypatch.setattr(web, "overlaps", lambda issue: [("BEI-2", store.PR_OPENED, ["apps/api/router.py"])])
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Likely to conflict" in page and "BEI-2" in page and "apps/api/router.py" in page


def test_open_pr_queues_publishing(client, ws):
    store.transition("issue-1", store.NEEDS_ATTENTION, reason="Uncommitted changes left in the worktree: x.ts")
    (ws / "report.md").write_text("Root cause: x")
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Open PR" in page
    client.post("/issue/issue-1/open-pr", headers=AUTH, data={"csrf_token": csrf_token(client)})
    assert store.get_issue("issue-1")["status"] == store.PUBLISH_QUEUED
