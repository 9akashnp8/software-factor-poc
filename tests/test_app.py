import base64
import json
import re
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

import app as web


def basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


AUTH = basic("akash", "s3cret")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch) -> Path:
    ws = tmp_path / "issue-1"
    ws.mkdir()
    (ws / "metadata.json").write_text(json.dumps({"identifier": "BEI-1", "status": "plan-generated-for-review", "session_id": "s"}))
    (ws / "plan.md").write_text("# Plan\n\nFix it.")
    (ws / "issue.md").write_text('<issue id="BEI-1">\nTitle: t\n\nHi <script>alert(1)</script> <img src=x onerror="alert(2)">\n</issue>')
    monkeypatch.setattr(web, "BUG_WORKSPACE_DIR", tmp_path)
    return ws


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web, "USERS", {"akash": generate_password_hash("s3cret")})
    monkeypatch.setitem(web.app.config, "SECRET_KEY", "test")
    return web.app.test_client()


@pytest.fixture
def started(monkeypatch) -> list:
    """Record agent runs instead of starting them."""
    runs = []
    monkeypatch.setattr(web, "_running", set())
    monkeypatch.setattr(web, "_worker", lambda *args: runs.append(args))
    return runs


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
    assert "issue-1" in response.get_data(as_text=True)


def test_issue_context_strips_script(client, ws):
    page = client.get("/issue/issue-1/issue_context", headers=AUTH).get_data(as_text=True)
    assert "<script>" not in page
    assert "onerror" not in page
    assert "Hi" in page


def test_approve_without_csrf_token_is_rejected(client, ws, started):
    assert client.post("/issue/issue-1/approve", headers=AUTH).status_code == 400
    assert started == []
    assert json.loads((ws / "metadata.json").read_text())["status"] == "plan-generated-for-review"


def test_approve_with_csrf_token_starts_implementation(client, ws, started):
    token = csrf_token(client)
    response = client.post("/issue/issue-1/approve", headers=AUTH, data={"csrf_token": token})
    assert response.status_code == 302
    meta = json.loads((ws / "metadata.json").read_text())
    assert meta["status"] == "plan-approved-agent-working"
    assert meta["plan_reviewed_by"] == "akash"
    assert started == [("issue-1", True, "")]


def test_rework_with_csrf_token_sends_feedback(client, ws, started):
    token = csrf_token(client)
    client.post("/issue/issue-1/rework-plan", headers=AUTH, data={"csrf_token": token, "feedback": "Check the router"})
    assert started == [("issue-1", False, "Check the router")]


def test_load_users():
    assert web._load_users("a:scrypt:1$x$y, b:pbkdf2:z,") == {"a": "scrypt:1$x$y", "b": "pbkdf2:z"}
