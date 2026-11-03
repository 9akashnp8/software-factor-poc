import base64
import json
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


def test_refresh_queues_the_plan_and_hides_approve(client, ws):
    token = csrf_token(client)
    client.post("/issue/issue-1/refresh-plan", headers=AUTH, data={"csrf_token": token})
    assert store.get_issue("issue-1")["status"] == store.REFRESH_QUEUED
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Approve plan" not in page and "Refresh plan" not in page


def test_plan_page_says_when_dev_has_moved(client, ws, monkeypatch):
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Refresh plan" in page and "since this plan was written" not in page
    monkeypatch.setattr(web.dev_changes, "summary", lambda issue: "2 new merges to dev, including BEI-17")
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "<strong>2 new merges to dev, including BEI-17</strong> since this plan was written." in page


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
    assert "Approve plan" in client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    store.transition("issue-1", store.PR_OPENED)
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Approve plan" not in page and "Request rework" not in page


def test_issue_without_plan_renders(client):
    seed_issue(store.PLANNING)
    response = client.get("/issue/issue-1/plan", headers=AUTH)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "No plan yet" in page and "Approve plan" not in page


def test_needs_attention_shows_reason(client, ws):
    store.transition("issue-1", store.NEEDS_ATTENTION, reason="The implement run didn't finish (error_max_turns)")
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "error_max_turns" in page and "Approve plan" in page


def test_unknown_issue_is_404(client):
    assert client.get("/issue/nope/plan", headers=AUTH).status_code == 404
    assert client.get("/issue/nope/issue_context", headers=AUTH).status_code == 404


def test_retry_requeues_an_issue_that_failed_before_planning(client):
    seed_issue(store.NEEDS_ATTENTION)
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Retry triage" in page and "Approve plan" not in page
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


def test_plans_render_as_commonmark():
    # As agents write it: a list straight after a paragraph line, nested by two spaces
    html = str(web.render_md("What the trace shows:\n- **Routing** is fine\n- It needs:\n  - a new row type\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"))
    assert "<p>What the trace shows:</p>\n<ul>\n<li><strong>Routing</strong> is fine</li>" in html
    assert "<li>It needs:\n<ul>\n<li>a new row type</li>" in html
    assert "<table>" in html and "<td>1</td>" in html


def test_stylesheet_leaves_plan_lists_alone(client, ws):
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "/static/factory.css" in page
    css = client.get("/static/factory.css", headers=AUTH).get_data(as_text=True)
    # A bare li rule would restyle every plan bullet
    assert not re.search(r"(^|[},]\s*)li\b[^{]*\{", css, re.M)


NOT_REPRODUCED = {
    "status": "not_reproduced",
    "method": "browser",
    "steps": ["Open the leave balance page"],
    "evidence": "Shows 12 days, as expected",
    "qa_question": "Which employee and which day did you see 0 days for?",
}


def not_reproduced(ws: Path):
    (ws / "reproduction.json").write_text(json.dumps(NOT_REPRODUCED), encoding="utf-8")
    store.set_fields("issue-1", reproduction="not_reproduced")


def test_reproduction_shows_on_the_plan_page(client, ws):
    record = {"status": "reproduced", "method": "test", "steps": ["pytest -k grade"], "evidence": "AssertionError: '.None'"}
    (ws / "reproduction.json").write_text(json.dumps(record), encoding="utf-8")
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Reproduction" in page and "reproduced" in page and "AssertionError: &#39;.None&#39;" in page
    assert "Approve plan" in page and "Question for QA" not in page


def test_a_bug_that_did_not_reproduce_cant_be_approved(client, ws):
    not_reproduced(ws)
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Approve plan" not in page and "Request rework" in page
    assert "Which employee and which day" in page and "Findings" in page
    token = csrf_token(client)
    client.post("/issue/issue-1/approve", headers=AUTH, data={"csrf_token": token})
    assert store.get_issue("issue-1")["status"] == store.PLAN_READY


def test_chat_bug_without_a_replay_is_flagged(client, ws):
    (ws / "conversation.json").write_text("{}", encoding="utf-8")
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Not replayed." in page


def test_posting_the_qa_question_comments_on_linear(client, ws, monkeypatch):
    not_reproduced(ws)
    monkeypatch.setattr(web.config, "LINEAR_WRITEBACK", True)
    posted = []

    async def fake_comment(issue_id, body):
        posted.append((issue_id, body))

    monkeypatch.setattr(web.linear, "comment", fake_comment)
    token = csrf_token(client)
    response = client.post(
        "/issue/issue-1/post-qa-question", headers=AUTH, data={"csrf_token": token, "question": "Which day?"}, follow_redirects=True
    )
    assert posted == [("issue-1", "Which day?")]
    assert "Question posted to Linear" in response.get_data(as_text=True)
    assert store.events("issue-1")[-1]["detail"] == "QA question posted to Linear"
    assert store.events("issue-1")[-1]["actor"] == "akash"


def test_qa_question_isnt_posted_while_writeback_is_off(client, ws, monkeypatch):
    not_reproduced(ws)
    posted = []

    async def fake_comment(issue_id, body):
        posted.append(body)

    monkeypatch.setattr(web.linear, "comment", fake_comment)
    token = csrf_token(client)
    response = client.post(
        "/issue/issue-1/post-qa-question", headers=AUTH, data={"csrf_token": token, "question": "Which day?"}, follow_redirects=True
    )
    assert posted == [] and "SF_LINEAR_WRITEBACK=0" in response.get_data(as_text=True)


def test_approving_can_require_a_browser_check(client, ws):
    token = csrf_token(client)
    assert "Require a browser check" in client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    client.post("/issue/issue-1/approve", headers=AUTH, data={"csrf_token": token, "browser_check": "1"})
    assert store.get_issue("issue-1")["browser_check"] == 1


def test_approving_without_the_box_leaves_the_browser_check_to_the_agent(client, ws):
    token = csrf_token(client)
    client.post("/issue/issue-1/approve", headers=AUTH, data={"csrf_token": token})
    assert store.get_issue("issue-1")["browser_check"] == 0


def test_plan_page_shows_verification_and_serves_only_listed_screenshots(client, ws):
    (ws / "verification.json").write_text(json.dumps({"status": "passed", "browser": {"done": True, "passed": True, "result": "Shows 12 days"}}))
    (ws / "evidence").mkdir()
    (ws / "evidence" / "after.png").write_bytes(b"\x89PNG")
    (ws / "secret.txt").write_text("no")
    page = client.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "Verification" in page and "passed: Shows 12 days" in page
    assert "/issue/issue-1/evidence/after.png" in page
    assert client.get("/issue/issue-1/evidence/after.png", headers=AUTH).data == b"\x89PNG"
    assert client.get("/issue/issue-1/evidence/..%2Fsecret.txt", headers=AUTH).status_code == 404
    assert client.get("/issue/issue-1/evidence/other.png", headers=AUTH).status_code == 404


def test_metrics_page(client, ws):
    page = client.get("/metrics", headers=AUTH).get_data(as_text=True)
    assert "Plans sent back" in page and "Time in each stage" in page
    assert 'aria-current="page">30 days' in page
    assert 'aria-current="page">7 days' in client.get("/metrics?days=7", headers=AUTH).get_data(as_text=True)
    assert 'aria-current="page">30 days' in client.get("/metrics?days=5", headers=AUTH).get_data(as_text=True)
