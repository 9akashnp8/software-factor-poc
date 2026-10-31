import importlib
import json
import subprocess
from pathlib import Path

import anyio
import httpx
import pytest

main = importlib.import_module("software_factory.main")

BRANCH = "fix/BEI-1"


def run(cwd: Path, *args: str):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def commit(repo: Path, name: str):
    (repo / name).write_text(name)
    run(repo, "add", name)
    run(repo, "commit", "-m", name)


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    run(tmp_path, "init", "--bare", "-b", main.BASE_BRANCH, str(remote))
    return remote


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-b", main.BASE_BRANCH)
    run(repo, "config", "user.email", "test@example.com")
    run(repo, "config", "user.name", "Test")
    run(repo, "remote", "add", "origin", str(remote))
    commit(repo, "base.txt")
    run(repo, "checkout", "-b", BRANCH)
    return repo


@pytest.fixture
def ws(tmp_path: Path, monkeypatch, repo: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "bug_workspace" / "issue-1"
    ws.mkdir(parents=True)
    (ws / "metadata.json").write_text(json.dumps({"identifier": "BEI-1", "status": "plan-approved-agent-working", "session_id": "s"}))
    (ws / "issue.md").write_text('<issue id="BEI-1">\nTitle: Grade shows .None\nDescription:\n...\n</issue>')
    (ws / "report.md").write_text("Root cause: placeholder grade.")
    monkeypatch.setattr(main, "ensure_worktree", lambda _: repo)
    return ws


def metadata(ws: Path) -> dict:
    return json.loads((ws / "metadata.json").read_text())


def stub_create(monkeypatch, url="https://ado/pullrequest/7", error=None):
    calls = []

    async def fake_create(branch, title, description):
        calls.append((branch, title, description))
        if error:
            raise error
        return url

    monkeypatch.setattr(main, "create_pull_request", fake_create)
    return calls


def open_pr():
    anyio.run(main.open_pull_request, "issue-1")


def test_pushes_and_records_pr_url(ws, repo, remote, monkeypatch):
    commit(repo, "fix.txt")
    calls = stub_create(monkeypatch)
    open_pr()
    meta = metadata(ws)
    assert meta["status"] == "pr-opened"
    assert meta["pr_url"] == "https://ado/pullrequest/7"
    assert calls == [(BRANCH, "BEI-1: Grade shows .None", "Root cause: placeholder grade.")]
    pushed = subprocess.run(["git", "-C", str(remote), "rev-parse", BRANCH], capture_output=True, text=True).stdout.strip()
    assert pushed == subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_no_commits_skips_push(ws, monkeypatch):
    calls = stub_create(monkeypatch)
    open_pr()
    assert metadata(ws)["status"] == "needs-attention"
    assert calls == []


def test_push_failure_needs_attention(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    run(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    calls = stub_create(monkeypatch)
    open_pr()
    meta = metadata(ws)
    assert meta["status"] == "needs-attention"
    assert meta["needs_attention_reason"].startswith("git push failed:")
    assert calls == []


def test_pr_failure_needs_attention(ws, repo, monkeypatch):
    commit(repo, "fix.txt")
    stub_create(monkeypatch, error=RuntimeError("AZURE_DEVOPS_PAT is not set"))
    open_pr()
    meta = metadata(ws)
    assert meta["status"] == "needs-attention"
    assert meta["needs_attention_reason"] == "PR creation failed: AZURE_DEVOPS_PAT is not set"
    assert "pr_url" not in meta


def test_description_includes_review_and_truncates(ws):
    (ws / "review.md").write_text("**Verdict:** Not blocking")
    assert main.pr_description(ws) == "Root cause: placeholder grade.\n\n---\n\n**Verdict:** Not blocking"
    (ws / "report.md").write_text("x" * 5000)
    assert len(main.pr_description(ws)) <= main.ADO_DESCRIPTION_LIMIT


def mock_ado(monkeypatch, handler):
    """Route create_pull_request's HTTP calls to handler instead of the network."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "test-pat")


def test_create_pull_request_posts_draft(monkeypatch):
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"value": []})
        return httpx.Response(201, json={"pullRequestId": 42})

    mock_ado(monkeypatch, handler)
    url = anyio.run(main.create_pull_request, BRANCH, "BEI-1: t", "d")
    assert url == f"{main.ADO_REPO_URL}/pullrequest/42"
    get, post = requests
    assert get.url.params["searchCriteria.sourceRefName"] == f"refs/heads/{BRANCH}"
    assert post.url.params["api-version"] == main.ADO_API_VERSION
    body = json.loads(post.content)
    assert body["isDraft"] is True
    assert body["targetRefName"] == f"refs/heads/{main.BASE_BRANCH}"
    assert post.headers["authorization"].startswith("Basic ")


def test_create_pull_request_reuses_active_pr(monkeypatch):
    def handler(request: httpx.Request):
        assert request.method == "GET"
        return httpx.Response(200, json={"value": [{"pullRequestId": 9}]})

    mock_ado(monkeypatch, handler)
    assert anyio.run(main.create_pull_request, BRANCH, "t", "d") == f"{main.ADO_REPO_URL}/pullrequest/9"


def test_create_pull_request_requires_pat(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_DEVOPS_PAT"):
        anyio.run(main.create_pull_request, BRANCH, "t", "d")
